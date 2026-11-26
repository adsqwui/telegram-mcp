"""MCP server: a Telegram user account for an AI agent (Telethon, MTProto)."""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import functools
import hmac
import time
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from telethon import errors, functions, types, utils

from . import __version__, config, media, network
from .client import describe_chat, dialogs, get_client, is_visible, kind_of, resolve
from .render import (
    button_rows,
    day_range,
    downloadable,
    local,
    media_kind,
    media_label,
    parse_day,
    parse_moment,
    render,
    safe_filename,
)

MAX_READ = 500
MAX_INLINE_IMAGES = 10
MAX_MEDIA_PER_CALL = 10
POLL_INTERVAL = 0.7  # how often the chat is re-read while waiting for a reply
SETTLE_SECONDS = 1.5  # the reply counts as finished after this long without changes
WATCH_LAST = 30
MAX_WAIT = 60.0

mcp = MCPServer(
    "telegram",
    version=__version__,
    instructions=(
        "A Telegram user account. `chat` accepts a numeric id from tg_list_chats (most reliable), "
        "@username, a t.me link, 'me' for Saved Messages, or the exact chat title. "
        "Read with tg_read_messages (date='today' or a date for one day, before_id to page back through "
        "the whole history). Voice messages come transcribed; tg_get_media returns images, transcripts and "
        "file contents. Messages you send go out from the user's real account: send only what the task asks for. "
        "Never press buttons that pay, confirm an order or top up a balance."
    ),
)

_last_write = 0.0


def tool(title: str, write: bool = False):
    """Register a tool that returns plain text/content. Expected failures reach the model as a
    readable isError result instead of a generic crash message."""

    def decorate(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            if write and config.get().read_only:
                raise ToolError("Refused: the server runs with TG_READ_ONLY=true, writing is disabled.")
            try:
                return await fn(*args, **kwargs)
            except errors.FloodWaitError as e:
                raise ToolError(f"Telegram FloodWait: wait {e.seconds} s before the next request of this kind.")
            except (errors.RPCError, ValueError, RuntimeError, OSError) as e:
                raise ToolError(f"{type(e).__name__}: {e}") from e

        annotations = ToolAnnotations(title=title, read_only_hint=not write, destructive_hint=False, open_world_hint=True)
        return mcp.tool(annotations=annotations, structured_output=False)(wrapper)

    return decorate


async def _throttle() -> None:
    global _last_write
    delay = _last_write + config.get().min_action_gap - time.monotonic()
    if delay > 0:
        await asyncio.sleep(delay)
    _last_write = time.monotonic()


def _fingerprint(msg) -> tuple:
    return (msg.message, msg.edit_date, str(msg.reply_markup), getattr(msg.media, "photo", None) and msg.photo.id)


async def _act_and_wait(client, entity, action, wait: float):
    """Run a write action. With wait > 0, also collect incoming messages that appeared or changed
    until the chat is quiet for SETTLE_SECONDS (handy for bots)."""
    before = {}
    if wait > 0:
        before = {m.id: _fingerprint(m) for m in await client.get_messages(entity, limit=WATCH_LAST)}
    await _throttle()
    result = await action()
    if wait <= 0:
        return result, []

    deadline = time.monotonic() + min(wait, MAX_WAIT)
    seen: dict = {}
    latest: dict = {}
    last_change = None
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_INTERVAL)
        msgs = await client.get_messages(entity, limit=WATCH_LAST)
        changed = {m.id: m for m in msgs if not m.out and before.get(m.id) != _fingerprint(m)}
        current = {i: _fingerprint(m) for i, m in changed.items()}
        if current != seen:
            seen, latest, last_change = current, changed, time.monotonic()
        elif last_change and time.monotonic() - last_change >= SETTLE_SECONDS:
            break
    return result, sorted(latest.values(), key=lambda m: m.id)


