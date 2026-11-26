"""How the client reaches Telegram: TG_CONNECTION picks the transport, TG_PROXY an optional proxy."""

from __future__ import annotations

import asyncio
import base64
import binascii
from urllib.parse import parse_qs, quote, unquote, urlsplit

from telethon import connection as tl
from telethon import types
from telethon.network.requeststate import RequestState
from telethon.network.connection.http import HttpPacketCodec
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

# Hosts of Telegram's web clients, one per data center
WS_HOSTS = {1: "pluto", 2: "venus", 3: "aurora", 4: "vesta", 5: "flora"}
HTTP_WAIT_MS = 25000  # how long Telegram may hold the long-poll POST


class _WebSocketStream:
    """The reader/writer pair Telethon's Connection expects, on top of a WebSocket.
    Writes are buffered and go out as one binary frame on drain()."""

    def __init__(self, ws):
        self._ws = ws
        self._in = bytearray()
        self._out = bytearray()
        self._closing = None

    async def readexactly(self, n: int) -> bytes:
        while len(self._in) < n:
            try:
                self._in += await self._ws.recv(decode=False)
            except ConnectionClosed as e:
                raise ConnectionError(f"WebSocket closed: {e}") from e
        data = bytes(self._in[:n])
        del self._in[:n]
        return data

    def write(self, data: bytes) -> None:
        self._out += data

    async def drain(self) -> None:
        if not self._out:
            return
        data, self._out = bytes(self._out), bytearray()
        try:
            await self._ws.send(data)
        except ConnectionClosed as e:
            raise ConnectionError(f"WebSocket closed: {e}") from e

    def close(self) -> None:
        self._closing = asyncio.ensure_future(self._ws.close())

    async def wait_closed(self) -> None:
        if self._closing:
            await self._closing


class ConnectionWebSocket(tl.ConnectionTcpObfuscated):
    """MTProto over wss://<dc>.web.telegram.org/apiws, the way Telegram's web clients connect.
    From the outside it is a TLS connection to a telegram.org web host, which gets through networks
    that block plain MTProto. Same obfuscated framing as ConnectionTcpObfuscated."""

    async def _connect(self, timeout=None, ssl=None):
        host = WS_HOSTS.get(abs(self._dc_id))
        if host is None:
            raise ConnectionError(f"No WebSocket host is known for data center {self._dc_id}.")
        ws = await ws_connect(
            f"wss://{host}.web.telegram.org/apiws",
            subprotocols=["binary"],
            proxy=_proxy_url(self._proxy),
            open_timeout=timeout,
            ping_interval=None,  # MTProto sends its own pings
            max_size=None,
            compression=None,
        )
        self._reader = self._writer = _WebSocketStream(ws)
        self._codec = self.packet_codec(self)
        self._init_conn()
        await self._writer.drain()

    def __str__(self):
        return f"{WS_HOSTS.get(abs(self._dc_id), self._dc_id)}.web.telegram.org/WebSocket"


def _proxy_url(proxy: dict | None) -> str | None:
    """Telethon's proxy dict back to a URL for websockets. None means no proxy, not the system one."""
    if not proxy:
        return None
    scheme = {"socks5": "socks5h", "socks4": "socks4a", "http": "http"}[proxy["proxy_type"]]
    auth = ""
    if proxy.get("username"):
        auth = quote(proxy["username"], safe="") + (":" + quote(proxy.get("password") or "", safe="")) + "@"
    return f"{scheme}://{auth}{proxy['addr']}:{proxy['port']}"


class _HttpPacketCodec(HttpPacketCodec):
    async def read_packet(self, reader):
        # Telethon's version compares line[-1] (an int) with b"\n", so it rejects every response
        length = None
        while True:
            line = await reader.readline()
            if not line.endswith(b"\n"):
                raise asyncio.IncompleteReadError(line, None)
            if not line.strip():
                break
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                length = int(value)
        if length is None:
            raise ConnectionError("Telegram's HTTP response has no Content-Length.")
        return await reader.readexactly(length)


class ConnectionHttp(tl.Connection):
    """MTProto over plain HTTP on port 80, for networks that only let HTTP out.

    Telethon's ConnectionHttp does not work: it switches to TLS on port 443, and it pipelines every
    POST over one keep-alive connection. Telegram holds a POST that only carries acks for up to 25 s
    (a long poll), so the real request behind it waits that long. Here each POST gets its own short
    connection and replies are taken in whatever order they arrive. http_long_poll() keeps one POST
    waiting on the server, because over HTTP a result can only come back inside some POST's response."""

    packet_codec = _HttpPacketCodec

    def __init__(self, ip, port, dc_id, **kwargs):
        super().__init__(ip, 80, dc_id, **kwargs)
        self._replies: asyncio.Queue = asyncio.Queue()
        self._posts: set[asyncio.Task] = set()
        self._idle = asyncio.Event()  # no POST in flight
        self._idle.set()
        self._timeout = None

    async def _open(self):
        if self._proxy:
            return await asyncio.open_connection(sock=await self._proxy_connect(timeout=self._timeout))
        return await asyncio.wait_for(asyncio.open_connection(self._ip, self._port), self._timeout)

    async def _connect(self, timeout=None, ssl=None):
        self._timeout = timeout
        _, writer = await self._open()  # fail here, not on the first request, if the address is unreachable
        writer.close()
        self._codec = self.packet_codec(self)

    def _send(self, data):
        task = asyncio.ensure_future(self._post(self._codec.encode_packet(data)))
        self._posts.add(task)
        self._idle.clear()
        task.add_done_callback(self._post_done)

    def _post_done(self, task):
        self._posts.discard(task)
        if not self._posts:
            self._idle.set()

    async def _post(self, request: bytes) -> None:
        writer = None
        try:
            reader, writer = await self._open()
            writer.write(request)
            await writer.drain()
            await self._replies.put((await self._codec.read_packet(reader), None))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._replies.put((None, e))
        finally:
            if writer:
                writer.close()

    async def _send_loop(self):
        try:
            while self._connected:
                self._send(await self._send_queue.get())
        except asyncio.CancelledError:
            pass

    async def _recv(self):
        body, error = await self._replies.get()
        if error:
            raise error
        return body

    async def disconnect(self):
        for task in list(self._posts):
            task.cancel()
        await super().disconnect()


