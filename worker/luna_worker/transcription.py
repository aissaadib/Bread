from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from .protocol import log

REACTION_TERMS = (
    "oh my god", "no way", "let's go", "lets go", "what the", "holy", "wow", "insane", "crazy",
    "are you serious", "i can't believe", "that was", "get out",
)
_LOCAL_MODEL_NAMES = ("faster-whisper-base.en", "faster-whisper-small.en", "faster-whisper-base")


def _model_roots() -> list[Path]:
    """Directories that may contain a local `.models` folder.

    Packaged builds place the worker under a different prefix than the source
    checkout, so search both the current working directory and the worker's
    real location, walking up to the filesystem root. `LUNA_MODELS_DIR` points
    directly at a models folder for installed copies.
    """
    roots: list[Path] = []
    explicit = os.environ.get("LUNA_MODELS_DIR", "").strip()
    if explicit:
        roots.append(Path(explicit))
    for start in (Path.cwd(), Path(__file__).resolve().parent):
        current = start
        while True:
            roots.append(current / ".models")
            if current.parent == current:
                break
            current = current.parent
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def _local_models() -> list[Path]:
    return [root / name for root in _model_roots() for name in _LOCAL_MODEL_NAMES]


def _resolve_model_path() -> Path | None:
    explicit = os.environ.get("LUNA_WHISPER_MODEL", "").strip()
    if explicit:
        path = Path(explicit)
        return path if path.is_dir() else None
    for candidate in _local_models():
        if (candidate / "model.bin").is_file():
            return candidate
    return None


def available() -> bool:
    """True when a local Faster-Whisper model can be used without downloading anything."""
    return _resolve_model_path() is not None


def transcribe(path: str, progress: Callable[[str], None]) -> tuple[list[dict], str | None]:
    explicit = os.environ.get("LUNA_WHISPER_MODEL", "").strip()
    model_path = _resolve_model_path()
    if model_path is None:
        if explicit:
            return [], "Transcription skipped: the configured local Whisper model directory was not found."
        return [], "Transcription skipped: set LUNA_WHISPER_MODEL to a local Faster-Whisper model directory."
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return [], "Transcription skipped: install the optional faster-whisper package to enable it."

    model = None
    try:
        progress("Loading the local transcription model")
        model = WhisperModel(str(model_path), device="cpu", compute_type="int8",
                             cpu_threads=max(1, min(4, os.cpu_count() or 1)),
                             num_workers=1, local_files_only=True)
        segments, info = model.transcribe(
            path, beam_size=1, vad_filter=False, word_timestamps=False,
            condition_on_previous_text=False,
        )
        transcript: list[dict] = []
        for index, segment in enumerate(segments, start=1):
            text = segment.text.strip()
            if not text:
                continue
            transcript.append({
                "start_seconds": float(segment.start),
                "end_seconds": float(segment.end),
                "text": text,
                "language": info.language,
            })
            if index % 25 == 0:
                progress(f"Transcribed {index} speech segments")
        return transcript, None
    except Exception as exc:
        log(f"Local transcription failed: {type(exc).__name__}: {exc}")
        return [], f"Transcription skipped: local model processing failed ({type(exc).__name__})."
    finally:
        del model
