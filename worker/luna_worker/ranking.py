from __future__ import annotations

from typing import Any

from . import DEFAULT_CONFIG


def _reaction_text(transcript: list[dict], timestamp_ms: int) -> str | None:
    from .transcription import REACTION_TERMS

    seconds = timestamp_ms / 1000
    for segment in transcript:
        if segment["start_seconds"] - 8 <= seconds <= segment["end_seconds"] + 8:
            text = segment["text"]
            lowered = text.lower()
            if any(term in lowered for term in REACTION_TERMS):
                return text
    return None


def rank(observations: list[dict], speech_intervals: list[dict], transcript: list[dict], duration_seconds: float,
         config: dict | None = None) -> list[dict]:
    settings = (config or DEFAULT_CONFIG)["ranking"]
    pre_context_ms = int(settings["pre_context_ms"])
    post_context_ms = int(settings["post_context_ms"])
    max_duration_ms = int(settings["max_duration_ms"])
    merge_gap_ms = int(settings["merge_gap_ms"])
    max_event_span_ms = int(settings["max_event_span_ms"])
    weights = settings["weights"]
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
        if "audio_anomaly" not in signals and "speech_reaction" not in signals:
            continue
        audio = max((float(item["magnitude"]) for item in group if item["signal"] == "audio_anomaly"), default=0.0)
        visual = max((float(item["magnitude"]) for item in group if item["signal"] == "visual_activity"), default=0.0)
        has_speech = any(item.get("speech_present") for item in group)
        reaction_text = next((item.get("reaction_text") for item in group if item.get("reaction_text")), None)
        speech_score = 1.0 if reaction_text else (0.35 if has_speech else 0.0)
        if reaction_text:
            signals.add("speech_reaction")
        elif has_speech:
            signals.add("speech_activity")

        peak_observation = max(
            group,
            key=lambda item: float(item["magnitude"]) * (1.0 if item["signal"] == "audio_anomaly" else 0.55),
        )
        peak_ms = int(peak_observation["timestamp_ms"])
        first_ms = min(int(item["timestamp_ms"]) for item in group)
        last_ms = max(int(item["timestamp_ms"]) for item in group)
        start_ms = max(0, first_ms - pre_context_ms)
        end_ms = min(duration_ms, last_ms + post_context_ms)
        if end_ms - start_ms > max_duration_ms:
            start_ms = max(0, peak_ms - max_duration_ms // 2)
            end_ms = min(duration_ms, start_ms + max_duration_ms)
            start_ms = max(0, end_ms - max_duration_ms)

        score = round(min(100.0, float(weights["audio"]) * audio + float(weights["visual"]) * visual + float(weights["speech"]) * speech_score))
        descriptions = []
        if audio:
            max_delta = max((float(item.get("metadata", {}).get("delta_db", 0))
                             for item in group if item["signal"] == "audio_anomaly"), default=0.0)
            if max_delta >= 6:
                descriptions.append(f"Audio rose {max_delta:.1f} dB above its local baseline")
            elif any(item.get("metadata", {}).get("texture_change") for item in group):
                descriptions.append("Audio texture changed from its local baseline")
        if visual:
            descriptions.append("sampled frames showed increased visual change")
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
        if deduplicated and candidate["peak_ms"] - deduplicated[-1]["peak_ms"] <= 5_000:
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
