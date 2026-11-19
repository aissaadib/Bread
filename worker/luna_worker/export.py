"""Short-form clip rendering built on the project's FFmpeg integration."""

from __future__ import annotations

import queue
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable

from . import captions, framing
from .titles import _clean

ASPECT_LABELS = {"vertical": "Vertical", "square": "Square", "original": "Original"}
MIN_CLIP_SECONDS = 1.0
MAX_CLIP_SECONDS = 180.0


class ExportError(RuntimeError):
    pass


class ExportCancelled(RuntimeError):
    pass


def _safe_title(title: str) -> str:
    return _clean(title or "Highlight")


def _unique_destination(folder: Path, title: str, number: int, aspect: str) -> Path:
    base = f"{number:02d} - {_safe_title(title)} - {ASPECT_LABELS.get(aspect, 'Short')}"
    suffix = 1
    while True:
        name = f"{base}.mp4" if suffix == 1 else f"{base} ({suffix}).mp4"
        destination = folder / name
        partial = folder / f".{name}.partial.mp4"
        if not destination.exists() and not partial.exists():
            return destination
        suffix += 1


def _friendly_failure(stderr: str) -> str:
    text = stderr.strip()
    lowered = text.lower()
    if "no space left" in lowered or "not enough space" in lowered:
        return "The export failed because the output drive ran out of free space."
    if "invalid data found" in lowered or "error while decoding" in lowered:
        return "The recording could not be decoded at this timestamp. It may be damaged or use an unsupported codec."
    if "unknown encoder" in lowered:
        return "This FFmpeg build is missing the H.264 encoder required for export."
    if "permission denied" in lowered:
        return "The export folder cannot be written to. Choose another folder."
    detail = text[-500:].replace("\r", " ").replace("\n", " ").strip()
    return f"FFmpeg could not render this clip: {detail}" if detail else "FFmpeg could not render this clip."


def _check_disk_space(folder: Path, duration_seconds: float) -> None:
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        return
    # Conservative 1080p veryfast budget plus headroom for the MP4 finalization.
    needed = int(duration_seconds * 3_000_000) + 120_000_000
    if free < needed:
        raise ExportError(
            f"Not enough free disk space in the export folder. "
            f"About {needed / 1_048_576:.0f} MB is required and {free / 1_048_576:.0f} MB is available."
        )


def _validate_range(start_ms: int, end_ms: int, duration_seconds: float) -> tuple[int, int]:
    maximum = max(1, int(round(duration_seconds * 1000)))
    start_ms = min(max(0, int(start_ms)), maximum)
    end_ms = min(max(start_ms + 1, int(end_ms)), maximum)
    seconds = (end_ms - start_ms) / 1000.0
    if seconds < MIN_CLIP_SECONDS - 0.001:
        raise ExportError("Short-form clips must be at least 1 second long.")
    if seconds > MAX_CLIP_SECONDS + 0.001:
        raise ExportError("Short-form clips are limited to 180 seconds.")
    return start_ms, end_ms


def render_clip(source: str, info: dict, output_dir: str, title: str, number: int,
                start_ms: int, end_ms: int, aspect: str, mode: str,
                crop_x: float, crop_y: float, crf: int, captions_enabled: bool,
                transcript: list[dict] | None,
                on_progress: Callable[[float], None],
                cancelled: threading.Event,
                on_started: Callable[[str], None] | None = None,
                on_stage: Callable[[str], None] | None = None) -> str:
    folder = Path(output_dir)
    if not folder.is_dir():
        raise ExportError("The export folder is unavailable.")
    start_ms, end_ms = _validate_range(start_ms, end_ms, float(info["duration_seconds"]))
    duration_seconds = (end_ms - start_ms) / 1000.0
    _check_disk_space(folder, duration_seconds)

    source_width = int(info.get("display_width") or info.get("width") or 0)
    source_height = int(info.get("display_height") or info.get("height") or 0)
    if source_width < 2 or source_height < 2:
        raise ExportError("The recording has no usable video dimensions.")
    layout = framing.compute_layout(source_width, source_height, aspect, mode, crop_x, crop_y)
    destination = _unique_destination(folder, title, number, aspect)
    partial = folder / f".{destination.name}.partial.mp4"
    if on_started:
        on_started(str(destination))

    if cancelled.is_set():
        raise ExportCancelled("Export cancelled.")

    with tempfile.TemporaryDirectory(prefix="luna-captions-") as temporary:
        temporary_path = Path(temporary)
        captions_filename = None
        if captions_enabled:
            cues = captions.build_cues(transcript or [], start_ms, end_ms)
            if cues:
                ass_path = temporary_path / "captions.ass"
                captions.write_ass(cues, layout.canvas_width, layout.canvas_height, ass_path)
                captions_filename = ass_path.name
        graph = framing.build_filter_graph(layout, captions_filename)
        command = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-accurate_seek", "-ss", f"{start_ms / 1000:.3f}", "-i", source,
            "-t", f"{duration_seconds:.3f}",
            "-filter_complex", graph,
            "-map", "[vout]", "-map", "0:a:0?", "-sn", "-dn",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(max(14, min(28, int(crf)))),
            "-profile:v", "high", "-pix_fmt", "yuv420p", "-fps_mode", "passthrough",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            "-map_metadata", "-1", "-metadata:s:v:0", "rotate=0",
            "-progress", "pipe:1", "-nostats",
            "-f", "mp4", str(partial),
        ]
        process = subprocess.Popen(
            command,
            cwd=str(temporary_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        lines: queue.Queue[str | None] = queue.Queue()
        stderr_chunks: list[str] = []

        def read_stdout() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                lines.put(line)
            lines.put(None)

        def read_stderr() -> None:
            assert process.stderr is not None
            for line in process.stderr:
                stderr_chunks.append(line)
                if len(stderr_chunks) > 120:
                    del stderr_chunks[:60]

        stdout_thread = threading.Thread(target=read_stdout, daemon=True)
        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        try:
            on_progress(0.0)
            finalizing_sent = False
            furthest = 0.0
            while True:
                if cancelled.is_set():
                    raise ExportCancelled("Export cancelled.")
                try:
                    line = lines.get(timeout=0.2)
                except queue.Empty:
                    if process.poll() is not None:
                        break
                    continue
                if line is None:
                    break
                key, _, value = line.strip().partition("=")
                if key in ("out_time_us", "out_time_ms"):
                    try:
                        elapsed_us = int(value)
                    except ValueError:
                        continue
                    fraction = min(0.995, max(0.0, elapsed_us / (duration_seconds * 1_000_000)))
                    # FFmpeg progress output is not strictly monotonic around B-frame
                    # reordering; never let the UI move backwards.
                    if fraction > furthest:
                        furthest = fraction
                        on_progress(fraction)
                    if fraction >= 0.97 and not finalizing_sent and on_stage:
                        finalizing_sent = True
                        on_stage("finalizing")
            return_code = process.wait()
            stdout_thread.join(timeout=2)
            stderr_thread.join(timeout=2)
            stderr = "".join(stderr_chunks)
            if cancelled.is_set():
                raise ExportCancelled("Export cancelled.")
            if return_code != 0:
                raise ExportError(_friendly_failure(stderr))
            if not partial.is_file() or partial.stat().st_size == 0:
                raise ExportError("FFmpeg finished without writing a playable video file.")
            on_progress(1.0)
            if on_stage:
                on_stage("finalizing")
            try:
                partial.replace(destination)
            except OSError as exc:
                raise ExportError(f"The rendered clip could not be finalized: {exc}") from exc
            return str(destination)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=4)
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            partial.unlink(missing_ok=True)
