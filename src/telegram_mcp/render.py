"""Dates in and messages out, as compact text an AI can read."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from telethon import types, utils

_DAY_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y")
_MOMENT_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d.%m.%Y %H:%M")


def parse_day(value: str, tz: ZoneInfo) -> date:
    v = value.strip().lower()
    today = datetime.now(tz).date()
    if v in {"today", "сегодня"}:
        return today
    if v in {"yesterday", "вчера"}:
        return today - timedelta(days=1)
    for fmt in _DAY_FORMATS:
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            pass
    raise ValueError(f'Cannot read the date "{value}". Use today, yesterday, YYYY-MM-DD or DD.MM.YYYY.')


def day_range(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


def parse_moment(value: str, tz: ZoneInfo, end_of_day: bool = False) -> datetime:
    """A day (start of it, or the end with end_of_day) or a day with a time, in tz."""
    for fmt in _MOMENT_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=tz)
        except ValueError:
            pass
    start, end = day_range(parse_day(value, tz), tz)
    return end if end_of_day else start


def local(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def size(n: int | None) -> str:
    if not n:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def duration(seconds: float | None) -> str:
    s = int(seconds or 0)
    return f"{s // 60}:{s % 60:02d}"


def media_kind(msg) -> str | None:
    if msg.media is None or isinstance(msg.media, types.MessageMediaWebPage):
        return None
    for kind in ("photo", "voice", "video_note", "sticker", "gif", "audio", "video", "document", "contact", "poll"):
        if getattr(msg, kind, None):
            return kind
    if msg.geo or msg.venue:
        return "location"
    return type(msg.media).__name__.removeprefix("MessageMedia").lower()


def media_label(msg) -> str | None:
    kind = media_kind(msg)
    if kind is None:
        return None
    f = msg.file
    if kind in {"voice", "video_note"}:
        return f"{kind.replace('_', ' ')} {duration(f.duration if f else 0)}"
    if kind in {"audio", "video"} and f:
        title = f" {f.name}" if f.name else ""
        return f"{kind}{title} {duration(f.duration)}, {size(f.size)}"
    if kind == "document" and f:
        return f"file {f.name or '(no name)'} ({size(f.size)}, {f.mime_type})"
    if kind == "sticker" and f:
        return f"sticker {f.emoji or ''}".strip()
    if kind == "contact":
        c = msg.contact
        return f"contact {c.first_name} {c.last_name or ''} {c.phone_number or ''}".strip()
    if kind == "poll":
        p = msg.poll.poll
        question = getattr(p.question, "text", p.question)
        answers = " / ".join(getattr(a.text, "text", a.text) for a in p.answers)
        return f"poll: {question} ({answers})"
    if kind == "location":
        g = msg.geo
        return f"location {g.lat:.5f},{g.long:.5f}" if g else "location"
    return kind


def downloadable(msg) -> bool:
    return media_kind(msg) in {"photo", "voice", "video_note", "sticker", "gif", "audio", "video", "document"}


def sender_label(msg) -> str:
    if msg.out:
        return "me"
    s = msg.sender
    if s is None:
        return msg.post_author or "unknown"
    name = utils.get_display_name(s) or "deleted account"
    username = getattr(s, "username", None)
    parts = [name]
    if username:
        parts.append(f"@{username}")
    if isinstance(s, types.User):
        parts.append(f"id {s.id}")
    if msg.post_author:
        parts.append(f"signed {msg.post_author}")
    return " · ".join(parts)


def _forward_label(fwd) -> str:
    if fwd.sender:
        return utils.get_display_name(fwd.sender)
    if fwd.chat:
        return utils.get_display_name(fwd.chat)
    return fwd.from_name or "hidden user"


def button_rows(msg) -> list[list]:
    markup = msg.reply_markup
    if isinstance(markup, (types.ReplyInlineMarkup, types.ReplyKeyboardMarkup)):
        return [row.buttons for row in markup.rows]
    return []


def _button_note(button) -> str:
    t = button.type
    if isinstance(t, types.InlineButtonTypeCallback):
        return " (asks for 2FA password, blocked)" if t.requires_password else ""
    if isinstance(t, types.ButtonTypeDefault):
        return ""
    if isinstance(t, types.InlineButtonTypeUrl):
        return f" -> {t.url}"
    if isinstance(t, (types.InlineButtonTypeWebView, types.ButtonTypeSimpleWebView)):
        return f" [mini app: {t.url}]"
    if isinstance(t, types.InlineButtonTypeCopy):
        return f" [copies: {t.copy_text}]"
    return f" [{type(t).__name__}, not clickable here]"


def _buttons(msg) -> list[str]:
    rows = button_rows(msg)
    if not rows:
        return []
    inline = isinstance(msg.reply_markup, types.ReplyInlineMarkup)
    lines = ["buttons (tg_click):" if inline else "reply keyboard (tg_click sends the text):"]
    for r, row in enumerate(rows):
        for c, b in enumerate(row):
            lines.append(f"  [{r},{c}] {b.text}{_button_note(b)}")
    return lines


def render(msg, tz: ZoneInfo, transcript: str | None = None) -> str:
    if isinstance(msg, types.MessageService):
        action = type(msg.action).__name__.removeprefix("MessageAction")
        return f"#{msg.id} · {local(msg.date, tz)} · {sender_label(msg)} · [service: {action}]"
    head = [f"#{msg.id}", local(msg.date, tz), sender_label(msg)]
    if msg.edit_date:
        head.append("edited")
    lines = [" · ".join(head)]
    if msg.reply_to and msg.reply_to.reply_to_msg_id:
        lines.append(f"  reply to #{msg.reply_to.reply_to_msg_id}")
    if msg.fwd_from:
        lines.append(f"  forwarded from {_forward_label(msg.forward)}")
    label = media_label(msg)
    if label:
        lines.append(f"  [{label}]")
    if transcript is not None:
        lines.append(f"  transcript: {transcript or '(no speech)'}")
    if msg.message:
        lines += ["  " + line for line in msg.message.splitlines()] or [""]
    lines += ["  " + b for b in _buttons(msg)]
    return "\n".join(lines)


def safe_filename(name: str) -> str:
    name = re.sub(r"[^\w.\- ()\[\]]+", "_", name).strip(" .")
    return name[:120] or "file"

