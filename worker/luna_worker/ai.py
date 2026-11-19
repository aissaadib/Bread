"""Optional cloud AI assessment layer (network-free core).

This module holds everything about Gemini assessment that can be tested without
a network connection: window planning over the local detector's candidates,
prompt construction, strict validation of the returned JSON, and fusion of the
semantic score with the existing local evidence.

The HTTP transport lives in ``gemini.py`` and orchestration in ``ai_worker.py``.
Luna stays fully functional when this layer never runs.
"""

from __future__ import annotations

import json
from typing import Any

DEFAULT_MODEL = "gemini-3.8-flash"

EVENT_TYPES = (
    "goal",
    "point",
    "round_win",
    "clutch",
    "skilled_play",
    "unexpected_death",
    "funny_moment",
    "reaction",
    "chase",
    "fail",
    "other",
    "none",
)

EVENT_LABELS = {
    "goal": "Goal",
    "point": "Point scored",
    "round_win": "Round win",
    "clutch": "Clutch play",
    "skilled_play": "Skilled play",
    "unexpected_death": "Unexpected death",
    "funny_moment": "Funny moment",
    "reaction": "Reaction",
    "chase": "Chase",
    "fail": "Failed attempt",
    "other": "Highlight",
    "none": "No highlight",
}

#: Model output is constrained to this schema so a malformed reply is a
#: protocol error we can reject instead of guessing at free-form text.
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "is_highlight": {"type": "BOOLEAN"},
        "event_type": {"type": "STRING", "enum": list(EVENT_TYPES)},
        "interest_score": {"type": "INTEGER"},
        "peak_offset_seconds": {"type": "NUMBER", "nullable": True},
        "reason": {"type": "STRING"},
        "visually_clear": {"type": "BOOLEAN"},
        "mostly_menu": {"type": "BOOLEAN"},
        "visual_focus": {
            "type": "OBJECT",
            "nullable": True,
            "properties": {"x": {"type": "NUMBER"}, "y": {"type": "NUMBER"}},
            "required": ["x", "y"],
        },
    },
    "required": ["is_highlight", "event_type", "interest_score", "reason"],
    "propertyOrdering": [
        "is_highlight", "event_type", "interest_score", "peak_offset_seconds",
        "reason", "visually_clear", "mostly_menu", "visual_focus",
    ],
}

HIGHLIGHT_DEFINITION = (
    "A worthwhile gaming highlight is one self-contained moment where the outcome "
    "of play visibly changes or a human reaction peaks: a goal, a point or round win, "
    "a clutch or comeback, a skilled play such as a double kill or ace, an unexpected "
    "death or failure, a funny glitch, a jump-scare, or a decisive fight. "
    "It is NOT: a menu, loading screen, lobby, inventory or settings screen, "
    "ordinary traversal with no consequence, a scoreboard with no action, "
    "or a static view with only ambient noise."
)

DEFAULTS: dict[str, Any] = {
    "model": DEFAULT_MODEL,
    "enabled": False,
    "max_windows": 6,
    "window_seconds": 22,
    "segment_seconds": 30,
    "pre_roll_seconds": 4,
    "post_roll_seconds": 6,
    "sample_every_seconds": 300,
    "max_sample_windows": 2,
    "low_confidence_score": 45,
    "max_low_confidence_windows": 2,
    "segment_max_width": 720,
    "segment_fps": 30,
    "request_timeout_seconds": 150,
    "max_retries": 3,
    "local_weight": 0.6,
    "ai_weight": 0.4,
}


class AssessmentError(ValueError):
    """The model reply could not be trusted as a valid assessment."""

    def __init__(self, message: str, code: str = "invalid_response"):
        super().__init__(message)
        self.code = code