async def http_long_poll(client) -> None:
    """While the client uses ConnectionHttp, send an http_wait whenever no POST is in flight, so
    Telegram always holds one request it can answer with RPC results and updates. Start it before
    client.connect(), which itself waits for the first request."""
    sender = client._sender
    while not sender.is_connected():
        await asyncio.sleep(0.05)
    while sender.is_connected():
        conn = sender._connection
        if not isinstance(conn, ConnectionHttp) or not conn._connected:
            await asyncio.sleep(0.5)  # connecting or reconnecting
            continue
        try:
            await asyncio.wait_for(conn._idle.wait(), 5)
        except asyncio.TimeoutError:
            continue
        conn._idle.clear()  # the http_wait below becomes the POST in flight
        wait = types.HttpWait(max_delay=0, wait_after=0, max_wait=HTTP_WAIT_MS)
        sender._send_queue.append(RequestState(wait))


CONNECTIONS = {
    "tcp": tl.ConnectionTcpFull,
    "obfuscated": tl.ConnectionTcpObfuscated,
    "http": ConnectionHttp,
    "websocket": ConnectionWebSocket,
}


def parse_proxy(url: str) -> tuple[str, dict | tuple]:
    """("socks", Telethon proxy dict) or ("mtproxy", (host, port, secret)).

    Accepts socks5://user:pass@host:port, socks4://host:port, http://host:port (HTTP CONNECT),
    and the share links Telegram uses: https://t.me/proxy?server=..&port=..&secret=.. (MTProxy)
    and https://t.me/socks?server=..&port=..&user=..&pass=.. (also tg://proxy and tg://socks).
    """
    u = urlsplit(url.strip())
    scheme = u.scheme.lower()
    link = None
    if scheme == "tg":
        link = u.netloc.lower()
    elif scheme in {"http", "https"} and u.netloc.lower() in {"t.me", "telegram.me"}:
        link = u.path.strip("/").lower()
    if link in {"proxy", "socks"}:
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            host, port = q["server"], int(q["port"])
        except (KeyError, ValueError):
            raise ValueError("TG_PROXY: the link needs server and port.") from None
        if link == "socks":
            return "socks", _socks("socks5", host, port, q.get("user"), q.get("pass"))
        secret = q.get("secret", "")
        raw = _secret_bytes(secret)
        if not raw:
            raise ValueError("TG_PROXY: the MTProxy link has no valid secret.")
        if raw[0] == 0xEE:
            raise ValueError("TG_PROXY: fake-TLS MTProxy secrets (ee...) are not supported by Telethon.")
        return "mtproxy", (host, port, secret)

    kinds = {"socks5": "socks5", "socks5h": "socks5", "socks4": "socks4", "socks4a": "socks4", "http": "http"}
    if scheme not in kinds or not u.hostname or not u.port:
        raise ValueError(
            "TG_PROXY must look like socks5://user:pass@host:port, socks4://host:port, http://host:port "
            "or a t.me/proxy / t.me/socks link."
        )
    user, password = (unquote(v) if v else None for v in (u.username, u.password))
    return "socks", _socks(kinds[scheme], u.hostname, u.port, user, password)


def _secret_bytes(secret: str) -> bytes:
    """MTProxy secrets come as hex or as url-safe base64."""
    try:
        return bytes.fromhex(secret)
    except ValueError:
        pass
    try:
        return base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
    except (binascii.Error, ValueError):
        return b""


def _socks(kind: str, host: str, port: int, username: str | None, password: str | None) -> dict:
    return {"proxy_type": kind, "addr": host, "port": port, "username": username, "password": password, "rdns": True}


def client_options(connection: str, proxy: str | None) -> dict:
    """connection= and proxy= arguments for TelegramClient."""
    if connection not in CONNECTIONS:
        raise ValueError(f"TG_CONNECTION must be one of {', '.join(CONNECTIONS)}, not {connection!r}.")
    if not proxy:
        return {"connection": CONNECTIONS[connection], "proxy": None}
    kind, info = parse_proxy(proxy)
    if kind == "mtproxy":
        if connection != "tcp":
            raise ValueError("MTProxy uses its own transport: leave TG_CONNECTION empty or set it to tcp.")
        return {"connection": tl.ConnectionTcpMTProxyRandomizedIntermediate, "proxy": info}
    return {"connection": CONNECTIONS[connection], "proxy": info}


def describe(connection: str, proxy: str | None) -> str:
    """For tg_status, without the proxy's password or secret."""
    if not proxy:
        return connection
    kind, info = parse_proxy(proxy)
    if kind == "mtproxy":
        return f"MTProxy {info[0]}:{info[1]}"
    return f"{connection} via {info['proxy_type']} proxy {info['addr']}:{info['port']}"
