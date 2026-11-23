"""Local speech-to-text with faster-whisper, plus a cache so a voice message is transcribed once."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

from . import config

_model = None
_model_error: str | None = None
_lock = asyncio.Lock()


def _cache() -> sqlite3.Connection:
    s = config.get()
    s.files_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(s.files_dir / "cache.sqlite")
    db.execute("CREATE TABLE IF NOT EXISTS transcripts (key TEXT PRIMARY KEY, text TEXT NOT NULL)")
    return db


def cached(key: str) -> str | None:
    with _cache() as db:
        row = db.execute("SELECT text FROM transcripts WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _load_model():
    global _model, _model_error
    if _model is None and _model_error is None:
        s = config.get()
        try:
            from faster_whisper import WhisperModel

            # local_files_only: the model is downloaded at install time, so don't ask Hugging Face on every start
            _model = WhisperModel(
                s.whisper_model, device="cpu", compute_type="int8", download_root=str(s.models_dir),
                local_files_only=True,
            )
        except Exception as e:  # missing package, no model and no network, broken download
            _model_error = f"{type(e).__name__}: {e}"
    if _model_error:
        raise RuntimeError(f"Whisper is not available ({_model_error}). Run telegram-mcp-model on the server.")
    return _model


def _run(path: Path) -> str:
    model = _load_model()
    language = config.get().whisper_language
    # The VAD filter skips silence and stops whisper from inventing words there...
    segments, _ = model.transcribe(str(path), language=language, vad_filter=True)
    text = " ".join(seg.text.strip() for seg in segments).strip()
    if text:
        return text
    # ...but it also drops quiet recordings entirely, so try once more without it.
    segments, _ = model.transcribe(str(path), language=language, vad_filter=False, condition_on_previous_text=False)
    text = " ".join(seg.text.strip() for seg in segments).strip()
    return f"(quiet audio, may be inaccurate) {text}" if text else ""


async def transcribe(key: str, path: Path) -> str:
    """Text of an audio/video file. key identifies the message, e.g. "<chat_id>:<msg_id>"."""
    hit = cached(key)
    if hit is not None:
        return hit
    async with _lock:  # one at a time, whisper already uses all CPU cores
        text = await asyncio.to_thread(_run, path)
    with _cache() as db:
        db.execute("INSERT OR REPLACE INTO transcripts (key, text) VALUES (?, ?)", (key, text))
    return text


def download_main() -> None:
    """Download the whisper model from TG_WHISPER_MODEL into models/ (used by install.sh)."""
    s = config.load(sys.argv[1] if len(sys.argv) > 1 else None)
    from faster_whisper import download_model

    print(f"Downloading whisper model '{s.whisper_model}' into {s.models_dir} ...", flush=True)
    path = download_model(s.whisper_model, cache_dir=str(s.models_dir))
    print(f"Whisper model ready: {path}", flush=True)
