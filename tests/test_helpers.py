import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from telegram_mcp import config
from telegram_mcp.client import parse_chat_ref
from telegram_mcp.render import day_range, parse_day, parse_moment, safe_filename

MSK = ZoneInfo("Europe/Moscow")


@pytest.mark.parametrize(
    "ref, expected",
    [
        ("me", ("me", "me")),
        ("Избранное", ("me", "me")),
        ("-1001234567890", ("id", -1001234567890)),
        ("12345", ("id", 12345)),
        ("@durov", ("username", "durov")),
        ("https://t.me/durov", ("username", "durov")),
        ("t.me/some_channel/123", ("username", "some_channel")),
        ("https://t.me/+AbCd_123", ("invite", "AbCd_123")),
        ("https://t.me/joinchat/AbCd", ("invite", "AbCd")),
        ("t.me/bot?start=ref", ("username", "bot")),
        ("Отдел продаж", ("title", "Отдел продаж")),
    ],
)
def test_parse_chat_ref(ref, expected):
    assert parse_chat_ref(ref) == expected


def test_parse_day_forms():
    today = datetime.now(MSK).date()
    assert parse_day("today", MSK) == today
    assert parse_day("сегодня", MSK) == today
    assert (today - parse_day("вчера", MSK)).days == 1
    assert parse_day("2026-11-24", MSK) == date(2026, 11, 24)
    assert parse_day("24.11.2026", MSK) == date(2026, 11, 24)
    with pytest.raises(ValueError):
        parse_day("next week", MSK)


def test_day_range_is_local_midnight_to_midnight():
    start, end = day_range(date(2026, 11, 24), MSK)
    assert start.isoformat() == "2026-11-24T00:00:00+03:00"
    assert (end - start).total_seconds() == 86400


def test_parse_moment():
    assert parse_moment("2026-11-24 14:30", MSK).isoformat() == "2026-11-24T14:30:00+03:00"
    assert parse_moment("2026-11-24", MSK).hour == 0
    assert parse_moment("2026-11-24", MSK, end_of_day=True).isoformat() == "2026-11-25T00:00:00+03:00"


def test_safe_filename():
    assert safe_filename("../../etc/passwd") == "_.._etc_passwd"
    assert safe_filename("отчёт 2026.pdf") == "отчёт 2026.pdf"
    assert safe_filename("") == "file"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for key in ("TG_ALLOWED_ROOTS", "TG_FILES_DIR", "TG_SESSION", "MCP_BEARER_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text("TG_API_ID=1\nTG_API_HASH=x\n")
    return config.load(env)


def test_send_file_only_from_allowed_roots(settings, tmp_path):
    from telegram_mcp.server import _check_local_file

    inside = settings.files_dir / "a.txt"
    inside.parent.mkdir(parents=True)
    inside.write_text("ok")
    assert _check_local_file(str(inside)) == inside

    outside = tmp_path / "secret.txt"
    outside.write_text("no")
    with pytest.raises(ValueError, match="Only files inside"):
        _check_local_file(str(outside))
    with pytest.raises(ValueError, match="credentials"):
        _check_local_file(str(settings.env_file))

    link = settings.files_dir / "link.txt"
    link.symlink_to(outside)  # a symlink inside the folder must not leak a file outside it
    with pytest.raises(ValueError, match="Only files inside"):
        _check_local_file(str(link))


def test_bearer_auth():
    from telegram_mcp.server import BearerAuth

    calls = []

    async def app(scope, receive, send):
        calls.append(scope["type"])

    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request"}

    guarded = BearerAuth(app, "secret")
    scope = {"type": "http", "headers": [(b"authorization", b"Bearer wrong")]}
    asyncio.run(guarded(scope, receive, send))
    assert sent[0]["status"] == 401 and not calls

    scope = {"type": "http", "headers": [(b"authorization", b"Bearer secret")]}
    asyncio.run(guarded(scope, receive, send))
    assert calls == ["http"]

    asyncio.run(guarded({"type": "lifespan"}, receive, send))
    assert calls == ["http", "lifespan"]
