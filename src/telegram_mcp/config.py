"""Settings from .env. Call load() once at startup, then use get()."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True)
class Settings:
    root: Path
    env_file: Path
    api_id: int
    api_hash: str
    session: Path
    connection: str
    proxy: str | None
    tz: ZoneInfo
    read_only: bool
    allowed_chats: frozenset[str]
    min_action_gap: float
    files_dir: Path
    allowed_roots: tuple[Path, ...]
    max_inline_bytes: int
    transcribe: bool
    whisper_model: str
    whisper_language: str | None
    transcribe_max_seconds: int
    models_dir: Path
    host: str
    port: int
    bearer_token: str | None


_settings: Settings | None = None


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


_CHAT_USERNAME = re.compile(r"(?:@|(?:https?://)?(?:t\.me|telegram\.me)/)([A-Za-z]\w{3,31})/?", re.I)


def _chat_list(value: str | None) -> frozenset[str]:
    """TG_ALLOWED_CHATS: ids, @usernames, t.me/username links and "me", separated by commas or spaces.
    Titles are not accepted: anyone can create a chat with the same title."""
    chats = set()
    for item in re.split(r"[,\s]+", value or ""):
        if not item:
            continue
        if item.lower() == "me":
            chats.add("me")
        elif re.fullmatch(r"-?\d+", item):
            chats.add(str(int(item)))
        elif m := _CHAT_USERNAME.fullmatch(item):
            chats.add("@" + m.group(1).lower())
        else:
            raise ValueError(f"TG_ALLOWED_CHATS: {item!r} is not a chat id, @username or 'me'.")
    return frozenset(chats)


def _path(root: Path, value: str | None, default: str) -> Path:
    path = Path(value or default).expanduser()
    return (path if path.is_absolute() else root / path).resolve()


def load(env_file: str | Path | None = None) -> Settings:
    global _settings
    if env_file:
        env = Path(env_file).expanduser().resolve()
    else:
        env = Path(os.environ.get("TELEGRAM_MCP_HOME") or PROJECT_ROOT).resolve() / ".env"
    root = env.parent
    load_dotenv(env, override=False)
    e = os.environ.get

    files_dir = _path(root, e("TG_FILES_DIR"), "files")
    roots = [p for p in (e("TG_ALLOWED_ROOTS") or "").split(os.pathsep) if p.strip()]
    allowed = tuple(_path(root, p, p) for p in roots) or (files_dir,)

    _settings = Settings(
        root=root,
        env_file=env,
        api_id=int(e("TG_API_ID") or 0),
        api_hash=e("TG_API_HASH", ""),
        session=_path(root, e("TG_SESSION"), "sessions/mcp"),
        connection=(e("TG_CONNECTION") or "tcp").strip().lower(),
        proxy=(e("TG_PROXY") or "").strip() or None,
        tz=ZoneInfo(e("TG_TIMEZONE") or "Europe/Moscow"),
        read_only=_bool(e("TG_READ_ONLY"), False),
        allowed_chats=_chat_list(e("TG_ALLOWED_CHATS")),
        min_action_gap=float(e("TG_MIN_ACTION_GAP") or 1.0),
        files_dir=files_dir,
        allowed_roots=allowed,
        max_inline_bytes=int(float(e("TG_MAX_INLINE_MB") or 10) * 1024 * 1024),
        transcribe=_bool(e("TG_TRANSCRIBE"), True),
        whisper_model=e("TG_WHISPER_MODEL") or "small",
        whisper_language=(e("TG_WHISPER_LANGUAGE") or "").strip() or None,
        transcribe_max_seconds=int(e("TG_TRANSCRIBE_MAX_SECONDS") or 600),
        models_dir=_path(root, e("TG_MODELS_DIR"), "models"),
        host=e("MCP_HOST") or "127.0.0.1",
        port=int(e("MCP_PORT") or 8765),
        bearer_token=(e("MCP_BEARER_TOKEN") or "").strip() or None,
    )
    return _settings


def get() -> Settings:
    return _settings or load()
