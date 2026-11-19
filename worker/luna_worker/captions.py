"""Caption cue generation and ASS subtitle output.

Captions are built only from real Faster-Whisper words or segments. Nothing is
invented when transcription is unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

MAX_WORDS = 7
MAX_LINE_CHARS = 32
MAX_CUE_SECONDS = 2.8
GAP_BREAK_SECONDS = 0.7


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    lines: tuple[str, ...]


def _split_lines(text: str) -> tuple[str, ...]:
    words = text.split()
    if not words:
        return ()
    if len(" ".join(words)) <= MAX_LINE_CHARS:
        return (" ".join(words),)
    best_index = 1
    best_delta = float("inf")
    for index in range(1, len(words)):
        left = len(" ".join(words[:index]))
        right = len(" ".join(words[index:]))
        delta = abs(left - right)
        if delta < best_delta:
            best_delta = delta
            best_index = index
    lines = (" ".join(words[:best_index]), " ".join(words[best_index:]))
    return lines


def _word_value(word: dict) -> str:
    value = str(word.get("word", "")).strip()
    return value


def _flush(words: list[dict], cues: list[Cue]) -> None:
    if not words:
        return
    text = " ".join(_word_value(word) for word in words).strip()
    if not text:
        words.clear()
        return
    lines = _split_lines(text)
    if lines:
        cues.append(Cue(float(words[0]["start_seconds"]), float(words[-1]["end_seconds"]), lines))
    words.clear()


def build_cues(segments: list[dict], start_ms: int, end_ms: int) -> list[Cue]:
    clip_start = max(0, start_ms) / 1000.0
    clip_end = max(clip_start + 0.5, end_ms / 1000.0)
    cues: list[Cue] = []
    words: list[dict] = []

    for segment in segments or []:
        segment_words = segment.get("words") or []
        if segment_words:
            for raw_word in segment_words:
                try:
                    word = {
                        "word": _word_value(raw_word),
                        "start_seconds": float(raw_word["start_seconds"]),
                        "end_seconds": float(raw_word["end_seconds"]),
                    }
                except (KeyError, TypeError, ValueError):
                    continue
                if not word["word"]:
                    continue
                if words:
                    gap = word["start_seconds"] - float(words[-1]["end_seconds"])
                    current_span = word["end_seconds"] - float(words[0]["start_seconds"])
                    if gap > GAP_BREAK_SECONDS or len(words) >= MAX_WORDS or current_span > MAX_CUE_SECONDS:
                        _flush(words, cues)
                words.append(word)
                if word["word"][-1:] in ".!?":
                    _flush(words, cues)
        else:
            text = str(segment.get("text", "")).strip()
            if not text:
                continue
            try:
                segment_start = float(segment["start_seconds"])
                segment_end = float(segment["end_seconds"])
            except (KeyError, TypeError, ValueError):
                continue
            lines = _split_lines(text)
            if lines:
                cues.append(Cue(segment_start, segment_end, lines))
    _flush(words, cues)

    clipped: list[Cue] = []
    for cue in cues:
        if cue.end <= clip_start or cue.start >= clip_end:
            continue
        start = max(0.0, cue.start - clip_start)
        end = max(start + 0.35, min(clip_end, cue.end) - clip_start)
        clipped.append(Cue(start, min(end, clip_end - clip_start), cue.lines))
    return clipped


def _timestamp(seconds: float) -> str:
    total_centiseconds = max(0, int(round(seconds * 100)))
    hours, remainder = divmod(total_centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    whole_seconds, centiseconds = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{centiseconds:02d}"


def write_ass(cues: list[Cue], canvas_width: int, canvas_height: int, destination: Path) -> None:
    font_size = max(30, int(round(canvas_height * 0.040)))
    margin_v = max(42, int(round(canvas_height * 0.085)))
    margin_h = max(40, int(round(canvas_width * 0.07)))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {canvas_width}
PlayResY: {canvas_height}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Luna,Arial,{font_size},&H00FFFFFF,&H000000FF,&H00120F0A,&H78000000,1,0,0,0,100,100,0,0,1,3,1,2,{margin_h},{margin_h},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines: list[str] = [header]
    for cue in cues:
        if not cue.lines or cue.end <= cue.start:
            continue
        text = "\\N".join(line.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}") for line in cue.lines)
        lines.append(
            f"Dialogue: 0,{_timestamp(cue.start)},{_timestamp(cue.end)},Luna,,0,0,0,,{text}\n"
        )
    destination.write_text("".join(lines), encoding="utf-8")
