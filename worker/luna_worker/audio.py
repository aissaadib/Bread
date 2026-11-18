from __future__ import annotations

import math
import statistics
import subprocess
import threading
from array import array
from collections import deque
from typing import Callable

from .protocol import log
from . import DEFAULT_CONFIG

def _read_exact(stream, size: int) -> bytes:
    parts = bytearray()
    while len(parts) < size:
        piece = stream.read(size - len(parts))
        if not piece:
            break
        parts.extend(piece)
    return bytes(parts)


def _window_features(raw: bytes) -> tuple[float, float, float]:
    samples = array("h")
    samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
    if not samples:
        return -120.0, 0.0, 0.0
    count = len(samples)
    energy = 0.0
    peak = 0
    crossings = 0
    previous = samples[0]
    for sample in samples:
        absolute = abs(sample)
        if absolute > peak:
            peak = absolute
        energy += sample * sample
        if (sample < 0) != (previous < 0):
            crossings += 1
        previous = sample
    rms = math.sqrt(energy / count) / 32768.0
    peak_ratio = peak / 32768.0
    rms_dbfs = max(-120.0, 20.0 * math.log10(max(rms, 1e-6)))
    return rms_dbfs, peak_ratio, crossings / max(1, count - 1)


def _load_vad():
    try:
        from silero_vad import get_speech_timestamps, load_silero_vad
        import torch

        torch.set_num_threads(1)
        return (torch, get_speech_timestamps, load_silero_vad(onnx=False)), None
    except Exception as exc:  # The optional VAD integration must never block basic audio analysis.
        log(f"Silero VAD unavailable; continuing with adaptive audio analysis: {exc}")
        return None, "Silero VAD is not installed or could not load its packaged model."


def _speech_intervals(raw: bytearray, offset_seconds: float, detector, model, torch, sample_rate: int) -> list[dict]:
    if not raw or detector is None or model is None:
        return []
    samples = array("h")
    samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
    waveform = torch.tensor(samples.tolist(), dtype=torch.float32) / 32768.0
    try:
        segments = detector(waveform, model, sampling_rate=sample_rate, return_seconds=True)
    except Exception as exc:  # Optional model failures must not stop the baseline analysis.
        log(f"Silero VAD chunk failed; continuing without this chunk: {exc}")
        return []
    return [
        {"start_seconds": offset_seconds + float(segment["start"]), "end_seconds": offset_seconds + float(segment["end"])}
        for segment in segments
    ]


def analyze(path: str, has_audio: bool, duration_seconds: float, cancelled: threading.Event,
            progress: Callable[[float], None], config: dict | None = None,
            audio_stream_index: int = 0) -> tuple[list[dict], list[dict], str | None]:
    if cancelled.is_set():
        raise InterruptedError("Analysis cancelled.")
    if not has_audio:
        return [], [], "No audio track was found; visual analysis will continue."
    settings = (config or DEFAULT_CONFIG)["audio"]
    sample_rate = int(settings["sample_rate"])
    window_seconds = int(settings["window_seconds"])
    window_bytes = sample_rate * window_seconds * 2

    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", path,
        "-map", f"0:a:{audio_stream_index}", "-vn", "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "pipe:1",
    ]
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=window_bytes * 2)
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
    features: list[tuple[float, float, float, float]] = []
    speech: list[dict] = []
    vad_bundle, vad_notice = _load_vad()
    vad_model = vad_bundle[2] if vad_bundle else None
    get_speech_timestamps = vad_bundle[1] if vad_bundle else None
    torch = vad_bundle[0] if vad_bundle else None
    vad_buffer = bytearray()
    vad_buffer_seconds = float(settings["vad_chunk_seconds"])
    vad_buffer_start = 0.0
    next_progress = 5
    samples_seen = 0

    try:
        assert process.stdout is not None
        while True:
            if cancelled.is_set():
                process.terminate()
                raise InterruptedError("Analysis cancelled.")
            raw = _read_exact(process.stdout, window_bytes)
            if not raw:
                break
            if len(raw) % 2:
                raw = raw[:-1]
            if not raw:
                break
            rms_dbfs, peak, zcr = _window_features(raw)
            samples = len(raw) // 2
            center_seconds = (samples_seen + samples / 2) / sample_rate
            samples_seen += samples
            features.append((center_seconds, rms_dbfs, peak, zcr))

            if vad_model is not None:
                if not vad_buffer:
                    vad_buffer_start = (samples_seen - samples) / sample_rate
                vad_buffer.extend(raw)
                if len(vad_buffer) >= int(sample_rate * 2 * vad_buffer_seconds):
                    speech.extend(_speech_intervals(vad_buffer, vad_buffer_start,
                                                    get_speech_timestamps, vad_model, torch, sample_rate))
                    vad_buffer.clear()
            percent = min(49, int(samples_seen / sample_rate / max(1.0, duration_seconds) * 50))
            if percent >= next_progress:
                progress(percent)
                next_progress = percent + 5

        if vad_model is not None and vad_buffer:
            speech.extend(_speech_intervals(vad_buffer, vad_buffer_start,
                                            get_speech_timestamps, vad_model, torch, sample_rate))
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
        raise RuntimeError(f"FFmpeg audio extraction failed: {detail[-500:] or 'unknown error'}")
    if cancelled.is_set():
        raise InterruptedError("Analysis cancelled.")

    observations: list[dict] = []
    baseline_context = max(1, round(float(settings["baseline_context_seconds"]) / window_seconds))
    for index, (timestamp, rms_dbfs, peak, zcr) in enumerate(features):
        if len(features) <= 3:
            continue
        left = max(0, index - baseline_context)
        right = min(len(features), index + baseline_context + 1)
        baseline = [features[i][1] for i in range(left, right) if i != index]
        if not baseline:
            continue
        median = statistics.median(baseline)
        mad = statistics.median(abs(item - median) for item in baseline)
        threshold = max(float(settings["minimum_baseline_delta_db"]), 3.0 * mad)
        delta = rms_dbfs - median
        zcr_baseline = [features[i][3] for i in range(left, right) if i != index]
        zcr_median = statistics.median(zcr_baseline)
        zcr_mad = statistics.median(abs(item - zcr_median) for item in zcr_baseline)
        zcr_delta = abs(zcr - zcr_median)
        zcr_threshold = max(float(settings["minimum_zcr_delta"]), 3.0 * zcr_mad)
        energy_anomaly = delta >= threshold
        texture_anomaly = zcr_delta >= zcr_threshold
        active_audio = rms_dbfs > float(settings["minimum_rms_dbfs"]) and peak >= float(settings["minimum_peak"])
        if active_audio and (energy_anomaly or texture_anomaly):
            energy_magnitude = min(1.0, 0.35 + max(0.0, delta - threshold) / 22.0) if energy_anomaly else 0.0
            texture_magnitude = min(0.65, 0.3 + max(0.0, zcr_delta - zcr_threshold) * 2.0) if texture_anomaly else 0.0
            magnitude = max(energy_magnitude, texture_magnitude)
            observations.append({
                "timestamp_ms": int(timestamp * 1000),
                "signal": "audio_anomaly",
                "magnitude": round(magnitude, 4),
                "metadata": {"rms_dbfs": round(rms_dbfs, 2), "baseline_dbfs": round(median, 2),
                             "delta_db": round(delta, 2), "peak": round(peak, 4), "zero_crossing_rate": round(zcr, 4),
                             "zcr_delta": round(zcr_delta, 4), "texture_change": texture_anomaly},
            })
    return observations, speech, vad_notice