def settings(config: dict[str, Any] | None) -> dict[str, Any]:
    """Merge the stored ``ai`` config over the defaults, ignoring bad values.

    Accepts either a full analysis config (``{"ai": {...}}``) or an already
    flattened options dict, so callers cannot silently fall back to the
    defaults and ignore the user's window budget.
    """
    merged = dict(DEFAULTS)
    raw: dict[str, Any] | None = None
    if isinstance(config, dict):
        nested = config.get("ai")
        if isinstance(nested, dict):
            raw = nested
        elif any(key in config for key in DEFAULTS):
            raw = config
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key not in merged:
                continue
            default = merged[key]
            if isinstance(default, bool):
                merged[key] = bool(value)
            elif isinstance(default, int) and not isinstance(default, bool):
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    merged[key] = int(value)
            elif isinstance(default, float):
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    merged[key] = float(value)
            elif isinstance(value, str):
                merged[key] = value
    merged["max_windows"] = max(1, min(12, int(merged["max_windows"])))
    merged["window_seconds"] = max(6.0, min(60.0, float(merged["window_seconds"])))
    merged["segment_seconds"] = max(
        merged["window_seconds"], min(120.0, float(merged["segment_seconds"]))
    )
    merged["max_retries"] = max(0, min(6, int(merged["max_retries"])))
    return merged


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return min(maximum, max(minimum, value))


def _pick(item: dict[str, Any], snake: str, camel: str, default: Any = None) -> Any:
    """The Rust host writes camelCase; the local pipeline uses snake_case."""
    if snake in item and item[snake] is not None:
        return item[snake]
    if camel in item and item[camel] is not None:
        return item[camel]
    return default


