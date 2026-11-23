"""Downloading message media and turning it into MCP content the model can use."""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

from mcp_types import BlobResourceContents, EmbeddedResource, ImageContent, TextContent
from telethon import utils

from . import config, transcribe
from .render import media_kind, media_label, safe_filename

TEXT_LIMIT = 100_000  # characters of a text/PDF file returned inline
TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".html", ".htm", ".log", ".ini",
    ".py", ".js", ".ts", ".sql", ".sh", ".css", ".toml", ".env.example",
}
SPOKEN = {"voice", "video_note", "audio", "video"}
IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def text(value: str) -> TextContent:
    return TextContent(type="text", text=value)


def image(data: bytes, mime: str) -> ImageContent:
    return ImageContent(type="image", data=base64.b64encode(data).decode(), mime_type=mime)


def _mime(msg, path: Path) -> str:
    if media_kind(msg) == "photo":
        return "image/jpeg"
    return (msg.file and msg.file.mime_type) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


async def download(client, msg) -> Path:
    """Save the media under files/downloads/<chat_id>/ once; later calls reuse the file."""
    folder = config.get().files_dir / "downloads" / str(utils.get_peer_id(msg.peer_id))
    folder.mkdir(parents=True, exist_ok=True)
    existing = sorted(folder.glob(f"{msg.id}_*"))
    if existing:
        return existing[0]
    f = msg.file
    name = (f.name if f and f.name else f"{media_kind(msg)}{(f.ext if f else '') or ''}")
    target = folder / f"{msg.id}_{safe_filename(name)}"
    saved = await client.download_media(msg, file=str(target))
    if not saved:
        raise RuntimeError(f"Message #{msg.id} has nothing to download.")
    return Path(saved)


def transcript_key(msg) -> str:
    return f"{utils.get_peer_id(msg.peer_id)}:{msg.id}"


async def voice_transcript(client, msg) -> str:
    return await transcribe.transcribe(transcript_key(msg), await download(client, msg))


async def photo_bytes(client, msg) -> bytes | None:
    """Image bytes for inline preview: the photo itself, or the thumbnail of a video/document."""
    kind = media_kind(msg)
    if kind == "photo":
        return await client.download_media(msg, file=bytes)
    if kind in {"video", "video_note", "gif", "document"} and msg.document and msg.document.thumbs:
        return await client.download_media(msg, file=bytes, thumb=-1)
    return None


def _extract_text(path: Path, mime: str) -> str | None:
    suffix = path.suffix.lower()
    if mime.startswith("text/") or suffix in TEXT_EXTENSIONS or mime in {"application/json", "application/xml"}:
        return path.read_text(encoding="utf-8", errors="replace")
    if mime == "application/pdf" or suffix == ".pdf":
        from pypdf import PdfReader

        pages = PdfReader(path).pages
        return "\n\n".join(f"--- page {i} ---\n{p.extract_text() or ''}" for i, p in enumerate(pages, 1))
    return None


async def full_content(client, msg, transcribe_audio: bool = True, embed_file: bool = False) -> list:
    """Everything useful about one media message: saved path, image, transcript, file text, raw bytes."""
    s = config.get()
    kind = media_kind(msg)
    path = await download(client, msg)
    mime = _mime(msg, path)
    file_size = path.stat().st_size
    out = [text(f"#{msg.id} [{media_label(msg)}]\nsaved on the server: {path}\nmime: {mime}")]

    if kind == "photo" or (mime in IMAGE_MIMES and kind != "video"):
        if file_size <= s.max_inline_bytes:
            out.append(image(path.read_bytes(), mime))
        else:
            out.append(text(f"The image is {file_size} bytes, too big to return inline."))
    elif kind in {"video", "video_note", "gif"}:
        thumb = await photo_bytes(client, msg)
        if thumb:
            out += [text("Video thumbnail:"), image(thumb, "image/jpeg")]

    if kind in SPOKEN and transcribe_audio and s.transcribe:
        try:
            out.append(text(f"transcript: {await voice_transcript(client, msg) or '(no speech)'}"))
        except RuntimeError as e:
            out.append(text(f"No transcript: {e}"))

    if kind == "document":
        try:
            extracted = _extract_text(path, mime)
        except Exception as e:  # broken PDF or encoding: the file itself is still there
            extracted = None
            out.append(text(f"Could not read the text of the file: {type(e).__name__}: {e}"))
        if extracted is not None:
            cut = len(extracted) > TEXT_LIMIT
            out.append(text(f"file text{' (cut to 100k characters)' if cut else ''}:\n{extracted[:TEXT_LIMIT]}"))

    if embed_file:
        if file_size <= s.max_inline_bytes:
            blob = base64.b64encode(path.read_bytes()).decode()
            out.append(EmbeddedResource(type="resource", resource=BlobResourceContents(
                uri=path.as_uri(), mime_type=mime, blob=blob)))
        else:
            out.append(text(f"The file is {file_size} bytes, over TG_MAX_INLINE_MB, so it is not embedded."))
    return out
