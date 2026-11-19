"""Optional cloud semantic assessment worker (Gemini).

Launched by the Rust host *after* the local analysis completes. It never
replaces local analysis: it reviews a bounded set of already-detected windows,
plus a few sampled ones, and reports semantic evidence back as JSONL events.

The API key is read from the first stdin line (JSON), so it is never part of a
command line, an environment block or a file on disk. Each uploaded segment is
deleted from Gemini before the worker moves on, and all temporary files live in
a directory that is removed on every exit path.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from typing import Any

from luna_worker import ai, media
from luna_worker.gemini import (
    GeminiClient,
    GeminiError,
    QuotaExceededError,
    RateLimitedError,
)

_lock = threading.Lock()

# The Rust host parses worker events as UTF-8. Windows otherwise encodes piped
# stdout with the active code page, which corrupts JSON once a title, reason or
# path contains a non-ASCII character.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


class Cancelled(Exception):
    """Raised when the host asks the optional cloud step to stop."""


def send(event_type: str, **data: Any) -> None:
    message = {"type": event_type, **data}
    # ASCII escapes keep the protocol encoding-independent.
    line = json.dumps(message, separators=(",", ":"), ensure_ascii=True) + "\n"
    with _lock:
        sys.stdout.write(line)
        sys.stdout.flush()


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _read_key(stream, timeout_seconds: float = 20.0) -> str:
    """Block until the host sends the API key as the first control line."""
    holder: dict[str, Any] = {}
    ready = threading.Event()

    def reader() -> None:
        try:
            line = stream.readline()
        except (OSError, ValueError):
            line = ""
        if line:
            try:
                payload = json.loads(line)
                holder["key"] = str(payload.get("apiKey") or "")
            except (json.JSONDecodeError, AttributeError):
                holder["key"] = ""
        holder["eof"] = True
        ready.set()

    threading.Thread(target=reader, daemon=True).start()
    if not ready.wait(timeout_seconds):
        return ""
    return (holder.get("key") or "").strip()


def _start_cancel_listener(stream, cancelled: threading.Event) -> None:
    def reader() -> None:
        for line in stream:
            try:
                command = json.loads(line)
            except json.JSONDecodeError:
                continue
            if command.get("command") == "cancel":
                cancelled.set()
                return
        cancelled.set()

    threading.Thread(target=reader, daemon=True).start()


def _even(value: float) -> int:
    return max(2, int(round(value / 2.0)) * 2)


def extract_segment(source: str, info: dict[str, Any], window: dict[str, Any],
                    destination: Path, max_width: int) -> Path:
    """Cut one short, low-bitrate segment. The original is never modified."""
    start_ms = int(window["start_ms"])
    end_ms = int(window["end_ms"])
    duration = max(0.5, (end_ms - start_ms) / 1000.0)
    width = int(info.get("display_width") or info.get("width") or 0)
    height = int(info.get("display_height") or info.get("height") or 0)
    if width < 2 or height < 2:
        raise media.MediaError("video_track_missing", "The recording has no usable video dimensions.")
    target_width = min(max(240, int(max_width)), width if width > 0 else max_width)
    target_height = _even(target_width * height / width)
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-accurate_seek", "-ss", f"{start_ms / 1000:.3f}", "-i", source,
        "-t", f"{duration:.3f}",
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", f"scale={_even(target_width)}:{target_height}:flags=fast_bilinear",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k", "-ac", "2", "-ar", "44100",
        "-movflags", "+faststart", "-f", "mp4", str(destination),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
    if result.returncode != 0 or not destination.is_file() or destination.stat().st_size == 0:
        detail = (result.stderr or "").strip()[-300:]
        raise media.MediaError(
            "segment_failed",
            f"The analysis segment could not be cut. {detail}".strip(),
        )
    return destination


def assess_window(client: GeminiClient, source: str, info: dict[str, Any],
                  window: dict[str, Any], temporary: Path, options: dict[str, Any],
                  cancelled: threading.Event, index: int, total: int) -> dict[str, Any]:
    """One request: cut -> upload -> ask -> validate -> delete remote -> clean up."""
    if cancelled.is_set():
        raise Cancelled()
    segment = temporary / f"window-{index:02d}.mp4"
    remote_name = ""
    try:
        send("ai_progress", stage="extracting", progress=_progress(index, total, 0.0),
             window=index, total=total)
        extract_segment(source, info, window, segment,
                        int(options["segment_max_width"]))
        if cancelled.is_set():
            raise Cancelled()

        send("ai_progress", stage="uploading", progress=_progress(index, total, 0.35),
             window=index, total=total)
        file_info = client.upload_file(str(segment), "video/mp4",
                                       f"luna-window-{index:02d}.mp4")
        remote_name = file_info.get("name", "")
        file_info = client.wait_until_active(file_info)
        file_uri = file_info.get("uri") or remote_name
        if not file_uri:
            raise GeminiError("Gemini did not return a usable file reference.",
                              code="upload_failed")

        if cancelled.is_set():
            raise Cancelled()
        send("ai_progress", stage="assessing", progress=_progress(index, total, 0.65),
             window=index, total=total)
        raw = client.assess(file_uri, "video/mp4", ai.build_prompt(window))
        assessment = ai.parse_assessment(raw, window)
        assessment["model"] = client.model
        combined, label = ai.fuse(int(window.get("local_score", 0)), assessment, options)
        item = {
            "candidateId": window.get("candidate_id"),
            "windowStartMs": int(window["start_ms"]),
            "windowEndMs": int(window["end_ms"]),
            "origin": window.get("origin", "candidate"),
            "localScore": int(window.get("local_score", 0)),
            "localSignals": window.get("local_signals", []),
            "isHighlight": assessment["is_highlight"],
            "eventType": assessment["event_type"],
            "interestScore": assessment["interest_score"],
            "peakMs": assessment["peak_ms"],
            "reason": assessment["reason"],
            "aiTitle": ai.build_title(assessment),
            "focusX": assessment["focus_x"],
            "focusY": assessment["focus_y"],
            "focusConfidence": assessment["focus_confidence"],
            "mostlyMenu": assessment["mostly_menu"],
            "visuallyClear": assessment["visually_clear"],
            "combinedScore": combined,
            "combinedLabel": label,
            "model": assessment["model"],
            "segmentOffsetSeconds": assessment["segment_relative_offset_seconds"],
            "evidence": ai.summarize_evidence(assessment),
        }
        return item
    finally:
        if remote_name:
            send("ai_progress", stage="cleaning", progress=_progress(index, total, 0.95),
                 window=index, total=total)
            if client.delete_file(remote_name):
                send("ai_progress", stage="cleaned", progress=_progress(index, total, 1.0),
                     window=index, total=total)
        segment.unlink(missing_ok=True)


def _progress(index: int, total: int, fraction: float) -> int:
    if total <= 0:
        return 0
    return int(max(0.0, min(100.0, ((index + fraction) / total) * 100.0)))


def run(request: dict[str, Any], api_key: str, cancelled: threading.Event,
        client_factory=None) -> int:
    options = ai.settings({"ai": request.get("ai") or {}})
    job_id = str(request.get("jobId") or "")
    model = str(request.get("model") or options["model"])

    if not request.get("enabled", True):
        send("error", jobId=job_id, code="cloud_disabled",
             message="Cloud AI analysis is disabled for this analysis.")
        return 1
    if not api_key:
        send("error", jobId=job_id, code="missing_key",
             message="Add your own Gemini API key in Cloud AI settings to use cloud analysis. "
                     "Local analysis is unaffected.")
        return 1

    source = str(request.get("source") or "")
    duration_ms = int(request.get("durationMs") or 0)
    windows = ai.plan_windows(request.get("candidates") or [], duration_ms, options)
    if not windows:
        send("ai_complete", jobId=job_id, assessed=0, failed=0, rejected=0, total=0,
             model=model, requests=0, message="No candidate windows were available for review.")
        return 0

    try:
        info = media.inspect_media(source)
    except media.MediaError as error:
        send("error", jobId=job_id, code=error.code, message=str(error))
        return 1

    factory = client_factory or GeminiClient
    try:
        client = factory(api_key, model,
                         timeout_seconds=float(options["request_timeout_seconds"]),
                         max_retries=int(options["max_retries"]))
    except GeminiError as error:
        send("error", jobId=job_id, code=error.code, message=str(error))
        return 1

    send("ai_started", jobId=job_id, total=len(windows), model=client.model)
    assessed = 0
    failed = 0
    rejected = 0

    with tempfile.TemporaryDirectory(prefix="luna-ai-") as temporary_name:
        temporary = Path(temporary_name)
        for index, window in enumerate(windows):
            if cancelled.is_set():
                send("ai_complete", jobId=job_id, assessed=assessed, failed=failed,
                     rejected=rejected, total=len(windows), model=client.model,
                     requests=getattr(client, "requests_made", 0),
                     cancelled=True,
                     message="Cloud analysis was stopped. Local results are unchanged.")
                return 2
            try:
                item = assess_window(client, source, info, window, temporary, options,
                                     cancelled, index, len(windows))
                assessed += 1
                send("ai_item", jobId=job_id, item=item)
            except Cancelled:
                send("ai_complete", jobId=job_id, assessed=assessed, failed=failed,
                     rejected=rejected, total=len(windows), model=client.model,
                     requests=getattr(client, "requests_made", 0), cancelled=True,
                     message="Cloud analysis was stopped. Local results are unchanged.")
                return 2
            except QuotaExceededError as error:
                send("ai_complete", jobId=job_id, assessed=assessed, failed=failed,
                     rejected=rejected, total=len(windows), model=client.model,
                     requests=getattr(client, "requests_made", 0), rateLimited=True,
                     message=str(error))
                return 1
            except RateLimitedError as error:
                send("ai_complete", jobId=job_id, assessed=assessed, failed=failed,
                     rejected=rejected, total=len(windows), model=client.model,
                     requests=getattr(client, "requests_made", 0), rateLimited=True,
                     message=str(error))
                return 1
            except GeminiError as error:
                failed += 1
                send("ai_item", jobId=job_id, item={
                    "candidateId": window.get("candidate_id"),
                    "windowStartMs": int(window["start_ms"]),
                    "windowEndMs": int(window["end_ms"]),
                    "rejected": True, "code": error.code, "message": str(error),
                })
            except ai.AssessmentError as error:
                rejected += 1
                send("ai_item", jobId=job_id, item={
                    "candidateId": window.get("candidate_id"),
                    "windowStartMs": int(window["start_ms"]),
                    "windowEndMs": int(window["end_ms"]),
                    "rejected": True, "code": error.code, "message": str(error),
                })
            except (media.MediaError, OSError) as error:
                failed += 1
                send("ai_item", jobId=job_id, item={
                    "candidateId": window.get("candidate_id"),
                    "windowStartMs": int(window["start_ms"]),
                    "windowEndMs": int(window["end_ms"]),
                    "rejected": True, "code": "segment_failed", "message": str(error),
                })
            except Exception as error:  # Defensive: an optional step must not crash the app.
                log(f"Unexpected cloud analysis failure: {traceback.format_exc()}")
                failed += 1
                send("ai_item", jobId=job_id, item={
                    "candidateId": window.get("candidate_id"),
                    "windowStartMs": int(window["start_ms"]),
                    "windowEndMs": int(window["end_ms"]),
                    "rejected": True, "code": "unexpected_error",
                    "message": f"This window could not be assessed ({type(error).__name__}).",
                })

    send("ai_complete", jobId=job_id, assessed=assessed, failed=failed, rejected=rejected,
         total=len(windows), model=client.model,
         requests=getattr(client, "requests_made", 0),
         message=None if assessed else "Gemini returned no usable assessments.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Luna optional cloud analysis worker")
    parser.add_argument("--request", help="Path to the cloud analysis request JSON")
    parser.add_argument("--test-connection", action="store_true",
                        help="Only verify that the stored key can reach the model")
    args = parser.parse_args(argv)

    cancelled = threading.Event()
    api_key = _read_key(sys.stdin)
    _start_cancel_listener(sys.stdin, cancelled)

    if args.test_connection:
        model = os.environ.get("LUNA_AI_MODEL")
        try:
            client = GeminiClient(api_key, model)
        except GeminiError as error:
            send("test", ok=False, code=error.code, message=str(error))
            return 1
        try:
            result = client.test_connection()
        except GeminiError as error:
            send("test", ok=False, code=error.code, message=str(error))
            return 1
        send("test", ok=True, **result)
        return 0

    if not args.request:
        parser.error("--request is required unless --test-connection is used")

    request_path = Path(args.request)
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        send("error", jobId="", code="bad_request",
             message=f"The cloud analysis request could not be read: {error}")
        return 1
    finally:
        request_path.unlink(missing_ok=True)

    try:
        return run(request, api_key, cancelled)
    except Exception as error:  # Never leave the host without a terminal event.
        log(f"Cloud analysis worker crashed: {traceback.format_exc()}")
        send("error", jobId=str(request.get("jobId") or ""), code="worker_crashed",
             message=f"Cloud analysis stopped unexpectedly ({type(error).__name__}).")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
