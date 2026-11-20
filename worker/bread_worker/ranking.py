from __future__ import annotations

from typing import Any

from . import DEFAULT_CONFIG


def _reaction_text(transcript: list[dict], timestamp_ms: int) -> str | None:
    from .transcription import REACTION_TERMS

    seconds = timestamp_ms / 1000
    best: tuple[float, str] | None = None
    for segment in transcript:
        start = float(segment["start_seconds"])
        end = float(segment["end_seconds"])
        if seconds < start - 2.0 or seconds > end + 2.0:
            continue
        text = segment["text"]
        lowered = text.lower()
        if not any(term in lowered for term in REACTION_TERMS):
            continue
        distance = 0.0 if start <= seconds <= end else min(abs(seconds - start), abs(seconds - end))
        if best is None or distance < best[0]:
            best = (distance, text)
    return best[1] if best else None


def _merge_speech_intervals(intervals: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for interval in sorted(intervals, key=lambda item: float(item["start_seconds"])):
        start = float(interval["start_seconds"])
        end = float(interval["end_seconds"])
        if end <= start:
            continue
        if merged and start <= float(merged[-1]["end_seconds"]) + 0.35:
            merged[-1]["end_seconds"] = max(float(merged[-1]["end_seconds"]), end)
        else:
            merged.append({"start_seconds": start, "end_seconds": end})
    return merged


def _coalesce_observations(observations: list[dict], tolerance_ms: int) -> list[dict]:
    """Fold duplicate track observations while keeping genuinely separate nearby peaks."""
    coalesced: list[dict] = []
    for observation in observations:
        timestamp = int(observation["timestamp_ms"])
        if (coalesced
                and observation["signal"] == coalesced[-1]["signal"]
                and timestamp - int(coalesced[-1]["timestamp_ms"]) <= tolerance_ms):
            previous = coalesced[-1]
            if observation.get("speech_present"):
                previous["speech_present"] = True
            if observation.get("reaction_text") and not previous.get("reaction_text"):
                previous["reaction_text"] = observation["reaction_text"]
            previous_metadata = previous.get("metadata", {})
            new_metadata = observation.get("metadata", {})
            previous_track = previous_metadata.get("audio_track")
            new_track = new_metadata.get("audio_track")
            tracks = set(previous_metadata.get("audio_tracks", []))
            if previous_track:
                tracks.add(previous_track)
            if new_track:
                tracks.add(new_track)
            if float(observation["magnitude"]) > float(previous["magnitude"]):
                previous["timestamp_ms"] = timestamp
                previous["magnitude"] = observation["magnitude"]
                previous["metadata"] = dict(new_metadata)
            if len(tracks) > 1:
                previous.setdefault("metadata", {})["audio_tracks"] = sorted(tracks)
            continue
        coalesced.append(dict(observation))
    return coalesced


def rank(observations: list[dict], speech_intervals: list[dict], transcript: list[dict], duration_seconds: float,
         config: dict | None = None) -> list[dict]:
    settings = (config or DEFAULT_CONFIG)["ranking"]
    pre_context_ms = int(settings["pre_context_ms"])
    post_context_ms = int(settings["post_context_ms"])
    max_duration_ms = int(settings["max_duration_ms"])
    merge_gap_ms = int(settings["merge_gap_ms"])
    max_event_span_ms = int(settings["max_event_span_ms"])
    minimum_score = float(settings["minimum_score"])
    minimum_separation_ms = int(settings["minimum_separation_ms"])
    weights = settings["weights"]
    speech_intervals = _merge_speech_intervals(speech_intervals)

    enriched: list[dict] = []
    for item in observations:
        observation = dict(item)
        timestamp = int(observation["timestamp_ms"])
        if any(interval["start_seconds"] * 1000 <= timestamp <= interval["end_seconds"] * 1000
               for interval in speech_intervals):
            observation["speech_present"] = True
        reaction = _reaction_text(transcript, timestamp)
        if reaction:
            observation["reaction_text"] = reaction
        enriched.append(observation)

    enriched.sort(key=lambda item: int(item["timestamp_ms"]))
    enriched = _coalesce_observations(enriched, 750)

    groups: list[list[dict[str, Any]]] = []
    for observation in enriched:
        if (not groups
                or int(observation["timestamp_ms"]) - int(groups[-1][-1]["timestamp_ms"]) > merge_gap_ms
                or int(observation["timestamp_ms"]) - int(groups[-1][0]["timestamp_ms"]) > max_event_span_ms):
            groups.append([observation])
        else:
            groups[-1].append(observation)

    results: list[dict] = []
    duration_ms = max(0, int(duration_seconds * 1000))
    for group in groups:
        signals = {item["signal"] for item in group}
        audio = max((float(item["magnitude"]) for item in group if item["signal"] == "audio_anomaly"), default=0.0)
        visual = max((float(item["magnitude"]) for item in group if item["signal"] == "visual_activity"), default=0.0)
        has_speech = any(item.get("speech_present") for item in group)
        reaction_text = next((item.get("reaction_text") for item in group if item.get("reaction_text")), None)
        speech_score = 1.0 if reaction_text else (0.35 if has_speech else 0.0)
        if reaction_text:
            signals.add("speech_reaction")
        elif has_speech:
            signals.add("speech_activity")

        peak_observation = max(group, key=lambda item: float(item["magnitude"]))
        peak_ms = int(peak_observation["timestamp_ms"])
        first_ms = min(int(item["timestamp_ms"]) for item in group)
        last_ms = max(int(item["timestamp_ms"]) for item in group)
        start_ms = max(0, first_ms - pre_context_ms)
        end_ms = min(duration_ms, last_ms + post_context_ms)
        if end_ms - start_ms > max_duration_ms:
            start_ms = max(0, peak_ms - max_duration_ms // 2)
            end_ms = min(duration_ms, start_ms + max_duration_ms)
            start_ms = max(0, end_ms - max_duration_ms)

        score = round(min(100.0, float(weights["audio"]) * audio + float(weights["visual"]) * visual
                          + float(weights["speech"]) * speech_score))
        if score < minimum_score:
            continue
        descriptions = []
        if audio:
            max_delta = max((float(item.get("metadata", {}).get("delta_db", 0))
                             for item in group if item["signal"] == "audio_anomaly"), default=0.0)
            max_short_delta = max((float(item.get("metadata", {}).get("short_delta_db", 0))
                                   for item in group if item["signal"] == "audio_anomaly"), default=0.0)
            if max_short_delta >= 3:
                descriptions.append(f"Audio rose {max_short_delta:.1f} dB above its recent baseline")
            elif max_delta >= 6:
                descriptions.append(f"Audio rose {max_delta:.1f} dB above its long-term baseline")
            elif any(item.get("metadata", {}).get("spectral_change") for item in group):
                descriptions.append("Audio timbre changed from its local baseline")
        if visual:
            descriptions.append("sampled frames showed a visual scene change")
        if has_speech:
            descriptions.append("speech overlapped the event")
        if reaction_text:
            descriptions.append(f"transcript reaction: {reaction_text[:100]}")
        evidence = [
            {"signal": item["signal"], "timestamp_ms": int(item["timestamp_ms"]),
             "magnitude": float(item["magnitude"]), "metadata": item.get("metadata", {})}
            for item in group
        ]
        results.append({
            "start_ms": start_ms,
            "peak_ms": min(max(peak_ms, start_ms), end_ms),
            "end_ms": end_ms,
            "score": score,
            "signals": sorted(signals),
            "evidence": evidence,
            "explanation": "; ".join(descriptions) or "Combined local analysis signals",
        })

    results.sort(key=lambda item: item["start_ms"])
    deduplicated: list[dict] = []
    for candidate in results:
        if deduplicated and candidate["peak_ms"] - deduplicated[-1]["peak_ms"] <= minimum_separation_ms:
            previous = deduplicated[-1]
            previous["end_ms"] = min(duration_ms, max(previous["end_ms"], candidate["end_ms"]))
            if candidate["score"] > previous["score"]:
                previous["peak_ms"] = candidate["peak_ms"]
                previous["score"] = candidate["score"]
            previous["signals"] = sorted(set(previous["signals"]) | set(candidate["signals"]))
            previous["evidence"].extend(candidate["evidence"])
            previous["explanation"] = "; ".join(dict.fromkeys([previous["explanation"], candidate["explanation"]]))
        else:
            if deduplicated and candidate["start_ms"] < deduplicated[-1]["end_ms"]:
                midpoint = (candidate["peak_ms"] + deduplicated[-1]["peak_ms"]) // 2
                deduplicated[-1]["end_ms"] = max(deduplicated[-1]["peak_ms"], midpoint)
                candidate["start_ms"] = min(candidate["peak_ms"], max(candidate["start_ms"], midpoint))
            deduplicated.append(candidate)
    return deduplicated
