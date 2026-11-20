from __future__ import annotations

import threading
from typing import Callable

from . import ANALYSIS_VERSION, DEFAULT_CONFIG
from . import audio, clips, media, ranking, titles, transcription, video


def _heartbeat(stop: threading.Event, emit: Callable[..., None], stage: str, progress: int) -> None:
    """Keep the desktop host's idle timer alive while a model works without natural progress."""
    while not stop.wait(10):
        try:
            emit("progress", stage=stage, progress=progress)
        except OSError:
            return


def analyze(path: str, cancelled: threading.Event, emit: Callable[..., None], config: dict | None = None,
            output_dir: str | None = None) -> None:
    config = config or DEFAULT_CONFIG
    emit("stage", stage="inspecting", progress=1)
    info = media.inspect_media(path)
    observations: list[dict] = []
    notices: list[str] = []
    speech: list[dict] = []
    audio_summary = {"active_windows": 0, "total_windows": 0, "speech_seconds": 0.0}

    vad, vad_notice = audio.load_vad() if info["has_audio"] else (None, None)
    if vad_notice:
        notices.append(vad_notice)

    emit("stage", stage="analyzing_audio", progress=4)
    audio_tracks = max(1, int(info["audio_tracks"]))
    for stream_index in range(audio_tracks if info["has_audio"] else 1):
        audio_observations, speech_intervals, notice, summary = audio.analyze(
            path, info["has_audio"], info["duration_seconds"], cancelled,
            lambda percent, index=stream_index: emit(
                "progress", stage="analyzing_audio",
                progress=4 + int(((index + percent / 100) / audio_tracks) * 46),
            ),
            config, audio_stream_index=stream_index, vad=vad,
        )
        observations.extend(audio_observations)
        speech.extend(speech_intervals)
        audio_summary["active_windows"] += int(summary["active_windows"])
        audio_summary["total_windows"] += int(summary["total_windows"])
        audio_summary["speech_seconds"] += float(summary["speech_seconds"])
        if notice and notice not in notices:
            notices.append(notice)
    if info["has_audio"] and audio_summary["active_windows"] == 0:
        notices.append("The audio track is effectively silent; audio and speech signals were skipped.")

    if cancelled.is_set():
        raise InterruptedError("Analysis cancelled.")
    emit("stage", stage="analyzing_video", progress=50)
    visual_observations = video.analyze(
        path, info["duration_seconds"], cancelled,
        lambda percent: emit("progress", stage="analyzing_video", progress=percent),
        config,
    )
    observations.extend(visual_observations)

    transcript: list[dict] = []
    audible = bool(info["has_audio"]) and audio_summary["active_windows"] > 0
    if audible and transcription.available():
        emit("stage", stage="transcribing", progress=91)
        stop = threading.Event()
        heartbeat = threading.Thread(target=_heartbeat, args=(stop, emit, "transcribing", 91), daemon=True)
        heartbeat.start()
        try:
            transcript, notice = transcription.transcribe(path, lambda message: emit("notice", message=message))
        finally:
            stop.set()
            heartbeat.join(timeout=2)
        if notice:
            notices.append(notice)
        elif not transcript:
            notices.append("Local transcription ran but found no speech segments.")
    elif audible:
        notices.append("Transcription is optional; no local Whisper model was found. Set BREAD_WHISPER_MODEL or place one under .models.")
    elif info["has_audio"]:
        notices.append("Transcription was skipped because the audio track is effectively silent.")

    if cancelled.is_set():
        raise InterruptedError("Analysis cancelled.")
    emit("stage", stage="ranking", progress=95)
    candidates = ranking.rank(observations, speech, transcript, info["duration_seconds"], config)
    if output_dir and candidates:
        emit("stage", stage="rendering_clips", progress=96)
    title_fallback_noted = False
    for index, candidate in enumerate(candidates, start=1):
        title, title_source = titles.generate(candidate, transcript)
        candidate["title"] = title
        candidate["title_source"] = title_source
        if title_source == "evidence_fallback" and not title_fallback_noted:
            notices.append("Local AI title model is not available; clips use descriptive evidence titles.")
            title_fallback_noted = True
        if output_dir:
            try:
                candidate["clip_path"] = clips.render(path, output_dir, candidate, title, index)
            except RuntimeError as exc:
                candidate["clip_path"] = None
                notices.append(str(exc))
            emit("progress", stage="rendering_clips", progress=96 + int(index / len(candidates) * 3))
        emit("candidate", candidate=candidate)
    emit(
        "complete", progress=100, duration_seconds=info["duration_seconds"],
        candidate_count=len(candidates), analysis_version=ANALYSIS_VERSION,
        capabilities={"audio": info["has_audio"], "visual": True, "speech_detection": vad is not None,
                      "transcription": bool(transcript)}, notices=notices, transcript=transcript,
    )
