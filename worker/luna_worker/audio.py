from __future__ import annotations

import math
import statistics
import subprocess
import threading
from array import array
from collections import deque
from typing import Callable

from . import DEFAULT_CONFIG
from .protocol import log

try:  # NumPy is present with the optional speech stack but audio analysis must still work without it.
    import numpy as np
except Exception:  # pragma: no cover - exercised only in minimal Python installations.
    np = None

_SPECTRUM_BANDS = 24
_spectrum_plans: dict[tuple[int, int], tuple] = {}
_VAD_CACHE: tuple | None = None
_VAD_NOTICE: str | None = None
_VAD_LOCK = threading.Lock()


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


def _spectral_shape(raw: bytes, sample_rate: int):
    """Return a loudness-independent log band spectrum so flux measures timbre change."""
    if np is None:
        return None
    usable = raw[: len(raw) - (len(raw) % 2)]
    if len(usable) < 512:
        return None
    samples = np.frombuffer(usable, dtype="<i2")
    if samples.size < 256:
        return None
    values = samples.astype(np.float32) / 32768.0
    values *= np.hanning(values.size)
    magnitude = np.abs(np.fft.rfft(values))
    key = (values.size, sample_rate)
    plan = _spectrum_plans.get(key)
    if plan is None:
        frequencies = np.fft.rfftfreq(values.size, 1.0 / sample_rate)
        upper = max(120.0, min(sample_rate / 2.0, 8000.0))
        edges = np.geomspace(60.0, upper, _SPECTRUM_BANDS + 1)
        bins = np.clip(np.searchsorted(edges, frequencies, side="right") - 1, 0, _SPECTRUM_BANDS - 1)
        plan = (bins,)
        _spectrum_plans[key] = plan
    energy = np.bincount(plan[0], weights=magnitude, minlength=_SPECTRUM_BANDS)
    compressed = np.log1p(energy)
    total = float(compressed.sum())
    if total <= 0.0:
        return None
    return compressed / total


def _spectral_flux(current, previous) -> float:
    if current is None or previous is None or np is None:
        return 0.0
    return float(0.5 * np.abs(current - previous).sum())


def load_vad() -> tuple[tuple | None, str | None]:
    """Load Silero once per worker process and reuse it for every track."""
    global _VAD_CACHE, _VAD_NOTICE
    with _VAD_LOCK:
        if _VAD_CACHE is not None or _VAD_NOTICE is not None:
            return _VAD_CACHE, _VAD_NOTICE
        try:
            from silero_vad import get_speech_timestamps, load_silero_vad
            import torch
        except Exception as exc:
            _VAD_NOTICE = "Silero VAD is not installed; speech detection is unavailable in this Python environment."
            log(f"Silero VAD unavailable: {exc}")
            return None, _VAD_NOTICE
        try:
            torch.set_num_threads(1)
            try:
                model = load_silero_vad(onnx=True)
            except Exception:
                model = load_silero_vad(onnx=False)
            _VAD_CACHE = (torch, get_speech_timestamps, model)
            log("Silero VAD model loaded.")
        except Exception as exc:
            _VAD_NOTICE = "Silero VAD is installed but its packaged model could not be loaded."
            log(f"Silero VAD model failed to load: {exc}")
        return _VAD_CACHE, _VAD_NOTICE


def _speech_intervals(raw: bytearray, offset_seconds: float, detector, model, torch,
                      sample_rate: int) -> list[dict]:
    if not raw or detector is None or model is None:
        return []
    usable = raw[: len(raw) - (len(raw) % 2)]
    if np is not None:
        waveform = torch.from_numpy(np.frombuffer(usable, dtype="<i2").astype(np.float32) / 32768.0)
    else:
        samples = array("h")
        samples.frombytes(usable)
        waveform = torch.tensor(samples.tolist(), dtype=torch.float32) / 32768.0
    try:
        segments = detector(waveform, model, sampling_rate=sample_rate, return_seconds=True)
    except Exception as exc:  # Optional model failures must not stop the baseline analysis.
        log(f"Silero VAD chunk failed; continuing without this chunk: {exc}")
        return []
    return [
        {"start_seconds": offset_seconds + float(segment["start"]),
         "end_seconds": offset_seconds + float(segment["end"])}
        for segment in segments
    ]


def _window_baseline(values: list[float], index: int, half_window: int) -> tuple[float, float, list[float]]:
    left = max(0, index - half_window)
    right = min(len(values), index + half_window + 1)
    sample = [values[i] for i in range(left, right) if i != index]
    if not sample:
        sample = [values[index]]
    median = statistics.median(sample)
    mad = statistics.median(abs(value - median) for value in sample)
    return median, mad, sample


