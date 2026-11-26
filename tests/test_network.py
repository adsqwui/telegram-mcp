import asyncio
import logging
from collections import defaultdict
from types import SimpleNamespace

import pytest
from telethon import connection as tl
from telethon import types
from websockets.exceptions import ConnectionClosedOK
from websockets.frames import Close

from telegram_mcp import network

LOGGERS = defaultdict(lambda: logging.getLogger("test"))


@pytest.mark.parametrize(
    "url, expected",
    [
        ("socks5://127.0.0.1:1080", ("socks5", "127.0.0.1", 1080, None, None)),
        ("socks5h://u%40x:p%3Aw@proxy.lan:1080", ("socks5", "proxy.lan", 1080, "u@x", "p:w")),
        ("socks4://10.0.0.1:9050", ("socks4", "10.0.0.1", 9050, None, None)),
        ("http://user:pw@10.0.0.1:3128", ("http", "10.0.0.1", 3128, "user", "pw")),
        ("https://t.me/socks?server=1.2.3.4&port=1080&user=a&pass=b%25c", ("socks5", "1.2.3.4", 1080, "a", "b%c")),
        ("tg://socks?server=1.2.3.4&port=1080", ("socks5", "1.2.3.4", 1080, None, None)),
    ],
)
def test_parse_socks_and_http_proxies(url, expected):
    kind, info = network.parse_proxy(url)
    assert kind == "socks"
    assert (info["proxy_type"], info["addr"], info["port"], info["username"], info["password"]) == expected
    assert info["rdns"] is True


def test_parse_mtproxy_links():
    secret = "dd" + "ab" * 16
    for url in (f"https://t.me/proxy?server=mt.example&port=443&secret={secret}",
                f"tg://proxy?server=mt.example&port=443&secret={secret}"):
        assert network.parse_proxy(url) == ("mtproxy", ("mt.example", 443, secret))
    plain_base64 = "7Lw" + "A" * 19  # starts with 0xEC: looks like base64 fake-TLS at a glance, is not
    assert network.parse_proxy(f"tg://proxy?server=h&port=1&secret={plain_base64}")[0] == "mtproxy"


@pytest.mark.parametrize(
    "url, error",
    [
        ("https://t.me/proxy?server=a&port=443&secret=ee" + "ab" * 16, "fake-TLS"),
        ("https://t.me/proxy?server=a&port=443&secret=7gAB", "fake-TLS"),  # the same, base64
        ("https://t.me/proxy?server=a&port=443", "no valid secret"),
        ("https://t.me/proxy?server=a", "server and port"),
        ("ftp://host:21", "TG_PROXY must look like"),
        ("socks5://host", "TG_PROXY must look like"),
    ],
)
def test_bad_proxies(url, error):
    with pytest.raises(ValueError, match=error):
        network.parse_proxy(url)


def test_client_options():
    assert network.client_options("tcp", None) == {"connection": tl.ConnectionTcpFull, "proxy": None}
    assert network.client_options("obfuscated", None)["connection"] is tl.ConnectionTcpObfuscated
    assert network.client_options("http", None)["connection"] is network.ConnectionHttp
    ws = network.client_options("websocket", "socks5://h:1")
    assert ws["connection"] is network.ConnectionWebSocket and ws["proxy"]["addr"] == "h"
    mt = network.client_options("tcp", "tg://proxy?server=h&port=443&secret=" + "ab" * 16)
    assert mt["connection"] is tl.ConnectionTcpMTProxyRandomizedIntermediate and mt["proxy"][0] == "h"
    with pytest.raises(ValueError, match="TG_CONNECTION must be one of"):
        network.client_options("udp", None)
    with pytest.raises(ValueError, match="MTProxy uses its own transport"):
        network.client_options("websocket", "tg://proxy?server=h&port=443&secret=" + "ab" * 16)


def test_describe_hides_credentials():
    assert network.describe("tcp", None) == "tcp"
    text = network.describe("websocket", "socks5://user:hunter2@10.0.0.1:1080")
    assert text == "websocket via socks5 proxy 10.0.0.1:1080" and "hunter2" not in text
    secret = "ab" * 16
    assert secret not in network.describe("tcp", f"tg://proxy?server=h&port=443&secret={secret}")


def test_proxy_url_for_websockets():
    assert network._proxy_url(None) is None  # None, not True: websockets would use the system proxy
    _, info = network.parse_proxy("socks5://u%40x:p%3Aw@h:1080")
    assert network._proxy_url(info) == "socks5h://u%40x:p%3Aw@h:1080"
    _, info = network.parse_proxy("http://h:3128")
    assert network._proxy_url(info) == "http://h:3128"