def _replies(changed: list, wait: float) -> str:
    if wait <= 0:
        return ""
    if not changed:
        return f"\nNo new or edited incoming messages within {wait:g} s."
    tz = config.get().tz
    return f"\n{len(changed)} new or edited incoming message(s):\n\n" + "\n\n".join(render(m, tz) for m in changed)


# ---------------------------------------------------------------- reading


@tool("Account status")
async def tg_status() -> str:
    """Which account the server uses and how it is configured. Call it first if anything fails."""
    s = config.get()
    client = await get_client()
    me = await client.get_me()
    name = utils.get_display_name(me)
    lines = [
        f"account: {name}" + (f" @{me.username}" if me.username else "") + f" (id {me.id})",
        f"read only: {s.read_only}",
        "allowed chats: " + (", ".join(sorted(s.allowed_chats)) if s.allowed_chats else "all (TG_ALLOWED_CHATS is empty)"),
        f"connection: {network.describe(s.connection, s.proxy)}",
        f"timezone: {s.tz.key}",
        f"transcription: {'on, whisper ' + s.whisper_model if s.transcribe else 'off'}",
        f"files are saved to: {s.files_dir}",
        "tg_send_file accepts local files from: " + ", ".join(str(p) for p in s.allowed_roots),
    ]
    return "\n".join(lines)


@tool("List chats")
async def tg_list_chats(
    kind: Literal["all", "private", "group", "channel", "bot"] = "all",
    unread_only: bool = False,
    query: str | None = None,
    limit: int = 50,
) -> str:
    """List dialogs in Telegram's order (pinned first, then newest activity): id, title, type, unread count,
    last message.

    kind filters by type ("private" includes Saved Messages). query matches the title or @username.
    """
    s = config.get()
    needle = (query or "").strip().lstrip("@").casefold()
    rows, total = [], 0
    for d in await dialogs(force=True):
        if not is_visible(d.entity):
            continue
        k = kind_of(d.entity)
        if kind != "all" and k != kind and not (kind == "private" and k == "saved"):
            continue
        if unread_only and not d.unread_count:
            continue
        username = (getattr(d.entity, "username", None) or "").casefold()
        if needle and needle not in (d.name or "").casefold() and needle != username:
            continue
        total += 1
        if len(rows) >= max(1, min(limit, 500)):
            continue
        line = describe_chat(d.entity)
        if d.unread_count:
            line += f" · unread {d.unread_count}"
        if d.unread_mentions_count:
            line += f" (mentions {d.unread_mentions_count})"
        if d.pinned:
            line += " · pinned"
        if d.archived:
            line += " · archived"
        m = d.message
        if m:
            preview = (m.message or "").replace("\n", " ")[:90] or f"[{media_label(m) or 'service'}]"
            line += f"\n    {local(m.date, s.tz)} {'me' if m.out else ''}: {preview}".replace(" :", ":")
        rows.append(line)
    if not rows:
        return "No chats match."
    tail = f"\n\nShowing {len(rows)} of {total}. Raise limit or narrow with query." if total > len(rows) else ""
    return "\n".join(rows) + tail


@tool("Chat info")
async def tg_chat_info(chat: str) -> str:
    """Details of a chat, group, channel, user or bot: description, members, your rights, invite link, bot commands."""
    client = await get_client()
    entity = await resolve(chat)
    lines = [describe_chat(entity)]
    if isinstance(entity, types.User):
        full = (await client(functions.users.GetFullUserRequest(entity))).full_user
        if entity.phone:
            lines.append(f"phone: +{entity.phone}")
        if full.about:
            lines.append(f"about: {full.about}")
        info = full.bot_info
        if info and info.description:
            lines.append(f"bot description: {info.description}")
        if info and info.commands:
            lines.append("bot commands: " + ", ".join(f"/{c.command} ({c.description})" for c in info.commands))
    elif isinstance(entity, types.Channel):
        full = (await client(functions.channels.GetFullChannelRequest(entity))).full_chat
        lines.append(f"members: {full.participants_count}")
        if full.about:
            lines.append(f"about: {full.about}")
        role = "owner" if entity.creator else "admin" if entity.admin_rights else "member"
        lines.append(f"your role: {role}")
        if full.exported_invite:
            lines.append(f"invite link: {full.exported_invite.link}")
        if entity.username:
            lines.append(f"public link: https://t.me/{entity.username}")
    elif isinstance(entity, types.Chat):
        full = (await client(functions.messages.GetFullChatRequest(entity.id))).full_chat
        lines.append(f"members: {entity.participants_count}")
        if full.about:
            lines.append(f"about: {full.about}")
        if full.exported_invite:
            lines.append(f"invite link: {full.exported_invite.link}")
    return "\n".join(lines)


