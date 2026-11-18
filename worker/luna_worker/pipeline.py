from __future__ import annotations

import os
import threading
from typing import Callable

from . import ANALYSIS_VERSION, DEFAULT_CONFIG
from . import audio, media, ranking, transcription, video


def analyze(path: str, cancelled: threading.Event, emit: Callable[..., None], config: dict | None = None) -> None:
    config = config or DEFAULT_CONFIG
    emit("stage", stage="inspecting", progress=1)
    info = media.inspect_media(path)
    observations: list[dict] = []
    notices: list[str] = []
    speech: list[dict] = []

    emit("stage", stage="analyzing_audio", progress=4)
    audio_tracks = max(1, int(info["audio_tracks"]))
    for stream_index in range(audio_tracks if info["has_audio"] else 1):
        audio_observations, speech_intervals, notice = audio.analyze(
            path, info["has_audio"], info["duration_seconds"], cancelled,
            lambda percent, index=stream_index: emit(
                "progress", stage="analyzing_audio",
                progress=4 + int(((index + percent / 100) / audio_tracks) * 46),
            ),
            config, audio_stream_index=stream_index,
        )
        observations.extend(audio_observations)
        speech.extend(speech_intervals)
        if notice and notice not in notices:
            notices.append(notice)

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
    if os.environ.get("LUNA_WHISPER_MODEL"):
        emit("stage", stage="transcribing", progress=91)
        transcript, notice = transcription.transcribe(path, lambda message: emit("notice", message=message))
        if notice:
            notices.append(notice)
    else:
        notices.append("Transcription is optional and is not configured; no model was downloaded.")

    if cancelled.is_set():
        raise InterruptedError("Analysis cancelled.")
    emit("stage", stage="ranking", progress=95)
    candidates = ranking.rank(observations, speech, transcript, info["duration_seconds"], config)
    for candidate in candidates:
        emit("candidate", candidate=candidate)
    emit(
        "complete", progress=100, duration_seconds=info["duration_seconds"],
        candidate_count=len(candidates), analysis_version=ANALYSIS_VERSION,
        capabilities={"audio": info["has_audio"], "visual": True, "speech_detection": bool(speech),
                      "transcription": bool(transcript)}, notices=notices,
    )