def normalize_candidates(candidates: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in candidates or []:
        if not isinstance(item, dict):
            continue
        normalized.append({
            "id": _pick(item, "id", "id"),
            "start_ms": int(_pick(item, "start_ms", "startMs", 0) or 0),
            "peak_ms": int(_pick(item, "peak_ms", "peakMs", 0) or 0),
            "end_ms": int(_pick(item, "end_ms", "endMs", 0) or 0),
            "score": int(_pick(item, "score", "score", 0) or 0),
            "signals": list(_pick(item, "signals", "signals", []) or []),
        })
    return normalized


def plan_windows(
    candidates: list[dict[str, Any]],
    duration_ms: int,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Choose a bounded set of segments to send to the model.

    Every locally detected candidate gets priority, so Gemini sees the same
    evidence the user sees. Promising low-confidence candidates and periodic
    samples are included afterwards so the model can also surface moments the
    local detector missed, which is the whole point of adding it.
    """
    options = settings(config)
    duration_ms = max(1, int(duration_ms))
    window_ms = int(options["window_seconds"] * 1000)
    segment_ms = int(options["segment_seconds"] * 1000)
    pre_roll_ms = int(options["pre_roll_seconds"] * 1000)
    post_roll_ms = int(options["post_roll_seconds"] * 1000)

    def make_window(peak_ms: int, candidate: dict[str, Any] | None,
                    origin: str) -> dict[str, Any]:
        peak_ms = int(_clamp(peak_ms, 0, max(0, duration_ms - 1)))
        start = peak_ms - pre_roll_ms - (window_ms - pre_roll_ms - post_roll_ms) // 2
        start = int(_clamp(start, 0, max(0, duration_ms - 1000)))
        end = int(min(duration_ms, start + window_ms))
        if end - start > segment_ms:
            end = start + segment_ms
        if end <= start:
            end = min(duration_ms, start + 1000)
        return {
            "start_ms": start,
            "end_ms": end,
            "origin": origin,
            "candidate_id": int(candidate["id"]) if candidate and candidate.get("id") is not None else None,
            "local_score": int(candidate.get("score", 0)) if candidate else 0,
            "local_signals": list(candidate.get("signals") or []) if candidate else [],
            "peak_ms": peak_ms,
        }

    ordered = sorted(
        normalize_candidates(candidates),
        key=lambda item: (-float(item.get("score", 0)), int(item.get("start_ms", 0))),
    )
    windows: list[dict[str, Any]] = []
    low_confidence_limit = int(options["max_low_confidence_windows"])
    low_confidence_used = 0
    low_threshold = int(options["low_confidence_score"])

    for candidate in ordered:
        if len(windows) >= int(options["max_windows"]):
            break
        score = float(candidate.get("score", 0))
        if score < low_threshold:
            if low_confidence_used >= low_confidence_limit:
                continue
            low_confidence_used += 1
        peak = int(candidate.get("peak_ms") or candidate.get("start_ms") or 0)
        window = make_window(peak, candidate, "candidate")
        if any(_overlaps(window, existing) for existing in windows):
            continue
        windows.append(window)

    sample_limit = int(options["max_sample_windows"])
    sample_every_ms = max(30_000, int(options["sample_every_seconds"] * 1000))
    sampled = 0
    position = sample_every_ms // 2
    while (
        sampled < sample_limit
        and len(windows) < int(options["max_windows"])
        and position < duration_ms
    ):
        window = make_window(position, None, "sample")
        if not any(_overlaps(window, existing) for existing in windows):
            windows.append(window)
            sampled += 1
        position += sample_every_ms

    for index, window in enumerate(windows):
        window["index"] = index
    return windows


def _overlaps(left: dict[str, Any], right: dict[str, Any]) -> bool:
    start = max(int(left["start_ms"]), int(right["start_ms"]))
    end = min(int(left["end_ms"]), int(right["end_ms"]))
    if end <= start:
        return False
    shorter = min(
        int(left["end_ms"]) - int(left["start_ms"]),
        int(right["end_ms"]) - int(right["start_ms"]),
    )
    return (end - start) >= shorter * 0.5


def build_prompt(window: dict[str, Any]) -> str:
    """Explicit instructions; deliberately not "find viral moments"."""
    local_note = ""
    if window.get("origin") == "candidate" and window.get("local_signals"):
        signals = ", ".join(str(signal).replace("_", " ") for signal in window["local_signals"])
        local_note = (
            f"\n\nContext: a local signal detector flagged this window "
            f"({signals}). Treat that as a hint only, not as proof." 
        )
    return (
        "You are reviewing one short segment cut from a gaming recording. "
        "Judge only what is visible or audible inside this segment.\n\n"
        f"{HIGHLIGHT_DEFINITION}\n\n"
        "Rules:\n"
        "- peak_offset_seconds is the single most interesting instant, measured in "
        "seconds from the start of this segment (0 = first frame).\n"
        "- If nothing worthwhile happens, set is_highlight to false, interest_score "
        "below 25, and peak_offset_seconds to null.\n"
        "- Set mostly_menu to true when the segment is menus, lobbies, loading "
        "screens, inventories or scoreboards rather than gameplay.\n"
        "- visual_focus is the normalized centre of the primary action "
        "(x and y between 0 and 1) when a single focus exists.\n"
        "- reason is one sentence about what actually happens, with no speculation "
        "about anything outside this segment.\n"
        "- Output JSON that matches the provided schema."
        f"{local_note}"
    )


def _strip_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    return cleaned.strip()


def parse_assessment(
    payload: Any, window: dict[str, Any]
) -> dict[str, Any]:
    """Validate a model reply and convert it to absolute timestamps.

    Every failure mode raises ``AssessmentError`` so the caller can record the
    rejection instead of inventing evidence.
    """
    if isinstance(payload, str):
        text = _strip_fences(payload)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AssessmentError(f"The model reply was not valid JSON: {exc.msg}",
                                  "invalid_json") from exc
    if not isinstance(payload, dict):
        raise AssessmentError("The model reply was not a JSON object.", "invalid_shape")

    is_highlight = payload.get("is_highlight")
    if not isinstance(is_highlight, bool):
        raise AssessmentError("The model reply is missing a boolean is_highlight.",
                              "missing_is_highlight")

    raw_score = payload.get("interest_score")
    if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
        raise AssessmentError("The model reply has no numeric interest_score.",
                              "missing_interest_score")
    interest = int(round(_clamp(float(raw_score), 0.0, 100.0)))

    event_type = payload.get("event_type")
    if not isinstance(event_type, str) or not event_type.strip():
        raise AssessmentError("The model reply has no event_type.", "missing_event_type")
    event_type = event_type.strip().lower()
    if event_type not in EVENT_TYPES:
        event_type = "other"

    reason = payload.get("reason")
    reason = reason.strip()[:400] if isinstance(reason, str) else ""

    start_ms = int(window["start_ms"])
    end_ms = int(window["end_ms"])
    duration_seconds = max(0.001, (end_ms - start_ms) / 1000.0)

    offset = payload.get("peak_offset_seconds")
    peak_ms: int | None = None
    if offset is not None and not isinstance(offset, bool) and isinstance(offset, (int, float)):
        clamped = _clamp(float(offset), 0.0, duration_seconds)
        peak_ms = int(round(start_ms + clamped * 1000.0))
        peak_ms = int(_clamp(peak_ms, start_ms, end_ms))

    if is_highlight and peak_ms is None:
        raise AssessmentError(
            "The model marked a highlight without a usable peak_offset_seconds.",
            "missing_peak",
        )

    focus = payload.get("visual_focus")
    focus_x: float | None = None
    focus_y: float | None = None
    focus_confidence = 0.0
    if isinstance(focus, dict):
        raw_x, raw_y = focus.get("x"), focus.get("y")
        if (
            isinstance(raw_x, (int, float)) and not isinstance(raw_x, bool)
            and isinstance(raw_y, (int, float)) and not isinstance(raw_y, bool)
            and 0.0 <= float(raw_x) <= 1.0 and 0.0 <= float(raw_y) <= 1.0
        ):
            focus_x = round(float(raw_x), 4)
            focus_y = round(float(raw_y), 4)
            focus_confidence = 0.5

    return {
        "is_highlight": is_highlight,
        "event_type": event_type if is_highlight else "none",
        "interest_score": interest,
        "peak_ms": peak_ms,
        "peak_offset_seconds": None if peak_ms is None else round((peak_ms - start_ms) / 1000.0, 3),
        "reason": reason,
        "visually_clear": bool(payload.get("visually_clear")) if isinstance(
            payload.get("visually_clear"), bool) else None,
        "mostly_menu": bool(payload.get("mostly_menu")) if isinstance(
            payload.get("mostly_menu"), bool) else None,
        "focus_x": focus_x,
        "focus_y": focus_y,
        "focus_confidence": focus_confidence,
        "window_start_ms": start_ms,
        "window_end_ms": end_ms,
        "window_duration_seconds": round(duration_seconds, 3),
        "segment_relative_offset_seconds": None if peak_ms is None else round(
            (peak_ms - start_ms) / 1000.0, 3),
    }


def fuse(local_score: int, assessment: dict[str, Any] | None,
         config: dict[str, Any] | None = None) -> tuple[int, str]:
    """Combine the local ranking score with semantic evidence.

    The local detector stays the baseline: without an assessment the score is
    untouched, and a locally strong visual-only highlight is never discarded
    just because the model was unsure about audio or speech.
    """
    options = settings(config)
    local = int(_clamp(float(local_score), 0.0, 100.0))
    if not assessment:
        return local, "Local analysis only"
    if assessment.get("is_highlight"):
        local_weight = float(options["local_weight"])
        ai_weight = float(options["ai_weight"])
        combined = int(round(_clamp(local * local_weight + assessment["interest_score"] * ai_weight,
                                    0.0, 100.0)))
        label = f"Local {local} + AI {assessment['interest_score']}"
        if assessment.get("mostly_menu"):
            combined = int(round(combined * 0.7))
            label += " (AI saw mostly menus)"
        return combined, label
    demoted = int(round(local * 0.45))
    return demoted, f"Local {local}, AI found no highlight"


def build_title(assessment: dict[str, Any]) -> str | None:
    """A title made only from what the model actually returned."""
    if not assessment or not assessment.get("is_highlight"):
        return None
    label = EVENT_LABELS.get(assessment.get("event_type", "other"), "Highlight")
    reason = (assessment.get("reason") or "").strip().rstrip(".")
    if not reason:
        return label
    short = reason if len(reason) <= 72 else reason[:69].rstrip() + "…"
    if short.lower().startswith(label.lower()):
        return short
    return f"{label} — {short}"


def summarize_evidence(assessment: dict[str, Any]) -> str:
    """One-line provenance string stored with the candidate."""
    bits = [
        f"Gemini {assessment.get('model', '')}".strip(),
        f"{EVENT_LABELS.get(assessment.get('event_type', 'other'), 'Highlight')}",
        f"interest {assessment.get('interest_score')}",
    ]
    if assessment.get("mostly_menu"):
        bits.append("mostly menus")
    if assessment.get("visually_clear") is False:
        bits.append("unclear visuals")
    return " · ".join(bit for bit in bits if bit)
