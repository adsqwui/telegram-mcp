"""Telethon client and turning whatever the AI passes as `chat` into an entity."""

from __future__ import annotations

import asyncio
import platform
import re
import time

from telethon import TelegramClient, errors, functions, types, utils

from . import __version__, config, network

DEVICE_MODEL = "Telegram MCP"
DIALOGS_TTL = 120.0
# Telegram's service account sends login codes. An agent tricked by a message could leak them,
# so this chat is never listed or read.
HIDDEN_CHATS = {777000}

_client: TelegramClient | None = None
_client_lock = asyncio.Lock()
_dialogs: list = []
_dialogs_at = 0.0


class _Client(TelegramClient):
    _long_poll: asyncio.Task | None = None

    async def connect(self):
        if self._connection is network.ConnectionHttp and (self._long_poll is None or self._long_poll.done()):
            self._long_poll = asyncio.ensure_future(network.http_long_poll(self))
        try:
            await super().connect()
        except BaseException:
            if self._long_poll:
                self._long_poll.cancel()
            raise


def make_client(settings: config.Settings) -> TelegramClient:
    """Same device info for login and server, so the session shows up as "Telegram MCP"."""
    settings.session.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return _Client(
        str(settings.session),
        settings.api_id,
        settings.api_hash,
        **network.client_options(settings.connection, settings.proxy),
        device_model=DEVICE_MODEL,
        system_version=f"{platform.system()} {platform.release()}",
        app_version=__version__,
        lang_code="ru",
        system_lang_code="ru",
    )


async def get_client() -> TelegramClient:
    global _client
    async with _client_lock:
        if _client is None or not _client.is_connected():
            s = config.get()
            if not s.api_id or not s.api_hash:
                raise RuntimeError("TG_API_ID and TG_API_HASH are not set. Run telegram-mcp-login.")
            client = make_client(s)
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                raise RuntimeError("The Telegram session is not logged in. Run telegram-mcp-login on the server.")
            _client = client
    return _client


async def dialogs(force: bool = False) -> list:
    """All dialogs, cached for a couple of minutes. Also fills Telethon's entity cache."""
    global _dialogs, _dialogs_at
    if force or not _dialogs or time.monotonic() - _dialogs_at > DIALOGS_TTL:
        client = await get_client()
        _dialogs = await client.get_dialogs(limit=None)
        _dialogs_at = time.monotonic()
    return _dialogs


def kind_of(entity) -> str:
    if isinstance(entity, types.User):
        if entity.is_self:
            return "saved"
        return "bot" if entity.bot else "private"
    if isinstance(entity, types.Chat):
        return "group"
    if isinstance(entity, types.Channel):
        return "group" if (entity.megagroup or entity.gigagroup) else "channel"
    return "unavailable"


def describe_chat(entity) -> str:
    name = "Saved Messages" if getattr(entity, "is_self", False) else utils.get_display_name(entity) or "(no name)"
    username = getattr(entity, "username", None)
    handle = f" @{username}" if username else ""
    return f"{name}{handle} [{kind_of(entity)}, id {utils.get_peer_id(entity)}]"


_LINK = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me)/(.+)$", re.I)
_INVITE = re.compile(r"^(?:\+|joinchat/)([\w-]+)$")
_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


def parse_chat_ref(ref: str) -> tuple[str, str | int]:
    """Classify a chat reference: ("me"|"id"|"invite"|"username"|"title", value)."""
    ref = ref.strip()
    if ref.lower() in {"me", "self", "saved", "избранное"}:
        return "me", "me"
    if re.fullmatch(r"-?\d+", ref):
        return "id", int(ref)
    m = _LINK.match(ref)
    if m:
        rest = m.group(1).split("?")[0].strip("/")
        inv = _INVITE.match(rest)
        if inv:
            return "invite", inv.group(1)
        return "username", rest.split("/")[0]
    if ref.startswith("@"):
        return "username", ref[1:]
    return "title", ref


def is_hidden(entity) -> bool:
    return utils.get_peer_id(entity) in HIDDEN_CHATS


def is_allowed(entity) -> bool:
    """True when TG_ALLOWED_CHATS is empty or lists this chat by id, @username or "me"."""
    allowed = config.get().allowed_chats
    if not allowed:
        return True
    if getattr(entity, "is_self", False) and "me" in allowed:
        return True
    if str(utils.get_peer_id(entity)) in allowed:
        return True
    names = [getattr(entity, "username", None), *(u.username for u in getattr(entity, "usernames", None) or [])]
    return any(n and f"@{n.lower()}" in allowed for n in names)


def is_visible(entity) -> bool:
    return not is_hidden(entity) and is_allowed(entity)


async def resolve(ref: str, *, allowed_only: bool = True):
    """Entity for an id, @username, t.me link (public or invite), "me", or a chat title from the dialog list.

    allowed_only=False skips TG_ALLOWED_CHATS, for refs that name a person inside an allowed chat
    (a sender filter, members to add) rather than a chat to open.
    """
    entity = await _resolve(ref, is_visible if allowed_only else (lambda e: not is_hidden(e)))
    if is_hidden(entity):
        raise ValueError("The Telegram service chat (login codes) is not available through this server.")
    if allowed_only and not is_allowed(entity):
        raise ValueError("This chat is not in TG_ALLOWED_CHATS.")
    return entity


async def _resolve(ref: str, keep):
    client = await get_client()
    kind, value = parse_chat_ref(ref)
    if kind == "me":
        return await client.get_me()
    if kind == "id":
        try:
            return await client.get_entity(value)
        except ValueError:
            await dialogs(force=True)  # the entity may not be in the session cache yet
            return await client.get_entity(value)
    if kind == "invite":
        info = await client(functions.messages.CheckChatInviteRequest(value))
        if isinstance(info, (types.ChatInviteAlready, types.ChatInvitePeek)):
            return info.chat
        raise ValueError(f"You are not a member of the chat behind this invite link ({info.title}).")
    if kind == "username":
        return await client.get_entity(value)

    needle = str(value).casefold()
    visible = [d for d in await dialogs() if keep(d.entity)]
    found = [d.entity for d in visible if (d.name or "").casefold() == needle]
    if not found:
        found = [d.entity for d in visible if needle in (d.name or "").casefold()]
    if len(found) == 1:
        return found[0]
    if not found and _USERNAME.match(str(value)):
        try:
            return await client.get_entity(value)
        except (ValueError, errors.RPCError):
            pass
    if not found:
        raise ValueError(f'No chat matches "{value}". Use tg_list_chats to find the id.')
    options = "\n".join(f"  {describe_chat(e)}" for e in found[:15])
    raise ValueError(f'"{value}" matches {len(found)} chats, pass the id instead:\n{options}')