@tool("Read messages")
async def tg_read_messages(
    chat: str,
    date: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
    before_id: int | None = None,
    after_id: int | None = None,
    search: str | None = None,
    from_user: str | None = None,
    transcribe_voice: bool = True,
    include_images: bool = False,
    mark_read: bool = False,
) -> list:
    """Read messages of any chat, group or channel, oldest first.

    date: one day, "today", "yesterday", "2026-11-24" or "24.11.2026" (in TG_TIMEZONE).
    since / until: a range instead of date, a day or "2026-11-24 14:00".
    limit: up to 500 per call. To read the whole history, repeat the call with before_id set to the
    smallest id from the previous page until no more pages are reported. after_id returns only newer messages.
    search: only messages containing this text. from_user: only messages from this person.
    transcribe_voice: voice and round video messages come with a text transcript.
    include_images: photos are attached as images (at most 10 per call).
    mark_read: mark the chat as read up to the newest returned message (refused with TG_READ_ONLY=true).
    """
    s = config.get()
    if mark_read and s.read_only:
        raise ToolError("Refused: mark_read changes the account and the server runs with TG_READ_ONLY=true.")
    client = await get_client()
    entity = await resolve(chat)
    lo = hi = None
    if date:
        lo, hi = day_range(parse_day(date, s.tz), s.tz)
    if since:
        lo = parse_moment(since, s.tz)
    if until:
        hi = parse_moment(until, s.tz, end_of_day=True)
    limit = max(1, min(limit, MAX_READ))
    sender = await resolve(from_user, allowed_only=False) if from_user else None

    msgs, more = [], False
    async for m in client.iter_messages(
        entity, offset_date=hi, offset_id=before_id or 0, min_id=after_id or 0, search=search or None, from_user=sender
    ):
        if lo and m.date < lo:
            break
        if len(msgs) == limit:
            more = True
            break
        msgs.append(m)
    msgs.reverse()

    transcripts, notes = {}, []
    if transcribe_voice and s.transcribe:
        for m in msgs:
            if media_kind(m) not in {"voice", "video_note"}:
                continue
            if (m.file.duration or 0) > s.transcribe_max_seconds:
                notes.append(f"#{m.id} is longer than {s.transcribe_max_seconds} s, get it with tg_get_media.")
                continue
            try:
                transcripts[m.id] = await media.voice_transcript(client, m)
            except RuntimeError as e:
                notes.append(str(e))
                break

    images = []
    if include_images:
        for m in msgs:
            if media_kind(m) == "photo" and len(images) < MAX_INLINE_IMAGES:
                images.append((m.id, await client.download_media(m, file=bytes)))

    if lo or hi:
        span = f" from {local(lo, s.tz) if lo else 'the start'} to {local(hi, s.tz) if hi else 'now'}"
    else:
        span = ""
    head = f"{describe_chat(entity)} · {len(msgs)} message(s){span}"
    if not msgs:
        return [media.text(head + "\nNo messages found.")]
    parts = [head, *(render(m, s.tz, transcripts.get(m.id)) for m in msgs)]
    if more:
        parts.append(f"More messages exist. Next (older) page: the same call with before_id={msgs[0].id}.")
    with_media = [m.id for m in msgs if media_kind(m) and m.id not in transcripts and m.id not in dict(images)]
    if with_media:
        ids = ", ".join(map(str, with_media[:30]))
        parts.append(f"Messages with media: {ids}. tg_get_media returns images, transcripts and file contents.")
    parts += notes
    if mark_read:
        await client.send_read_acknowledge(entity, max_id=msgs[-1].id)
        parts.append("Marked as read.")

    out = [media.text("\n\n".join(parts))]
    for msg_id, data in images:
        out += [media.text(f"photo from #{msg_id}:"), media.image(data, "image/jpeg")]
    return out


