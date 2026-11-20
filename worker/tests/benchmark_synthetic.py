from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "worker"))
from bread_worker.pipeline import analyze

DURATION_SECONDS = 3 * 60 * 60
KNOWN_EVENT_SECONDS = 90 * 60
ARTIFACTS = ROOT / ".test-output"


def build_recording(destination: Path) -> None:
    video = f"color=c=black:s=160x90:r=1:d={DURATION_SECONDS}"
    video_filter = "drawbox=x=0:y=0:w=160:h=90:color=white:t=fill:enable='between(t,5400,5402)'"
    audio = f"sine=frequency=440:sample_rate=16000:duration={DURATION_SECONDS}"
    audio_filter = "volume='if(between(t,5400,5401),30,1)':eval=frame"
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", video, "-f", "lavfi", "-i", audio,
        "-vf", video_filter, "-af", audio_filter,
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage", "-crf", "35",
        "-c:a", "aac", "-b:a", "48k", "-shortest", str(destination),
    ], check=True, env=os.environ.copy())


def measure_analysis(source: Path) -> tuple[list[dict], float, float]:
    events: list[dict] = []
    finished = threading.Event()
    peak_rss = [0]
    process = psutil.Process(os.getpid()) if psutil else None

    def sample_memory() -> None:
        if process is None:
            return
        while not finished.is_set():
            try:
                current = process.memory_info().rss
                for child in process.children(recursive=True):
                    try:
                        current += child.memory_info().rss
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                peak_rss[0] = max(peak_rss[0], current)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            finished.wait(0.05)

    sampler = threading.Thread(target=sample_memory, daemon=True) if process else None
    if sampler:
        sampler.start()
    started = time.perf_counter()
    try:
        analyze(str(source), threading.Event(), lambda kind, **data: events.append({"type": kind, **data}))
    finally:
        wall_seconds = time.perf_counter() - started
        finished.set()
        if sampler:
            sampler.join(timeout=2)
    return events, wall_seconds, peak_rss[0] / (1024 * 1024) if process else float("nan")


def main() -> int:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    source = ARTIFACTS / "three-hour-synthetic.mp4"
    try:
        build_recording(source)
        events, wall_seconds, peak_rss_mb = measure_analysis(source)
        candidates = [event["candidate"] for event in events if event["type"] == "candidate"]
        if not candidates:
            print(json.dumps({"error": "No event candidate was returned", "wall_seconds": wall_seconds}, indent=2))
            return 1
        event = min(candidates, key=lambda item: abs(item["peak_ms"] - KNOWN_EVENT_SECONDS * 1000))
        report = {
            "recording_duration_seconds": DURATION_SECONDS,
            "input_size_bytes": source.stat().st_size,
            "known_event_seconds": KNOWN_EVENT_SECONDS,
            "detected_peak_ms": event["peak_ms"],
            "peak_error_ms": abs(event["peak_ms"] - KNOWN_EVENT_SECONDS * 1000),
            "candidate_count": len(candidates),
            "signals": event["signals"],
            "analysis_wall_seconds": round(wall_seconds, 3),
            "real_time_throughput_x": round(DURATION_SECONDS / wall_seconds, 2),
            "peak_rss_mb_python_plus_children": round(peak_rss_mb, 1) if peak_rss_mb == peak_rss_mb else None,
        }
        print(json.dumps(report, indent=2))
        return 0 if report["peak_error_ms"] <= 2_000 else 1
    finally:
        shutil.rmtree(ARTIFACTS, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
