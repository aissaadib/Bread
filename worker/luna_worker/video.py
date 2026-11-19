from __future__ import annotations

import statistics
import subprocess
import threading
from collections import deque
from typing import Callable

from . import DEFAULT_CONFIG

try:  # NumPy keeps frame statistics cheap on long recordings but is optional.
    import numpy as np
except Exception:  # pragma: no cover - exercised only in minimal Python installations.
    np = None


def _histogram(frame: bytes) -> list[float]:
    bins = [0] * 16
    for value in frame:
        bins[value >> 4] += 1
    total = len(frame) or 1
    return [count / total for count in bins]


def _frame_difference(previous: bytes, current: bytes) -> float:
    total = 0
    for before, after in zip(previous, current):
        total += abs(before - after)
    return total / max(1, len(current)) / 255.0


def _histogram_distance(previous: list[float], current: list[float]) -> float:
    return 0.5 * sum(abs(before - after) for before, after in zip(previous, current))


def _activity_magnitude(value: float, threshold: float, base: float, span: float, scale: float) -> float:
    if value < threshold:
        return 0.0
    return min(1.0, base + min(span, (value - threshold) / max(0.03, scale) * span))


def analyze(path: str, duration_seconds: float, cancelled: threading.Event,
            progress: Callable[[float], None], config: dict | None = None) -> list[dict]:
    settings = (config or DEFAULT_CONFIG)["video"]
    width = int(settings["width"])
    height = int(settings["height"])
    sample_rate = float(settings["sample_rate_fps"])
    interval_seconds = 1.0 / sample_rate
    frame_bytes = width * height
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", path,
        "-an", "-vf", f"fps={sample_rate},scale={width}:{height}:flags=area", "-pix_fmt", "gray",
        "-f", "rawvideo", "pipe:1",
    ]
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=frame_bytes * 2)
    except FileNotFoundError as exc:
        raise RuntimeError("FFmpeg is missing. Install FFmpeg and add ffmpeg to PATH.") from exc

    def stop_on_cancel() -> None:
        cancelled.wait()
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    threading.Thread(target=stop_on_cancel, daemon=True).start()
    stderr_tail: deque[bytes] = deque(maxlen=30)

    def drain_stderr() -> None:
        assert process.stderr is not None
        for line in iter(process.stderr.readline, b""):
            stderr_tail.append(line)

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()
    differences: list[float] = []
    histograms: list[float] = []
    previous_frame: bytes | None = None
    previous_values = None
    previous_histogram: list[float] | None = None
    frame_index = 0
    next_progress = 55
    try:
        assert process.stdout is not None
        while True:
            if cancelled.is_set():
                process.terminate()
                raise InterruptedError("Analysis cancelled.")
            frame = process.stdout.read(frame_bytes)
            if len(frame) != frame_bytes:
                break
            if np is not None:
                values = np.frombuffer(frame, dtype=np.uint8)
                counts = np.histogram(values, bins=16, range=(0, 256))[0].astype(np.float64)
                histogram = (counts / max(1.0, float(counts.sum()))).tolist()
                if previous_values is not None:
                    difference = float(np.abs(values.astype(np.int16) - previous_values.astype(np.int16)).mean()) / 255.0
                    differences.append(difference)
                    histograms.append(_histogram_distance(previous_histogram or histogram, histogram))
                previous_values = values
                previous_histogram = histogram
            else:
                histogram = _histogram(frame)
                if previous_frame is not None and previous_histogram is not None:
                    differences.append(_frame_difference(previous_frame, frame))
                    histograms.append(_histogram_distance(previous_histogram, histogram))
                previous_frame = frame
                previous_histogram = histogram
            frame_index += 1
            percent = min(90, 50 + int(frame_index * interval_seconds / max(1.0, duration_seconds) * 40))
            if percent >= next_progress:
                progress(percent)
                next_progress = percent + 5
    finally:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        stderr_thread.join(timeout=2)
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()

    if process.returncode not in (0, -15, 255) and not cancelled.is_set():
        detail = b"".join(stderr_tail).decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"FFmpeg video sampling failed: {detail[-500:] or 'unknown error'}")
    if cancelled.is_set():
        raise InterruptedError("Analysis cancelled.")

    observations: list[dict] = []
    context = int(settings["baseline_context_frames"])
    minimum_motion = float(settings["minimum_motion"])
    minimum_histogram = float(settings["minimum_histogram_change"])
    minimum_magnitude = float(settings["minimum_visual_magnitude"])
    for index, (difference, histogram_change) in enumerate(zip(differences, histograms)):
        left = max(0, index - context)
        right = min(len(differences), index + context + 1)
        motion_baseline = [differences[i] for i in range(left, right) if i != index]
        histogram_baseline = [histograms[i] for i in range(left, right) if i != index]
        if not motion_baseline or not histogram_baseline:
            continue
        motion_median = statistics.median(motion_baseline)
        motion_mad = statistics.median(abs(value - motion_median) for value in motion_baseline)
        histogram_median = statistics.median(histogram_baseline)
        histogram_mad = statistics.median(abs(value - histogram_median) for value in histogram_baseline)
        motion_threshold = max(minimum_motion, motion_median + 3.0 * motion_mad)
        histogram_threshold = max(minimum_histogram, histogram_median + 3.0 * histogram_mad)

        motion_magnitude = _activity_magnitude(difference, motion_threshold, 0.50, 0.50, 0.25)
        histogram_magnitude = _activity_magnitude(histogram_change, histogram_threshold, 0.40, 0.60, 0.30)
        magnitude = max(motion_magnitude, histogram_magnitude)
        if magnitude < minimum_magnitude:
            continue
        observations.append({
            "timestamp_ms": int((index + 1) * interval_seconds * 1000),
            "signal": "visual_activity",
            "magnitude": round(magnitude, 4),
            "metadata": {
                "frame_difference": round(difference, 4),
                "motion_baseline": round(motion_median, 4),
                "histogram_change": round(histogram_change, 4),
                "histogram_baseline": round(histogram_median, 4),
            },
        })
    return observations
