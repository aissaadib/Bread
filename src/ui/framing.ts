import type { FramingAspect, FramingMode } from "./types";

/**
 * TypeScript mirror of worker/luna_worker/framing.py.
 *
 * Preview and export must agree on the canvas, the source rectangle, and the
 * foreground placement. Keep both implementations in sync when changing the
 * composition rules.
 */

export type Rect = { x: number; y: number; width: number; height: number };

export type Layout = {
  aspect: FramingAspect;
  mode: FramingMode;
  canvasWidth: number;
  canvasHeight: number;
  foreground: Rect;
  background: Rect;
  crop: Rect;
  hasHorizontalCrop: boolean;
  hasVerticalCrop: boolean;
};

export const CANVAS_DIMENSIONS: Record<Exclude<FramingAspect, "original">, [number, number]> = {
  vertical: [1080, 1920],
  square: [1080, 1080],
};

export function clampUnit(value: number): number {
  return Math.min(1, Math.max(0, Number.isFinite(value) ? value : 0));
}

function even(value: number): number {
  return Math.max(2, Math.round(value / 2) * 2);
}

export function normalizeMode(aspect: FramingAspect, mode: FramingMode): FramingMode {
  if (aspect === "original") return "original";
  return mode;
}

function contain(sourceWidth: number, sourceHeight: number, canvasWidth: number, canvasHeight: number): Rect {
  let width: number;
  let height: number;
  if (sourceWidth / sourceHeight >= canvasWidth / canvasHeight) {
    width = canvasWidth;
    height = Math.min(canvasHeight, even((canvasWidth * sourceHeight) / sourceWidth));
  } else {
    height = canvasHeight;
    width = Math.min(canvasWidth, even((canvasHeight * sourceWidth) / sourceHeight));
  }
  return { x: Math.floor((canvasWidth - width) / 2), y: Math.floor((canvasHeight - height) / 2), width, height };
}

function cover(sourceWidth: number, sourceHeight: number, canvasWidth: number, canvasHeight: number): Rect {
  let width: number;
  let height: number;
  if (sourceWidth / sourceHeight >= canvasWidth / canvasHeight) {
    height = canvasHeight;
    width = even((canvasHeight * sourceWidth) / sourceHeight);
  } else {
    width = canvasWidth;
    height = even((canvasWidth * sourceHeight) / sourceWidth);
  }
  return { x: Math.floor((canvasWidth - width) / 2), y: Math.floor((canvasHeight - height) / 2), width, height };
}

function evenRect(
  sourceWidth: number,
  sourceHeight: number,
  cropWidth: number,
  cropHeight: number,
  cropX: number,
  cropY: number,
): Rect {
  const width = Math.min(sourceWidth, Math.max(2, cropWidth - (cropWidth % 2)));
  const height = Math.min(sourceHeight, Math.max(2, cropHeight - (cropHeight % 2)));
  let x = Math.round((sourceWidth - width) * clampUnit(cropX));
  let y = Math.round((sourceHeight - height) * clampUnit(cropY));
  x = Math.min(Math.max(0, x), sourceWidth - width);
  y = Math.min(Math.max(0, y), sourceHeight - height);
  if (x % 2) x = Math.max(0, x - 1);
  if (y % 2) y = Math.max(0, y - 1);
  return { x, y, width, height };
}

export function computeLayout(
  sourceWidth: number,
  sourceHeight: number,
  aspect: FramingAspect,
  requestedMode: FramingMode,
  cropX = 0.5,
  cropY = 0.5,
): Layout {
  const width = Math.max(2, Math.round(sourceWidth));
  const height = Math.max(2, Math.round(sourceHeight));
  const mode = normalizeMode(aspect, requestedMode);
  const [canvasWidth, canvasHeight] =
    aspect === "original" ? [even(width), even(height)] : CANVAS_DIMENSIONS[aspect];
  const foreground = contain(width, height, canvasWidth, canvasHeight);
  const background = cover(width, height, canvasWidth, canvasHeight);

  let crop: Rect;
  if (mode === "crop") {
    const targetRatio = canvasWidth / canvasHeight;
    const sourceRatio = width / height;
    let cropWidth: number;
    let cropHeight: number;
    if (sourceRatio >= targetRatio) {
      cropHeight = height;
      cropWidth = even(height * targetRatio);
    } else {
      cropWidth = width;
      cropHeight = even(width / targetRatio);
    }
    crop = evenRect(width, height, cropWidth, cropHeight, cropX, cropY);
  } else {
    crop = evenRect(width, height, width, height, 0.5, 0.5);
  }

  return {
    aspect,
    mode,
    canvasWidth,
    canvasHeight,
    foreground,
    background,
    crop,
    hasHorizontalCrop: crop.width < width,
    hasVerticalCrop: crop.height < height,
  };
}
