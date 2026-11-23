"""Telegram login by QR code. Run it yourself in a terminal: `uv run telegram-mcp-login`.

1. Asks for api_id and api_hash once and saves them to .env.
2. Shows a QR code on a small web page (the link is printed) and in the terminal.
   Scan it in Telegram on the phone: Settings -> Devices -> Link Desktop Device.
3. Asks for the 2FA password if the account has one.

The session shows up in Telegram as "Telegram MCP". The session file gives full access
to the account: keep it private and never copy it anywhere else.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import secrets
import socket
import sys
import threading
import webbrowser
from getpass import getpass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import qrcode
import qrcode.image.svg
from dotenv import set_key
from telethon import errors

from . import config
from .client import make_client

PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Telegram MCP: вход</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; min-height:100vh; display:grid; place-items:center; background:#0e1116;
         font:15px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif; color:#d7dde5; }
  main { width:min(360px, 92vw); text-align:center; }
  h1 { font-size:20px; font-weight:600; margin:0 0 6px; color:#fff; }
  p { margin:0; color:#8b96a5; }
  .qr { margin:24px auto; width:280px; height:280px; padding:16px; background:#fff; border-radius:16px;
        transition:opacity .2s ease; }
  .qr svg { width:100%; height:100%; display:block; }
  .state { margin-top:8px; font-weight:500; color:#d7dde5; }
  .ok .qr { opacity:.15; } .ok .state { color:#5fd38d; }
  ol { text-align:left; padding-left:20px; color:#8b96a5; margin:18px 0 0; }
</style></head>
<body><main id="root">
  <h1>Вход в Telegram MCP</h1>
  <p>QR обновляется сам, страницу перезагружать не нужно.</p>
  <div class="qr" id="qr"></div>
  <div class="state" id="state">Ждём сканирования…</div>
  <ol><li>Откройте Telegram на телефоне</li>
      <li>Настройки → Устройства → Подключить устройство</li>
      <li>Наведите камеру на код</li></ol>
</main>
<script>
let version = -1;
const text = {waiting:"Ждём сканирования…", password:"Код принят. Введите пароль 2FA в терминале.",
              done:"Готово, можно закрыть страницу.", error:"Ошибка, смотрите терминал."};
async function tick() {
  try {
    const s = await (await fetch("state", {cache:"no-store"})).json();
    if (s.version !== version) {
      version = s.version;
      document.getElementById("qr").innerHTML = await (await fetch("qr.svg?v=" + version)).text();
    }
    document.getElementById("state").textContent = s.name ? text.done + " Аккаунт: " + s.name : text[s.status];
    document.getElementById("root").className = s.status === "done" ? "ok" : "";
    if (s.status === "done" || s.status === "error") return;
  } catch (e) {}
  setTimeout(tick, 1500);
}
tick();
</script></body></html>"""


class QrPage:
    """A tiny web server with the current QR code at http://host:port/<random token>/."""

    def __init__(self, host: str, port: int):
        self.token = secrets.token_urlsafe(12)
        self.state = {"status": "waiting", "version": 0, "name": None}
        self.svg = ""
        page = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                prefix = f"/{page.token}/"
                if not self.path.startswith(prefix):
                    return self.send_error(404)
                route = self.path[len(prefix):].split("?")[0]
                body, ctype = {
                    "": (PAGE, "text/html; charset=utf-8"),
                    "qr.svg": (page.svg, "image/svg+xml"),
                    "state": (json.dumps(page.state), "application/json"),
                }.get(route, (None, None))
                if body is None:
                    return self.send_error(404)
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def show(self, url: str) -> None:
        img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, border=1)
        buf = io.BytesIO()
        img.save(buf)
        self.svg = buf.getvalue().decode()
        self.state["version"] += 1

    def close(self) -> None:
        self.server.shutdown()


def _headless() -> bool:
    return sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _primary_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))  # no packet is sent, this only picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return "SERVER_IP"


def _print_terminal_qr(url: str) -> None:
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    if sys.stdout.isatty():
        qr.print_ascii(tty=True)
    else:
        qr.print_ascii(invert=True)


def ask(prompt: str, check, error: str, hidden: bool = False) -> str:
    while True:
        value = (getpass(prompt) if hidden else input(prompt)).strip()
        if check(value):
            return value
        print(error)