@tool("Get media")
async def tg_get_media(chat: str, message_ids: list[int], transcribe: bool = True, embed_file: bool = False) -> list:
    """Download the media of up to 10 messages and return it in a usable form.

    Photos and images come back as images, video as a thumbnail plus transcript, voice/audio as a
    transcript, text and PDF files as their text. Every file is saved on the server and its path is
    returned (it can be sent again with tg_send_file). embed_file also attaches the raw file bytes.
    """
    client = await get_client()
    entity = await resolve(chat)
    ids = list(dict.fromkeys(message_ids))[:MAX_MEDIA_PER_CALL]
    out, missing = [], []
    for msg_id, msg in zip(ids, await client.get_messages(entity, ids=ids)):
        if msg is None:
            missing.append(msg_id)
        elif not downloadable(msg):
            out.append(media.text(f"#{msg.id} has no file ({media_label(msg) or 'text only'})."))
        else:
            out += await media.full_content(client, msg, transcribe_audio=transcribe, embed_file=embed_file)
    if missing:
        out.append(media.text(f"Not found in this chat: {', '.join(map(str, sorted(missing)))}"))
    return out or [media.text("Nothing to return.")]


# ---------------------------------------------------------------- writing

_PARSE_MODES = {"markdown": "md", "html": "html", "plain": None}


@tool("Send message", write=True)
async def tg_send_message(
    chat: str,
    text: str,
    reply_to: int | None = None,
    format: Literal["markdown", "html", "plain"] = "markdown",
    silent: bool = False,
    wait_reply: float = 0,
) -> str:
    """Send a text message to a private chat, group, channel (you must be allowed to post) or bot.

    format "markdown": **bold**, __italic__, `code`, [text](https://link). reply_to: message id to reply to.
    silent: no notification sound. wait_reply: seconds to wait and return incoming replies (useful for bots).
    """
    client = await get_client()
    entity = await resolve(chat)

    def action():
        return client.send_message(entity, text, parse_mode=_PARSE_MODES[format], reply_to=reply_to, silent=silent)

    sent, changed = await _act_and_wait(client, entity, action, wait_reply)
    return f"Sent #{sent.id} to {describe_chat(entity)}." + _replies(changed, wait_reply)


def _check_local_file(raw: str) -> Path:
    s = config.get()
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"File not found on the server: {path}")
    forbidden = (s.session.parent.resolve(), s.env_file)
    if any(path == f or f in path.parents for f in forbidden):
        raise ValueError("This file belongs to the server's credentials and is never sent.")
    if not any(root == path or root in path.parents for root in s.allowed_roots):
        roots = ", ".join(str(r) for r in s.allowed_roots)
        raise ValueError(f"Only files inside {roots} can be sent (TG_ALLOWED_ROOTS).")
    return path


