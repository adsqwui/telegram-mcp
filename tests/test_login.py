import json
import socket
import urllib.error
import urllib.request

import pytest

from telegram_mcp import login


def test_qr_page_defaults_to_loopback(monkeypatch):
    monkeypatch.delenv("TG_LOGIN_HOST", raising=False)
    monkeypatch.setattr(login, "_headless", lambda: True)  # a VPS used to get 0.0.0.0
    assert login._login_host() == "127.0.0.1"
    monkeypatch.setenv("TG_LOGIN_HOST", " ")
    assert login._login_host() == "127.0.0.1"
    monkeypatch.setenv("TG_LOGIN_HOST", "0.0.0.0")
    assert login._login_host() == "0.0.0.0"


def test_qr_page_listens_on_loopback_only():
    page = login.QrPage(login._login_host(), 0)
    try:
        assert page.server.server_address[0] == "127.0.0.1"
        base = f"http://127.0.0.1:{page.port}"
        page.show("tg://login?token=abc")
        state = json.load(urllib.request.urlopen(f"{base}/{page.token}/state"))
        assert state == {"status": "waiting", "version": 1, "name": None}
        assert "<svg" in urllib.request.urlopen(f"{base}/{page.token}/qr.svg").read().decode()
        with pytest.raises(urllib.error.HTTPError, match="404"):
            urllib.request.urlopen(f"{base}/wrong-token/state")

    finally:
        page.close()


def reachable(ip: str, port: int) -> bool:
    try:
        urllib.request.urlopen(f"http://{ip}:{port}/", timeout=2)
    except urllib.error.HTTPError as e:
        return e.code == 404 and e.headers.get("Server", "").startswith("BaseHTTP")  # our page, not a VPN
    except OSError:
        return False
    return False


def test_qr_page_is_not_reachable_from_the_network():
    ip = next((a for a in _local_ips() if not a.startswith("127.")), None)
    if ip is None:
        pytest.skip("no non-loopback address")
    exposed = login.QrPage("0.0.0.0", 0)
    try:
        assert reachable(ip, exposed.port)  # the check works: an all-interfaces page is seen on this ip
    finally:
        exposed.close()
    page = login.QrPage(login._login_host(), 0)
    try:
        assert not reachable(ip, page.port)
    finally:
        page.close()


def _local_ips() -> list[str]:
    try:
        return socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        return []