def analyze(path: str, has_audio: bool, duration_seconds: float, cancelled: threading.Event,
            progress: Callable[[float], None], config: dict | None = None,
            audio_stream_index: int = 0, vad: tuple | None = None) -> tuple[list[dict], list[dict], str | None, dict]:
    summary = {"active_windows": 0, "total_windows": 0, "mean_rms_dbfs": -120.0,
               "peak_dbfs": -120.0, "speech_seconds": 0.0}
    if cancelled.is_set():
        raise InterruptedError("Analysis cancelled.")
    if not has_audio:
        return [], [], "No audio track was found; visual analysis will continue.", summary

    settings = (config or DEFAULT_CONFIG)["audio"]
    sample_rate = int(settings["sample_rate"])
    window_seconds = int(settings["window_seconds"])
    window_bytes = sample_rate * window_seconds * 2
    if vad is None:
        vad, _ = load_vad()

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
    fluxes: list[float] = []
    speech: list[dict] = []
    vad_model = vad[2] if vad else None
    get_speech_timestamps = vad[1] if vad else None
    torch = vad[0] if vad else None
    vad_buffer = bytearray()
    vad_buffer_seconds = float(settings["vad_chunk_seconds"])
    vad_buffer_start = 0.0
    next_progress = 5
    samples_seen = 0
    peak_ratio = 0.0
    previous_shape = None

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
            peak_ratio = max(peak_ratio, peak)
            shape = _spectral_shape(raw, sample_rate)
            flux = _spectral_flux(shape, previous_shape)
            previous_shape = shape
            samples = len(raw) // 2
            center_seconds = (samples_seen + samples / 2) / sample_rate
            samples_seen += samples
            features.append((center_seconds, rms_dbfs, peak, zcr))
            fluxes.append(flux)

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

    summary["total_windows"] = len(features)
    if features:
        summary["mean_rms_dbfs"] = round(statistics.fmean(item[1] for item in features), 2)
        summary["peak_dbfs"] = round(max(-120.0, 20.0 * math.log10(max(peak_ratio, 1e-6))), 2)
        summary["active_windows"] = sum(
            1 for _, rms_dbfs, peak, _ in features
            if rms_dbfs > float(settings["minimum_rms_dbfs"]) and peak >= float(settings["minimum_peak"])
        )
    summary["speech_seconds"] = round(sum(max(0.0, item["end_seconds"] - item["start_seconds"]) for item in speech), 2)

    # A digitally silent track cannot contain meaningful speech; drop spurious VAD output.
    if summary["mean_rms_dbfs"] < -80.0 and summary["peak_dbfs"] < -60.0:
        speech = []

    observations: list[dict] = []
    if len(features) <= 3:
        return observations, speech, None, summary

    long_half = max(1, round(float(settings["long_baseline_seconds"]) / window_seconds / 2))
    short_half = max(1, round(float(settings["short_baseline_seconds"]) / window_seconds / 2))
    rms_values = [item[1] for item in features]
    minimum_long_delta = float(settings["minimum_baseline_delta_db"])
    maximum_long_delta = float(settings["maximum_baseline_delta_db"])
    minimum_short_delta = float(settings["minimum_short_delta_db"])
    minimum_flux = float(settings["minimum_spectral_flux"])

    for index, (timestamp, rms_dbfs, peak, zcr) in enumerate(features):
        long_median, long_mad, _ = _window_baseline(rms_values, index, long_half)
        short_median, short_mad, _ = _window_baseline(rms_values, index, short_half)
        delta_long = rms_dbfs - long_median
        delta_short = rms_dbfs - short_median
        long_threshold = min(maximum_long_delta, max(minimum_long_delta, 3.0 * long_mad))
        short_threshold = min(maximum_long_delta, max(minimum_short_delta, 3.0 * short_mad))
        active_audio = rms_dbfs > float(settings["minimum_rms_dbfs"]) and peak >= float(settings["minimum_peak"])

        flux = fluxes[index]
        flux_median, flux_mad, _ = _window_baseline(fluxes, index, long_half)
        flux_threshold = max(minimum_flux, flux_median + 3.0 * flux_mad)
        spectral_change = active_audio and flux >= flux_threshold and delta_short >= max(1.5, minimum_short_delta / 2)
        energy_change = (active_audio and delta_short >= short_threshold
                         and delta_long >= max(2.0, minimum_long_delta / 2))

        if not energy_change and not spectral_change:
            continue
        energy_magnitude = 0.55 + min(0.45, max(0.0, delta_short - short_threshold) / 18.0) if energy_change else 0.0
        flux_magnitude = 0.35 + min(0.65, max(0.0, flux - flux_threshold) / max(0.1, 0.5 - flux_threshold)) if spectral_change else 0.0
        magnitude = max(energy_magnitude, flux_magnitude)
        observations.append({
            "timestamp_ms": int(timestamp * 1000),
            "signal": "audio_anomaly",
            "magnitude": round(magnitude, 4),
            "metadata": {
                "rms_dbfs": round(rms_dbfs, 2),
                "baseline_dbfs": round(long_median, 2),
                "short_baseline_dbfs": round(short_median, 2),
                "delta_db": round(delta_long, 2),
                "short_delta_db": round(delta_short, 2),
                "peak": round(peak, 4),
                "zero_crossing_rate": round(zcr, 4),
                "spectral_flux": round(flux, 4),
                "flux_baseline": round(flux_median, 4),
                "energy_change": energy_change,
                "spectral_change": spectral_change,
                "audio_track": audio_stream_index + 1,
            },
        })
    return observations, speech, None, summary
