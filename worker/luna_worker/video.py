from __future__ import annotations

import statistics
import subprocess
import threading
from collections import deque
from typing import Callable

from . import DEFAULT_CONFIG


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
    frames: list[tuple[float, float]] = []
    previous: bytes | None = None
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
            if previous is not None:
                try:
                    import numpy as np
                    import cv2

                    before = np.frombuffer(previous, dtype=np.uint8).reshape((height, width))
                    current = np.frombuffer(frame, dtype=np.uint8).reshape((height, width))
                    difference = float(cv2.absdiff(before, current).mean()) / 255.0
                except ImportError:
                    difference = sum(abs(a - b) for a, b in zip(previous, frame)) / frame_bytes / 255.0
                frames.append((frame_index * interval_seconds, difference))
            previous = frame
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
    for index, (timestamp, change) in enumerate(frames):
        context = int(settings["baseline_context_frames"])
        left = max(0, index - context)
        right = min(len(frames), index + context + 1)
        baseline = [frames[i][1] for i in range(left, right) if i != index]
        if not baseline:
            continue
        median = statistics.median(baseline)
        mad = statistics.median(abs(value - median) for value in baseline)
        threshold = max(float(settings["minimum_change"]), median + 3.0 * mad)
        if change >= threshold:
            magnitude = min(1.0, 0.25 + (change - threshold) / max(0.2, 1.0 - threshold))
            observations.append({
                "timestamp_ms": int(timestamp * 1000),
                "signal": "visual_activity",
                "magnitude": round(magnitude, 4),
                "metadata": {"frame_difference": round(change, 4), "local_baseline": round(median, 4)},
            })
    return observations