def feed(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


def test_http_codec_reads_headers_in_any_order():
    async def run():
        codec = network._HttpPacketCodec(None)
        raw = b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\nConnection: keep-alive\r\nServer: x\r\n\r\nbody"
        assert await codec.read_packet(feed(raw)) == b"body"
        with pytest.raises(ConnectionError, match="Content-Length"):
            await codec.read_packet(feed(b"HTTP/1.1 200 OK\r\n\r\n"))
        with pytest.raises(asyncio.IncompleteReadError):
            await codec.read_packet(feed(b"HTTP/1.1 200"))

    asyncio.run(run())


class FakeWebSocket:
    def __init__(self, frames):
        self.frames, self.sent, self.closed = list(frames), [], False

    async def recv(self, decode=None):
        if not self.frames:
            raise ConnectionClosedOK(Close(1000, ""), None)
        return self.frames.pop(0)

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        self.closed = True


def test_websocket_stream():
    async def run():
        ws = FakeWebSocket([b"abc", b"de", b"fgh"])
        stream = network._WebSocketStream(ws)
        assert await stream.readexactly(4) == b"abcd"  # across frames
        assert await stream.readexactly(2) == b"ef"
        stream.write(b"12")
        stream.write(b"34")
        await stream.drain()
        await stream.drain()  # nothing buffered, nothing sent
        assert ws.sent == [b"1234"]
        with pytest.raises(ConnectionError, match="WebSocket closed"):
            await stream.readexactly(5)
        stream.close()
        await stream.wait_closed()
        assert ws.closed

    asyncio.run(run())


def test_http_posts_do_not_block_each_other():
    """The first POST is held by the server (a long poll); the second must still get its answer."""

    async def run():
        release = asyncio.Event()
        seen = []

        async def handle(reader, writer):
            head = await reader.readuntil(b"\r\n\r\n")
            length = int(head.lower().split(b"content-length: ")[1].split(b"\r\n")[0])
            body = await reader.readexactly(length)
            seen.append(body)
            if body == b"held":
                await release.wait()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body.upper()))
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        conn = network.ConnectionHttp("127.0.0.1", 443, 2, loggers=LOGGERS)
        assert conn._port == 80
        conn._port = port
        await conn.connect(timeout=5)
        await conn.send(b"held")
        await asyncio.sleep(0.1)
        await conn.send(b"quick")
        assert await asyncio.wait_for(conn.recv(), 5) == b"QUICK"
        assert not conn._idle.is_set()
        release.set()
        assert await asyncio.wait_for(conn.recv(), 5) == b"HELD"
        await asyncio.sleep(0.05)
        assert conn._idle.is_set()
        await conn.disconnect()
        server.close()
        assert seen == [b"held", b"quick"]

    asyncio.run(run())


def test_http_long_poll_sends_http_wait_when_idle():
    async def run():
        conn = network.ConnectionHttp("127.0.0.1", 80, 2, loggers=LOGGERS)
        conn._connected = True
        queued = []
        sender = SimpleNamespace(_connection=conn, _send_queue=SimpleNamespace(append=queued.append), up=False)
        sender.is_connected = lambda: sender.up
        task = asyncio.ensure_future(network.http_long_poll(SimpleNamespace(_sender=sender)))
        await asyncio.sleep(0.1)
        assert not queued  # waits for the connection first
        sender.up = True
        await asyncio.sleep(0.2)
        assert len(queued) == 1 and isinstance(queued[0].request, types.HttpWait)
        assert queued[0].request.max_wait == network.HTTP_WAIT_MS
        await asyncio.sleep(0.2)
        assert len(queued) == 1  # still in flight, no second http_wait
        conn._idle.set()  # that POST came back
        await asyncio.sleep(0.2)
        assert len(queued) == 2
        sender.up = False
        conn._idle.set()
        await asyncio.wait_for(task, 2)

    asyncio.run(run())


def test_settings_reach_the_client(tmp_path, monkeypatch):
    from telegram_mcp import client, config

    monkeypatch.setenv("TG_CONNECTION", "WebSocket")
    monkeypatch.setenv("TG_PROXY", "socks5://u:p@127.0.0.1:1080")
    monkeypatch.setenv("TG_SESSION", str(tmp_path / "s"))
    env = tmp_path / ".env"
    env.write_text("TG_API_ID=1\nTG_API_HASH=x\n")
    c = client.make_client(config.load(env))
    assert c._connection is network.ConnectionWebSocket
    assert c._proxy["addr"] == "127.0.0.1" and c._proxy["username"] == "u"
    c.session.close()