@tool("Send file", write=True)
async def tg_send_file(
    chat: str,
    path: str | None = None,
    url: str | None = None,
    content_base64: str | None = None,
    filename: str | None = None,
    caption: str = "",
    send_as: Literal["auto", "photo", "document", "voice", "video_note"] = "auto",
    reply_to: int | None = None,
    silent: bool = False,
    wait_reply: float = 0,
) -> str:
    """Send a photo, voice message, video or any file. Give exactly one source:

    path: a file on the server (from tg_get_media or inside TG_ALLOWED_ROOTS);
    url: a public http(s) link that Telegram downloads itself;
    content_base64 + filename: the file bytes, for clients that are not on the server.
    send_as: auto picks by type, document keeps the original file, voice needs .ogg (opus) audio.
    caption supports the same markdown as tg_send_message.
    """
    if sum(x is not None for x in (path, url, content_base64)) != 1:
        raise ValueError("Pass exactly one of path, url or content_base64.")
    s = config.get()
    if path:
        source = str(_check_local_file(path))
    elif url:
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        source = url
    else:
        if not filename:
            raise ValueError("filename is required with content_base64, e.g. report.pdf")
        try:
            data = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("content_base64 is not valid base64.") from None
        outbox = s.files_dir / "outbox" / str(time.time_ns())  # own folder keeps the original file name
        outbox.mkdir(parents=True)
        target = outbox / safe_filename(filename)
        target.write_bytes(data)
        source = str(target)

    client = await get_client()
    entity = await resolve(chat)

    def action():
        return client.send_file(
            entity, source, caption=caption or None, parse_mode="md", reply_to=reply_to, silent=silent,
            force_document=send_as == "document", voice_note=send_as == "voice", video_note=send_as == "video_note",
        )

    sent, changed = await _act_and_wait(client, entity, action, wait_reply)
    sent = sent[0] if isinstance(sent, list) else sent
    return f"Sent #{sent.id} [{media_label(sent) or 'file'}] to {describe_chat(entity)}." + _replies(changed, wait_reply)


@tool("Press a button", write=True)
async def tg_click(chat: str, message_id: int, row: int, col: int, wait: float = 8.0) -> str:
    """Press an inline or keyboard button on a message (usually a bot's) and return what changed.

    row and col come from the "[row,col]" list under the message. Link and mini app buttons are not
    opened, their URL is returned instead.
    """
    client = await get_client()
    entity = await resolve(chat)
    msg = await client.get_messages(entity, ids=message_id)
    if msg is None:
        return f"Message #{message_id} not found in this chat."
    rows = button_rows(msg)
    if not rows:
        return f"Message #{message_id} has no buttons."
    try:
        button = rows[row][col]
    except IndexError:
        return f"Message #{message_id} has no button [{row},{col}]."

    t = button.type
    if isinstance(t, types.InlineButtonTypeCallback):
        if t.requires_password:
            return "This button asks for the account's 2FA password. Not pressing it."

        async def action():
            try:
                return await client(
                    functions.messages.GetBotCallbackAnswerRequest(peer=entity, msg_id=msg.id, data=t.data)
                )
            except errors.BotResponseTimeoutError:
                return None

    elif isinstance(t, types.ButtonTypeDefault):
        def action():
            return client.send_message(entity, button.text, parse_mode=None)

    elif isinstance(t, (types.InlineButtonTypeUrl, types.InlineButtonTypeWebView, types.ButtonTypeSimpleWebView)):
        return f"Button [{row},{col}] is a link, not opened: {t.url}"
    else:
        return (
            f"Button [{row},{col}] is {type(t).__name__}. Buttons like this share your phone or location, "
            "pay, or need inline mode, so they are not pressed."
        )

    answer, changed = await _act_and_wait(client, entity, action, max(wait, 1.0))
    note = ""
    if isinstance(answer, types.messages.BotCallbackAnswer) and (answer.message or answer.url):
        note = f"Bot answered the click ({'alert' if answer.alert else 'toast'}): {answer.message or ''}"
        if answer.url:
            note += f" url: {answer.url}"
    return (note or "Pressed.") + _replies(changed, max(wait, 1.0))


