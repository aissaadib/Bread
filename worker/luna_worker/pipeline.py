from __future__ import annotations

import os
import threading
from typing import Callable

from . import ANALYSIS_VERSION, DEFAULT_CONFIG
from . import audio, clips, media, ranking, titles, transcription, video


def analyze(path: str, cancelled: threading.Event, emit: Callable[..., None], config: dict | None = None,
            output_dir: str | None = None) -> None:
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
        capabilities={"audio": info["has_audio"], "visual": True, "speech_detection": bool(speech),
                      "transcription": bool(transcript)}, notices=notices,
    )
