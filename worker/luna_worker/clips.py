from __future__ import annotations

import subprocess
from pathlib import Path

from .titles import _clean


def render(source: str, output_dir: str, candidate: dict, title: str, number: int) -> str:
    folder = Path(output_dir)
    if not folder.is_dir():
        raise RuntimeError("The clip output folder is unavailable.")
    start = max(0, int(candidate["start_ms"]))
    end = max(start + 1, int(candidate["end_ms"]))
    duration = (end - start) / 1000
    safe_title = _clean(title)
    base = f"{number:02d} - {safe_title}"
    suffix = 1
    while True:
        filename = f"{base}.mp4" if suffix == 1 else f"{base} ({suffix}).mp4"
        destination = folder / filename
        temporary = folder / f".{base} ({suffix}).partial.mp4"
        if not destination.exists() and not temporary.exists():
            break
        suffix += 1

    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start / 1000:.3f}", "-i", source, "-t", f"{duration:.3f}",
        "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "ultrafast",
        "-crf", "23", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
        "-f", "mp4", str(temporary),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=max(120, duration * 20), check=False)
        if result.returncode:
            raise RuntimeError(f"FFmpeg could not render this clip: {result.stderr[-500:].strip()}")
        temporary.replace(destination)
        return str(destination)
    except RuntimeError:
        temporary.unlink(missing_ok=True)
        raise
    except (OSError, subprocess.TimeoutExpired) as exc:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Clip export failed: {exc}") from exc