def ensure_api_keys(s: config.Settings) -> config.Settings:
    if s.api_id and s.api_hash:
        print("api_id и api_hash уже есть в .env.")
        return s
    print("Возьмите api_id и api_hash на https://my.telegram.org → API development tools.\n")
    api_id = ask("api_id: ", str.isdigit, "api_id состоит только из цифр.")
    api_hash = ask(
        "api_hash (ввод скрыт): ",
        lambda v: re.fullmatch(r"[0-9a-fA-F]{32}", v) is not None,
        "api_hash это 32 символа из цифр и букв a-f.",
        hidden=True,
    )
    s.env_file.touch(mode=0o600, exist_ok=True)
    set_key(s.env_file, "TG_API_ID", api_id, quote_mode="never")
    set_key(s.env_file, "TG_API_HASH", api_hash, quote_mode="never")
    os.chmod(s.env_file, 0o600)
    os.environ["TG_API_ID"], os.environ["TG_API_HASH"] = api_id, api_hash
    print(f"Сохранено в {s.env_file}\n")
    return config.load(s.env_file)


async def _password(client, page: QrPage) -> None:
    page.state["status"] = "password"
    print("\nНа аккаунте включена двухэтапная проверка.")
    while True:
        password = await asyncio.to_thread(getpass, "Пароль 2FA (ввод скрыт): ")
        try:
            await client.sign_in(password=password)
            return
        except errors.PasswordHashInvalidError:
            print("Неверный пароль, попробуйте ещё раз.")


async def qr_login(s: config.Settings, force: bool) -> None:
    client = make_client(s)
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            if not force:
                print(f"Уже вошли как {me.first_name} (id {me.id}). Для повторного входа: --force")
                return
            await client.log_out()  # also removes the old session from the account's device list
            await client.disconnect()
            client = make_client(s)
            await client.connect()

        headless = _headless()
        host = os.environ.get("TG_LOGIN_HOST") or ("0.0.0.0" if headless else "127.0.0.1")
        try:
            page = QrPage(host, int(os.environ.get("TG_LOGIN_PORT") or 8766))
        except OSError:  # port busy: take any free one
            page = QrPage(host, 0)
        shown = _primary_ip() if host == "0.0.0.0" else "127.0.0.1"
        link = f"http://{shown}:{page.port}/{page.token}/"

        print("\nОткройте страницу с QR-кодом:")
        print(f"  {link}")
        if headless:
            print("Если порт закрыт файрволом, сделайте туннель со своего компьютера:")
            print(f"  ssh -L {page.port}:127.0.0.1:{page.port} user@{shown}")
            print(f"  и откройте http://127.0.0.1:{page.port}/{page.token}/")
        else:
            webbrowser.open(link)
        print("Или сканируйте код прямо из терминала: Telegram → Настройки → Устройства → Подключить устройство.\n")

        qr = await client.qr_login()
        try:
            while True:
                page.show(qr.url)
                _print_terminal_qr(qr.url)
                print("Код действует 30 секунд и обновляется сам.\n")
                try:
                    await qr.wait()
                    break
                except asyncio.TimeoutError:
                    await qr.recreate()
                except errors.SessionPasswordNeededError:
                    await _password(client, page)
                    break
        except BaseException:
            page.state["status"] = "error"
            raise
        me = await client.get_me()
        page.state.update(status="done", name=me.first_name)
        await asyncio.sleep(3)  # let the page show "done"
        page.close()
        os.chmod(f"{s.session}.session", 0o600)
        print(f"Готово, вошли как {me.first_name} (id {me.id}). Сессия: {s.session}.session")
        print('В Telegram она видна в списке устройств как "Telegram MCP".')
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram MCP login")
    parser.add_argument("--env-file")
    parser.add_argument("--keys-only", action="store_true", help="only ask for api_id and api_hash")
    parser.add_argument("--force", action="store_true", help="log out the current session and log in again")
    args = parser.parse_args()
    try:
        s = ensure_api_keys(config.load(args.env_file))
        if not args.keys_only:
            asyncio.run(qr_login(s, args.force))
    except KeyboardInterrupt:
        print("\nОтменено.")
        sys.exit(130)


if __name__ == "__main__":
    main()
