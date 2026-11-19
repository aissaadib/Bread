"""Shared short-form framing geometry.

The desktop preview and the FFmpeg renderer both use the values produced here
(or their TypeScript mirror in ``src/ui/framing.ts``) so the composition the
user approves is the composition that is encoded.
"""

from __future__ import annotations

from dataclasses import dataclass

CANVAS_DIMENSIONS = {"vertical": (1080, 1920), "square": (1080, 1080)}
ASPECTS = ("vertical", "square", "original")
MODES = ("full_frame", "crop", "original")


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class Layout:
    aspect: str
    mode: str
    canvas_width: int
    canvas_height: int
    foreground: Rect
    background: Rect
    crop: Rect
    has_horizontal_crop: bool
    has_vertical_crop: bool


def clamp_unit(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


def even(value: float) -> int:
    """Round to an even positive integer; yuv420p and x264 prefer even sizes."""
    return max(2, int(round(value / 2.0)) * 2)


def normalize_mode(aspect: str, mode: str) -> str:
    if aspect not in ASPECTS:
        raise ValueError(f"Unsupported aspect preset: {aspect}")
    if aspect == "original":
        return "original"
    if mode not in MODES:
        raise ValueError(f"Unsupported presentation mode: {mode}")
    return mode


def canvas_dimensions(aspect: str, source_width: int, source_height: int) -> tuple[int, int]:
    if aspect == "original":
        return even(source_width), even(source_height)
    if aspect not in CANVAS_DIMENSIONS:
        raise ValueError(f"Unsupported aspect preset: {aspect}")
    return CANVAS_DIMENSIONS[aspect]


def _contain(source_width: int, source_height: int, canvas_width: int, canvas_height: int) -> Rect:
    if source_width / source_height >= canvas_width / canvas_height:
        width = canvas_width
        height = min(canvas_height, even(canvas_width * source_height / source_width))
    else:
        height = canvas_height
        width = min(canvas_width, even(canvas_height * source_width / source_height))
    return Rect((canvas_width - width) // 2, (canvas_height - height) // 2, width, height)


def _cover(source_width: int, source_height: int, canvas_width: int, canvas_height: int) -> Rect:
    if source_width / source_height >= canvas_width / canvas_height:
        height = canvas_height
        width = even(canvas_height * source_width / source_height)
    else:
        width = canvas_width
        height = even(canvas_width * source_height / source_width)
    return Rect((canvas_width - width) // 2, (canvas_height - height) // 2, width, height)


def _even_rect(source_width: int, source_height: int, crop_width: int, crop_height: int,
               crop_x: float, crop_y: float) -> Rect:
    crop_width = min(source_width, max(2, crop_width - crop_width % 2))
    crop_height = min(source_height, max(2, crop_height - crop_height % 2))
    x = int(round((source_width - crop_width) * clamp_unit(crop_x)))
    y = int(round((source_height - crop_height) * clamp_unit(crop_y)))
    x = min(max(0, x), source_width - crop_width)
    y = min(max(0, y), source_height - crop_height)
    if x % 2:
        x = max(0, x - 1)
    if y % 2:
        y = max(0, y - 1)
    return Rect(x, y, crop_width, crop_height)


def compute_layout(source_width: int, source_height: int, aspect: str, mode: str,
                   crop_x: float = 0.5, crop_y: float = 0.5) -> Layout:
    source_width = max(2, int(source_width))
    source_height = max(2, int(source_height))
    mode = normalize_mode(aspect, mode)
    canvas_width, canvas_height = canvas_dimensions(aspect, source_width, source_height)
    foreground = _contain(source_width, source_height, canvas_width, canvas_height)
    background = _cover(source_width, source_height, canvas_width, canvas_height)

    if mode == "crop":
        target_ratio = canvas_width / canvas_height
        source_ratio = source_width / source_height
        if source_ratio >= target_ratio:
            crop_height = source_height
            crop_width = even(source_height * target_ratio)
        else:
            crop_width = source_width
            crop_height = even(source_width / target_ratio)
        crop = _even_rect(source_width, source_height, crop_width, crop_height, crop_x, crop_y)
    else:
        crop = _even_rect(source_width, source_height, source_width, source_height, 0.5, 0.5)

    return Layout(
        aspect=aspect,
        mode=mode,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
        foreground=foreground,
        background=background,
        crop=crop,
        has_horizontal_crop=crop.width < source_width,
        has_vertical_crop=crop.height < source_height,
    )


def build_filter_graph(layout: Layout, captions_filename: str | None = None) -> str:
    canvas_width = layout.canvas_width
    canvas_height = layout.canvas_height
    parts: list[str] = []

    if layout.mode == "full_frame":
        background = layout.background
        foreground = layout.foreground
        parts.append("[0:v:0]split=2[bg][fg]")
        parts.append(
            f"[bg]scale={background.width}:{background.height}:flags=bicubic,"
            f"crop={canvas_width}:{canvas_height},"
            "boxblur=luma_radius=28:luma_power=2,"
            "eq=brightness=-0.30:saturation=0.82[bgv]"
        )
        parts.append(f"[fg]scale={foreground.width}:{foreground.height}:flags=lanczos[fgv]")
        parts.append(
            f"[bgv][fgv]overlay=x={foreground.x}:y={foreground.y}:format=auto[base]"
        )
    elif layout.mode == "crop":
        crop = layout.crop
        parts.append(
            f"[0:v:0]crop={crop.width}:{crop.height}:{crop.x}:{crop.y},"
            f"scale={canvas_width}:{canvas_height}:flags=lanczos[base]"
        )
    else:
        foreground = layout.foreground
        parts.append(
            f"[0:v:0]scale={foreground.width}:{foreground.height}:flags=lanczos,"
            f"pad={canvas_width}:{canvas_height}:{foreground.x}:{foreground.y}:color=0x0b0c0a[base]"
        )

    if captions_filename:
        parts.append(f"[base]subtitles=filename={captions_filename}[captioned]")
        base = "captioned"
    else:
        base = "base"
    parts.append(f"[{base}]setsar=1,format=yuv420p[vout]")
    return ";".join(parts)
