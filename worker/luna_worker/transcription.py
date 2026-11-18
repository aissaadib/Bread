from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

REACTION_TERMS = (
    "oh my god", "no way", "let's go", "lets go", "what the", "holy", "wow", "insane", "crazy",
    "are you serious", "i can't believe", "that was", "get out",
)


def transcribe(path: str, progress: Callable[[str], None]) -> tuple[list[dict], str | None]:
    model_path = os.environ.get("LUNA_WHISPER_MODEL", "").strip()
    if not model_path:
        return [], "Transcription skipped: set LUNA_WHISPER_MODEL to a local Faster-Whisper model directory."
    if not Path(model_path).is_dir():
        return [], "Transcription skipped: the configured local Whisper model directory was not found."
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return [], "Transcription skipped: install the optional faster-whisper package to enable it."

    try:
        progress("Loading the local transcription model")
        model = WhisperModel(model_path, device="cpu", compute_type="int8", cpu_threads=2,
                             num_workers=1, local_files_only=True)
        segments, info = model.transcribe(path, beam_size=1, vad_filter=False, word_timestamps=False)
        transcript = [
            {"start_seconds": float(segment.start), "end_seconds": float(segment.end),
             "text": segment.text.strip(), "language": info.language}
            for segment in segments
        ]
        del model
        return transcript, None
    except Exception as exc:
        return [], f"Transcription skipped: local model processing failed ({type(exc).__name__})."
