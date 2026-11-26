import asyncio
from types import SimpleNamespace

import pytest
from telethon import types

from telegram_mcp import client, config, server

ME = types.User(id=1, is_self=True, first_name="Me")
ALICE = types.User(id=5, username="Alice", first_name="Alice")
BOB = types.User(id=6, first_name="Bob")
WORK = types.Channel(id=100, title="Work", photo=types.ChatPhotoEmpty(), date=None, megagroup=True)
NEWS = types.Channel(id=200, title="News", photo=types.ChatPhotoEmpty(), date=None, username="news_chan")
CODES = types.User(id=777000, first_name="Telegram")
WORK_ID = -1000000000100  # peer id of WORK as tg_list_chats shows it


def load(tmp_path, monkeypatch, allowed: str):
    monkeypatch.setenv("TG_ALLOWED_CHATS", allowed)
    env = tmp_path / ".env"
    env.write_text("TG_API_ID=1\nTG_API_HASH=x\n")
    return config.load(env)


class FakeClient:
    async def get_me(self):
        return ME

    async def get_entity(self, value):
        by_key = {5: ALICE, 6: BOB, WORK_ID: WORK, -1000000000200: NEWS, 777000: CODES, "alice": ALICE, "news_chan": NEWS}
        key = value.lower() if isinstance(value, str) else value
        if key not in by_key:
            raise ValueError(f"no entity {value}")
        return by_key[key]


def dialog(entity, name):
    return SimpleNamespace(
        entity=entity, name=name, unread_count=0, unread_mentions_count=0, pinned=False, archived=False, message=None
    )


@pytest.fixture
def fake_telegram(monkeypatch):
    async def get_client():
        return FakeClient()

    dialogs = [dialog(ME, "Saved Messages"), dialog(ALICE, "Alice"), dialog(BOB, "Bob"),
               dialog(WORK, "Work"), dialog(NEWS, "News"), dialog(CODES, "Telegram")]

    async def get_dialogs(force=False):
        return dialogs

    monkeypatch.setattr(client, "get_client", get_client)
    monkeypatch.setattr(client, "dialogs", get_dialogs)
    monkeypatch.setattr(server, "dialogs", get_dialogs)


def test_parse_allowed_chats(tmp_path, monkeypatch):
    s = load(tmp_path, monkeypatch, f"{WORK_ID}, @Alice  me,https://t.me/news_chan")
    assert s.allowed_chats == {str(WORK_ID), "@alice", "me", "@news_chan"}
    assert load(tmp_path, monkeypatch, "").allowed_chats == frozenset()


@pytest.mark.parametrize("bad", ["Отдел продаж", "@ab", "https://t.me/+invite"])
def test_titles_and_invites_are_rejected(tmp_path, monkeypatch, bad):
    with pytest.raises(ValueError, match="TG_ALLOWED_CHATS"):
        load(tmp_path, monkeypatch, bad)


def test_empty_list_allows_everything(tmp_path, monkeypatch, fake_telegram):
    load(tmp_path, monkeypatch, "")
    assert asyncio.run(client.resolve("Bob")) is BOB
    with pytest.raises(ValueError, match="service chat"):
        asyncio.run(client.resolve("777000"))


def test_resolve_checks_the_list(tmp_path, monkeypatch, fake_telegram):
    load(tmp_path, monkeypatch, f"{WORK_ID} @alice me")
    assert asyncio.run(client.resolve(str(WORK_ID))) is WORK
    assert asyncio.run(client.resolve("5")) is ALICE  # listed by username, opened by id
    assert asyncio.run(client.resolve("me")) is ME
    for ref in ("6", "@news_chan", "https://t.me/news_chan", "-1000000000200"):
        with pytest.raises(ValueError, match="not in TG_ALLOWED_CHATS"):
            asyncio.run(client.resolve(ref))


def test_title_lookup_ignores_chats_outside_the_list(tmp_path, monkeypatch, fake_telegram):
    load(tmp_path, monkeypatch, "@alice")
    with pytest.raises(ValueError, match="No chat matches") as e:
        asyncio.run(client.resolve("News"))
    assert "200" not in str(e.value)  # nothing about the hidden chat leaks into the error
    assert asyncio.run(client.resolve("Ali")) is ALICE


def test_people_inside_an_allowed_chat_skip_the_list(tmp_path, monkeypatch, fake_telegram):
    load(tmp_path, monkeypatch, str(WORK_ID))
    assert asyncio.run(client.resolve("Bob", allowed_only=False)) is BOB
    with pytest.raises(ValueError, match="service chat"):
        asyncio.run(client.resolve("777000", allowed_only=False))


def test_list_chats_shows_only_allowed(tmp_path, monkeypatch, fake_telegram):
    load(tmp_path, monkeypatch, f"{WORK_ID} @news_chan")
    out = asyncio.run(server.tg_list_chats())
    assert "Work" in out and "News" in out
    assert "Alice" not in out and "Bob" not in out and "Saved Messages" not in out

    load(tmp_path, monkeypatch, "")
    out = asyncio.run(server.tg_list_chats())
    assert all(name in out for name in ("Alice", "Bob", "Work", "News", "Saved Messages"))
    assert "777000" not in out