async def _invite(client, entity, members: list[str]) -> list[str]:
    """Add users to a group or channel; returns one line per user."""
    report = []
    for ref in members:
        try:
            user = await resolve(ref, allowed_only=False)
            if not isinstance(user, types.User):
                report.append(f"  {ref}: not a user, skipped")
                continue
            if user.is_self:
                report.append(f"  {ref}: this is your own account, it is already in the chat")
                continue
            await _throttle()
            if isinstance(entity, types.Chat):
                result = await client(functions.messages.AddChatUserRequest(entity.id, user, fwd_limit=100))
            else:
                result = await client(functions.channels.InviteToChannelRequest(entity, [user]))
            if getattr(result, "missing_invitees", None):
                report.append(f"  {ref}: privacy settings do not allow adding, send them the invite link")
            else:
                report.append(f"  {ref}: added")
        except (errors.RPCError, ValueError) as e:
            report.append(f"  {ref}: not added ({type(e).__name__}: {e})")
    return report


async def _create(title: str, about: str, megagroup: bool, members: list[str] | None, username: str | None) -> str:
    client = await get_client()
    await _throttle()
    result = await client(
        functions.channels.CreateChannelRequest(title=title, about=about, megagroup=megagroup, broadcast=not megagroup)
    )
    entity = result.chats[0]
    lines = [f"Created {describe_chat(entity)}."]
    if username:
        try:
            await client(functions.channels.UpdateUsernameRequest(entity, username.lstrip("@")))
            lines.append(f"public link: https://t.me/{username.lstrip('@')}")
        except errors.RPCError as e:
            lines.append(f"Could not set the public username: {type(e).__name__}: {e}")
    # The chat's primary link, the same one tg_chat_info shows; create a new one only if there is none
    invite = (await client(functions.channels.GetFullChannelRequest(entity))).full_chat.exported_invite
    if invite is None:
        invite = await client(functions.messages.ExportChatInviteRequest(entity))
    lines.append(f"invite link: {invite.link}")
    if members:
        lines += ["members:", *await _invite(client, entity, members)]
    await dialogs(force=True)
    return "\n".join(lines)


@tool("Create group", write=True)
async def tg_create_group(title: str, members: list[str] | None = None, about: str = "") -> str:
    """Create a group (supergroup) owned by this account. members: ids or @usernames to add right away.
    Returns the new chat id and an invite link."""
    return await _create(title, about, megagroup=True, members=members, username=None)


@tool("Create channel", write=True)
async def tg_create_channel(
    title: str, about: str = "", username: str | None = None, members: list[str] | None = None
) -> str:
    """Create a channel owned by this account. username makes it public (t.me/username), otherwise it is
    private. members: ids or @usernames to add as subscribers. Returns the new chat id and an invite link."""
    return await _create(title, about, megagroup=False, members=members, username=username)


@tool("Add members", write=True)
async def tg_add_members(chat: str, members: list[str]) -> str:
    """Add people to a group or channel where this account may invite. members: ids or @usernames."""
    client = await get_client()
    entity = await resolve(chat)
    if kind_of(entity) not in {"group", "channel"}:
        raise ValueError(f"{describe_chat(entity)} is not a group or channel.")
    return "\n".join([f"Adding to {describe_chat(entity)}:", *await _invite(client, entity, members)])


# ---------------------------------------------------------------- transports


class BearerAuth:
    """Rejects HTTP requests without the right "Authorization: Bearer <token>" header."""

    def __init__(self, app, token: str | None):
        self.app, self.expected = app, (f"Bearer {token}".encode() if token else None)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.expected:
            supplied = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(supplied, self.expected):
                from starlette.responses import JSONResponse

                return await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
        return await self.app(scope, receive, send)


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--env-file", help="path to .env (default: the project's .env)")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    s = config.load(args.env_file)

    if args.transport == "stdio":
        mcp.run("stdio")
        return

    import uvicorn

    host, port = args.host or s.host, args.port or s.port
    if host not in config.LOOPBACK_HOSTS and not s.bearer_token:
        raise SystemExit("MCP_BEARER_TOKEN must be set when the server listens on a non-loopback address.")
    app = mcp.streamable_http_app(json_response=True, stateless_http=True, host=host)
    uvicorn.run(BearerAuth(app, s.bearer_token), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
