from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any


SUPPORTED_EXTENSIONS = {".mp4", ".mkv", ".webm"}


class MediaError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def inspect_media(path: str) -> dict[str, Any]:
    source = Path(path)
    if not source.is_file():
        raise MediaError("source_missing", "The selected recording no longer exists.")
    if source.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise MediaError("unsupported_format", "Use an MP4, MKV or WebM recording.")
    if not os.access(source, os.R_OK):
        raise MediaError("source_unreadable", "The selected recording cannot be read.")

    command = [
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(source)
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    except FileNotFoundError as exc:
        raise MediaError("ffprobe_missing", "FFprobe is missing. Install FFmpeg and add ffprobe to PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError("inspection_timeout", "FFprobe took too long to inspect this recording.") from exc
    if result.returncode:
        raise MediaError("invalid_media", "FFprobe could not read this recording. It may be damaged or unsupported.")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MediaError("invalid_probe_response", "FFprobe returned invalid metadata.") from exc

    streams = data.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    if video is None:
        raise MediaError("video_track_missing", "This file does not contain a readable video track.")
    audio = [item for item in streams if item.get("codec_type") == "audio"]
    try:
        duration = float(data.get("format", {}).get("duration", 0))
    except (TypeError, ValueError):
        duration = 0.0
    if not duration or duration < 0:
        raise MediaError("duration_missing", "This recording has no readable duration.")

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    rotation = _rotation(video)
    display_width, display_height = width, height
    if abs(rotation) % 180 == 90:
        display_width, display_height = height, width
    return {
        "duration_seconds": duration,
        "has_audio": bool(audio),
        "has_video": True,
        "audio_tracks": len(audio),
        "video_codec": video.get("codec_name"),
        "audio_codec": audio[0].get("codec_name") if audio else None,
        "width": width,
        "height": height,
        "display_width": display_width,
        "display_height": display_height,
        "rotation": rotation,
    }


def _rotation(video: dict[str, Any]) -> int:
    try:
        for side_data in video.get("side_data_list", []) or []:
            if "rotation" in side_data:
                return int(round(float(side_data["rotation"])))
    except (TypeError, ValueError):
        pass
    try:
        return int(round(float(video.get("tags", {}).get("rotate", 0))))
    except (TypeError, ValueError):
        return 0
