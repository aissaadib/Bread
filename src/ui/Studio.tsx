import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { convertFileSrc, invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { open as openDialog } from "@tauri-apps/plugin-dialog";
import { computeLayout } from "./framing";
import type {
  CaptionData,
  ExportEvent,
  ExportRecord,
  FramingAspect,
  FramingMode,
  MediaInfo,
  StartExportResult,
  StudioClip,
} from "./types";

const MIN_CLIP_MS = 1_000;
const MAX_CLIP_MS = 180_000;
const PEAK_PADDING_MS = 300;

const ASPECT_OPTIONS: Array<{ value: FramingAspect; label: string }> = [
  { value: "vertical", label: "Reels / Shorts" },
  { value: "square", label: "Square 1:1" },
  { value: "original", label: "Original" },
];

// Fill-screen cropping is first because it is the only mode that produces a
// genuine full-frame Reels/Shorts composition from landscape gameplay.
const MODE_OPTIONS: Array<{ value: FramingMode; label: string; hint: string }> = [
  { value: "crop", label: "Fill screen", hint: "Crops the sides so gameplay fills the whole 9:16 canvas" },
  { value: "full_frame", label: "Preserve full", hint: "Keeps every edge of the gameplay over a blurred cover" },
  { value: "original", label: "Original", hint: "Keeps the source shape inside a dark background" },
];

const ASPECT_LABELS: Record<string, string> = {
  vertical: "Reels / Shorts · 1080 × 1920",
  square: "Square · 1080 × 1080",
  original: "Original resolution",
};

const MODE_LABELS: Record<string, string> = {
  crop: "Fill screen",
  full_frame: "Preserve full gameplay",
  original: "Original",
};

type StudioProps = {
  jobId: string;
  sourcePath: string;
  media: MediaInfo;
  clips: StudioClip[];
  initialPositionMs?: number;
  onClose: () => void;
  onExportingChange?: (active: boolean) => void;
};

function friendlyError(reason: unknown): string {
  return typeof reason === "string" ? reason : "Something went wrong. Please try again.";
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

function formatTime(milliseconds: number): string {
  const total = Math.max(0, milliseconds);
  const hours = Math.floor(total / 3_600_000);
  const minutes = Math.floor((total % 3_600_000) / 60_000);
  const seconds = Math.floor((total % 60_000) / 1_000);
  const tenths = Math.floor((total % 1_000) / 100);
  if (hours) return `${hours}:${pad(minutes)}:${pad(seconds)}`;
  return `${pad(minutes)}:${pad(seconds)}.${tenths}`;
}

function formatSeconds(milliseconds: number): string {
  return `${(Math.max(0, milliseconds) / 1000).toFixed(1)} s`;
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(Math.max(value, minimum), maximum);
}

function captionTextAt(segments: CaptionData["segments"], seconds: number): string {
  const match = segments.find(
    (segment) => seconds >= segment.start_seconds && seconds <= segment.end_seconds,
  );
  return match?.text?.trim() ?? "";
}

function exportStatusLabel(record: ExportRecord): string {
  switch (record.status) {
    case "completed":
      return "Saved";
    case "failed":
      return "Failed";
    case "cancelled":
      return "Cancelled";
    case "cancelling":
      return "Cancelling";
    case "queued":
      return "Queued";
    case "finalizing":
      return "Finalizing";
    case "preparing":
      return "Preparing";
    case "rendering":
      return `${Math.round(record.progress)}%`;
    default:
      return record.status;
  }
}

function exportIsActive(record: ExportRecord): boolean {
  return ["queued", "preparing", "rendering", "finalizing", "cancelling"].includes(record.status);
}

export default function Studio({
  jobId,
  sourcePath,
  media,
  clips: initialClips,
  initialPositionMs,
  onClose,
  onExportingChange,
}: StudioProps) {
  const [clips, setClips] = useState<StudioClip[]>(initialClips);
  const [activeIndex, setActiveIndex] = useState(0);
  const [aspect, setAspect] = useState<FramingAspect>("vertical");
  const [mode, setMode] = useState<FramingMode>("crop");
  const [cropX, setCropX] = useState(0.5);
  const [cropY, setCropY] = useState(0.5);
  const [crf, setCrf] = useState(21);
  const [captionsEnabled, setCaptionsEnabled] = useState(false);
  const [captionData, setCaptionData] = useState<CaptionData>({
    available: false,
    hasTranscript: false,
    segments: [],
  });
  const [outputDir, setOutputDir] = useState("");
  const [exports, setExports] = useState<ExportRecord[]>([]);
  const [activeBatchId, setActiveBatchId] = useState<string | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewReady, setPreviewReady] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [playheadMs, setPlayheadMs] = useState(initialClips[0]?.startMs ?? 0);

  const videoRef = useRef<HTMLVideoElement | null>(null);
  const backgroundRef = useRef<HTMLVideoElement | null>(null);
  const cropMapRef = useRef<HTMLVideoElement | null>(null);
  const initialPositionRef = useRef(initialPositionMs ?? initialClips[0]?.startMs ?? 0);
  const activeBatchRef = useRef<string | null>(null);

  const activeClip = clips[Math.min(activeIndex, clips.length - 1)];
  const durationMs = Math.max(MIN_CLIP_MS, Math.round(media.durationSeconds * 1_000));
  const clipDuration = activeClip.endMs - activeClip.startMs;
  const sourceUrl = convertFileSrc(sourcePath);
  const sourceWidth = media.displayWidth ?? media.width ?? 1080;
  const sourceHeight = media.displayHeight ?? media.height ?? 1920;

  const layout = useMemo(
    () => computeLayout(sourceWidth, sourceHeight, aspect, mode, cropX, cropY),
    [sourceWidth, sourceHeight, aspect, mode, cropX, cropY],
  );

  const effectiveMode: FramingMode = aspect === "original" ? "original" : layout.mode;
  const cropAvailable = aspect !== "original" && effectiveMode === "crop";
  const ratio = layout.canvasWidth / layout.canvasHeight;
  const previewWidth = Math.max(180, Math.min(560, Math.round(500 * ratio)));
  const previewHeight = previewWidth / ratio;

  const maxStart = Math.max(
    0,
    Math.min(activeClip.peakMs - PEAK_PADDING_MS, activeClip.endMs - MIN_CLIP_MS, durationMs - MIN_CLIP_MS),
  );
  const minEnd = Math.min(
    durationMs,
    Math.max(activeClip.peakMs + PEAK_PADDING_MS, activeClip.startMs + MIN_CLIP_MS),
  );
  const maxEnd = Math.min(durationMs, activeClip.startMs + MAX_CLIP_MS);

  const refreshExports = useCallback(async () => {
    try {
      setExports(await invoke<ExportRecord[]>("list_exports", { jobId, limit: 50 }));
    } catch {
      // Export history is informational; a transient read failure does not block exporting.
    }
  }, [jobId]);

  useEffect(() => {
    let disposed = false;
    void invoke<string>("default_export_dir", { sourcePath })
      .then((folder) => {
        if (!disposed) setOutputDir(folder);
      })
      .catch((reason) => {
        if (!disposed) setExportError(friendlyError(reason));
      });
    void refreshExports();
    return () => {
      disposed = true;
    };
  }, [refreshExports, sourcePath]);

  useEffect(() => {
    let disposed = false;
    let unlisten: (() => void) | undefined;
    void listen<ExportEvent>("export-event", ({ payload }) => {
      if (payload.batchId) {
        if (!activeBatchRef.current) activeBatchRef.current = payload.batchId;
        else if (activeBatchRef.current !== payload.batchId) return;
      }
      if (payload.itemId && payload.type.startsWith("item_")) {
        setExports((current) =>
          current.map((record) => {
            if (record.id !== payload.itemId) return record;
            switch (payload.type) {
              case "item_started":
                return {
                  ...record,
                  status: "preparing",
                  stage: payload.stage ?? "preparing",
                  progress: 0,
                  outputPath: payload.outputPath ?? record.outputPath,
                };
              case "item_progress":
                return {
                  ...record,
                  status: "rendering",
                  stage: "rendering",
                  progress: Math.round(clamp(payload.progress ?? record.progress, 0, 100)),
                };
              case "item_stage":
                return {
                  ...record,
                  status: payload.stage === "finalizing" ? "finalizing" : "rendering",
                  stage: payload.stage ?? record.stage,
                };
              case "item_completed":
                return {
                  ...record,
                  status: "completed",
                  stage: "completed",
                  progress: 100,
                  outputPath: payload.outputPath ?? record.outputPath,
                  errorMessage: null,
                };
              case "item_failed":
                return {
                  ...record,
                  status: "failed",
                  stage: "failed",
                  errorMessage: payload.message ?? "The clip could not be rendered.",
                };
              case "item_cancelled":
                return { ...record, status: "cancelled", stage: "cancelled" };
              default:
                return record;
            }
          }),
        );
      }
      if (payload.type === "error") {
        activeBatchRef.current = null;
        setActiveBatchId(null);
        setExportError(payload.message ?? "The export failed.");
        void refreshExports();
      }
      if (payload.type === "batch_completed") {
        activeBatchRef.current = null;
        setActiveBatchId(null);
        if (payload.cancelled) {
          setNotice("Export cancelled. Any finished clips are still available below.");
        } else if ((payload.failed ?? 0) > 0) {
          setNotice(`${payload.completed ?? 0} exported, ${payload.failed ?? 0} failed. Review the list below.`);
        } else {
          setNotice("Export finished. Open the file from the list below.");
        }
        void refreshExports();
      }
    }).then((stop) => {
      if (disposed) stop();
      else unlisten = stop;
    });
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [refreshExports]);

  useEffect(() => {
    let disposed = false;
    const handle = window.setTimeout(() => {
      void invoke<CaptionData>("get_caption_data", {
        jobId,
        startMs: activeClip.startMs,
        endMs: activeClip.endMs,
      })
        .then((data) => {
          if (!disposed) setCaptionData(data);
        })
        .catch(() => {
          if (!disposed) setCaptionData({ available: false, hasTranscript: false, segments: [] });
        });
    }, 180);
    return () => {
      disposed = true;
      window.clearTimeout(handle);
    };
  }, [jobId, activeClip.startMs, activeClip.endMs]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const currentMs = video.currentTime * 1_000;
    if (currentMs < activeClip.startMs || currentMs >= activeClip.endMs) {
      video.currentTime = activeClip.startMs / 1_000;
      if (backgroundRef.current) backgroundRef.current.currentTime = video.currentTime;
      setPlayheadMs(activeClip.startMs);
    }
  }, [activeIndex, activeClip.startMs, activeClip.endMs]);

  useEffect(() => {
    const video = videoRef.current;
    const background = backgroundRef.current;
    if (video && background) {
      background.currentTime = video.currentTime;
      if (!video.paused) void background.play().catch(() => undefined);
    }
  }, [effectiveMode]);

  useEffect(() => {
    onExportingChange?.(Boolean(activeBatchId));
  }, [activeBatchId, onExportingChange]);

  useEffect(() => {
    return () => {
      onExportingChange?.(false);
      videoRef.current?.pause();
      backgroundRef.current?.pause();
    };
  }, [onExportingChange]);

  function updateActiveClip(patch: Partial<StudioClip>) {
    setClips((current) =>
      current.map((clip, index) => (index === activeIndex ? { ...clip, ...patch } : clip)),
    );
  }

  function changeStart(value: number) {
    const nextStart = clamp(Math.round(value), 0, maxStart);
    const nextEnd = Math.max(
      nextStart + MIN_CLIP_MS,
      Math.min(activeClip.endMs, nextStart + MAX_CLIP_MS, durationMs),
    );
    updateActiveClip({ startMs: nextStart, endMs: nextEnd });
    if (playheadMs < nextStart || playheadMs >= nextEnd) seekTo(nextStart);
  }

  function changeEnd(value: number) {
    const nextEnd = clamp(Math.round(value), minEnd, maxEnd);
    updateActiveClip({ endMs: nextEnd });
    if (playheadMs < activeClip.startMs || playheadMs >= nextEnd) seekTo(nextEnd - 50);
  }

  function resetBoundaries() {
    updateActiveClip({
      startMs: activeClip.detectedStartMs,
      endMs: activeClip.detectedEndMs,
    });
    seekTo(activeClip.detectedStartMs);
  }

  function seekTo(milliseconds: number) {
    const target = clamp(milliseconds, activeClip.startMs, Math.max(activeClip.startMs, activeClip.endMs - 30));
    const video = videoRef.current;
    if (video) video.currentTime = target / 1_000;
    if (backgroundRef.current) backgroundRef.current.currentTime = target / 1_000;
    setPlayheadMs(target);
  }

  function togglePlayback() {
    const video = videoRef.current;
    if (!video) return;
    if (!video.paused) {
      video.pause();
      backgroundRef.current?.pause();
      return;
    }
    const currentMs = video.currentTime * 1_000;
    if (currentMs < activeClip.startMs || currentMs >= activeClip.endMs - 30) {
      video.currentTime = activeClip.startMs / 1_000;
      if (backgroundRef.current) backgroundRef.current.currentTime = video.currentTime;
      setPlayheadMs(activeClip.startMs);
    }
    void video.play().catch(() => {
      setPreviewError("Preview playback was blocked. Scrub the timeline to inspect the clip.");
    });
    if (backgroundRef.current) {
      backgroundRef.current.currentTime = video.currentTime;
      void backgroundRef.current.play().catch(() => undefined);
    }
  }

  function handleLoaded() {
    setPreviewReady(true);
    const target = clamp(
      initialPositionRef.current,
      activeClip.startMs,
      Math.max(activeClip.startMs, activeClip.endMs - 50),
    );
    const video = videoRef.current;
    if (video) {
      video.currentTime = target / 1_000;
      if (backgroundRef.current) backgroundRef.current.currentTime = video.currentTime;
    }
    setPlayheadMs(target);
  }

  function handleTimeUpdate() {
    const video = videoRef.current;
    if (!video) return;
    const currentMs = video.currentTime * 1_000;
    if (currentMs >= activeClip.endMs) {
      video.pause();
      backgroundRef.current?.pause();
      setPlayheadMs(activeClip.endMs);
      return;
    }
    if (currentMs >= activeClip.startMs) setPlayheadMs(currentMs);
    const background = backgroundRef.current;
    if (background && Math.abs(background.currentTime - video.currentTime) > 0.35) {
      background.currentTime = video.currentTime;
    }
  }

  function selectClip(index: number) {
    if (index === activeIndex) return;
    videoRef.current?.pause();
    backgroundRef.current?.pause();
    setPlaying(false);
    setActiveIndex(index);
    setPlayheadMs(clips[index].startMs);
  }

  function chooseAspect(next: FramingAspect) {
    setAspect(next);
    if (next === "original") {
      setMode("original");
      return;
    }
    // Returning from Original must land on a real short-form composition,
    // not on a leftover letterbox.
    if (mode === "original") setMode("crop");
  }

  async function chooseFolder() {
    try {
      const selection = await openDialog({ directory: true, multiple: false });
      if (typeof selection === "string") setOutputDir(selection);
    } catch (reason) {
      setExportError(friendlyError(reason));
    }
  }

  async function startExport() {
    setExportError(null);
    setNotice(null);
    if (activeBatchRef.current) return;
    if (!outputDir.trim()) {
      setExportError("Choose an export folder first.");
      return;
    }
    if (clips.length > 50) {
      setExportError("A single export batch is limited to 50 clips.");
      return;
    }
    for (const clip of clips) {
      const length = clip.endMs - clip.startMs;
      if (length < MIN_CLIP_MS) {
        setExportError(`“${clip.title}” must be at least 1 second long.`);
        return;
      }
      if (length > MAX_CLIP_MS) {
        setExportError(`“${clip.title}” is longer than the 3 minute limit.`);
        return;
      }
      if (clip.peakMs < clip.startMs || clip.peakMs > clip.endMs) {
        setExportError(`“${clip.title}” must keep its detected peak inside the clip.`);
        return;
      }
    }
    if (captionsEnabled && !captionData.hasTranscript) {
      setExportError(
        "Captions are unavailable for this recording because no speech transcript was saved. Export without captions.",
      );
      return;
    }
    try {
      const result = await invoke<StartExportResult>("start_short_export", {
        request: {
          jobId,
          aspect,
          mode: effectiveMode,
          cropX,
          cropY,
          captions: captionsEnabled,
          crf,
          outputDir,
          items: clips.map((clip) => ({
            candidateId: clip.candidateId,
            peakMs: clip.peakMs,
            startMs: clip.startMs,
            endMs: clip.endMs,
            title: clip.title,
          })),
        },
      });
      activeBatchRef.current = result.batchId;
      setActiveBatchId(result.batchId);
      setExports((current) => [
        ...result.exports,
        ...current.filter((record) => !result.exports.some((next) => next.id === record.id)),
      ]);
    } catch (reason) {
      setExportError(friendlyError(reason));
    }
  }

  async function cancelExport() {
    if (!activeBatchId) return;
    try {
      await invoke("cancel_short_export", { batchId: activeBatchId });
      setNotice("Cancelling the export safely…");
    } catch (reason) {
      setExportError(friendlyError(reason));
    }
  }

  async function retryExport(exportId: string) {
    setExportError(null);
    setNotice(null);
    try {
      const result = await invoke<StartExportResult>("retry_short_export", { exportId });
      activeBatchRef.current = result.batchId;
      setActiveBatchId(result.batchId);
      setExports((current) => [
        ...result.exports,
        ...current.filter((record) => !result.exports.some((next) => next.id === record.id)),
      ]);
    } catch (reason) {
      setExportError(friendlyError(reason));
    }
  }

  async function openExportFile(exportId: string) {
    try {
      await invoke("open_short_export", { exportId });
    } catch (reason) {
      setExportError(friendlyError(reason));
    }
  }

  async function revealExportFile(exportId: string) {
    try {
      await invoke("reveal_short_export", { exportId });
    } catch (reason) {
      setExportError(friendlyError(reason));
    }
  }

  useEffect(() => {
    const map = cropMapRef.current;
    if (!map) return;
    const target = playheadMs / 1_000;
    if (Math.abs(map.currentTime - target) > 1) {
      map.currentTime = target;
    }
  }, [playheadMs]);

  const cropKeptPercent = Math.round(
    ((layout.crop.width * layout.crop.height) / Math.max(1, sourceWidth * sourceHeight)) * 100,
  );
  const foregroundStyle = {
    left: `${(layout.foreground.x / layout.canvasWidth) * 100}%`,
    top: `${(layout.foreground.y / layout.canvasHeight) * 100}%`,
    width: `${(layout.foreground.width / layout.canvasWidth) * 100}%`,
    height: `${(layout.foreground.height / layout.canvasHeight) * 100}%`,
  };
  const cropSourceStyle = {
    left: `${(-layout.crop.x / layout.crop.width) * 100}%`,
    top: `${(-layout.crop.y / layout.crop.height) * 100}%`,
    width: `${(sourceWidth / layout.crop.width) * 100}%`,
    height: `${(sourceHeight / layout.crop.height) * 100}%`,
  };
  const videoStyle = effectiveMode === "crop" ? cropSourceStyle : foregroundStyle;
  const captionText =
    captionsEnabled && captionData.available
      ? captionTextAt(captionData.segments, playheadMs / 1_000)
      : "";
  const captionStyle = {
    fontSize: `${Math.max(10, Math.round(previewHeight * 0.04))}px`,
    bottom: `${Math.round(previewHeight * 0.085)}px`,
  };
  const bandLeft = (activeClip.startMs / durationMs) * 100;
  const bandWidth = (clipDuration / durationMs) * 100;
  const peakLeft = (activeClip.peakMs / durationMs) * 100;
  const playheadLeft = (clamp(playheadMs, 0, durationMs) / durationMs) * 100;
  const detectedLeft = (activeClip.detectedStartMs / durationMs) * 100;
  const detectedWidth = ((activeClip.detectedEndMs - activeClip.detectedStartMs) / durationMs) * 100;
  const batchRecords = exports.filter((record) => record.batchId === activeBatchId);
  const batchProgress = batchRecords.length
    ? batchRecords.reduce((sum, record) => sum + record.progress, 0) / batchRecords.length
    : 0;
  const detectingPeak = activeClip.peakMs;

  return (
    <section className="studio-section" aria-labelledby="studio-title">
      <div className="section-heading studio-heading">
        <div className="section-title-wrap">
          <span className="step-number">04</span>
          <div>
            <p className="eyebrow">SHORT-FORM STUDIO</p>
            <h2 id="studio-title">Shape your clip</h2>
          </div>
        </div>
        <div className="studio-heading-actions">
          <span className="format-note studio-source-name" title={media.name}>{media.name}</span>
          <button
            className="text-button"
            type="button"
            onClick={onClose}
            disabled={!!activeBatchId}
          >
            Close studio
          </button>
        </div>
      </div>

      {clips.length > 1 && (
        <div className="studio-queue">
          <span className="eyebrow">BATCH QUEUE · {clips.length} CLIPS</span>
          <div className="queue-list">
            {clips.map((clip, index) => (
              <button
                className={`queue-item ${index === activeIndex ? "selected" : ""}`}
                type="button"
                key={clip.key}
                disabled={!!activeBatchId}
                onClick={() => selectClip(index)}
              >
                <span className="queue-number">{pad(index + 1)}</span>
                <strong>{clip.title}</strong>
                <small>{formatSeconds(clip.endMs - clip.startMs)}</small>
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="studio-grid">
        <div className="studio-preview-column">
          <div className="preview-viewport">
            <div
              className={`preview-frame ${effectiveMode === "original" ? "preview-letterbox" : ""}`}
              style={{ width: `${previewWidth}px`, aspectRatio: `${layout.canvasWidth} / ${layout.canvasHeight}` }}
            >
              {effectiveMode === "full_frame" && (
                <video
                  ref={backgroundRef}
                  className="preview-video preview-background"
                  src={sourceUrl}
                  muted
                  playsInline
                  preload="auto"
                  aria-hidden="true"
                  tabIndex={-1}
                />
              )}
              <video
                ref={videoRef}
                className="preview-video"
                style={videoStyle}
                src={sourceUrl}
                playsInline
                preload="auto"
                onLoadedMetadata={handleLoaded}
                onTimeUpdate={handleTimeUpdate}
                onPlay={() => setPlaying(true)}
                onPause={() => setPlaying(false)}
                onError={() => setPreviewError("This recording cannot be previewed in the app. It can still be exported.")}
              />
              {!previewReady && !previewError && <span className="preview-loading">Loading preview…</span>}
              {previewError && <span className="preview-warning">{previewError}</span>}
              <span className="preview-badge">
                {ASPECT_LABELS[aspect]} · {MODE_LABELS[effectiveMode]}
                {aspect !== "original" && effectiveMode !== "crop" ? " (letterboxed)" : ""}
              </span>
              {captionText && (
                <p className="preview-caption" style={captionStyle}>{captionText}</p>
              )}
            </div>
          </div>

          <div className="preview-controls">
            <button
              className="play-button"
              type="button"
              onClick={togglePlayback}
              disabled={!!previewError}
              aria-label={playing ? "Pause preview" : "Play preview"}
            >
              {playing ? "❚❚" : "▶"}
            </button>
            <div className="preview-time">
              <strong>{formatTime(playheadMs)}</strong>
              <span>/ {formatTime(activeClip.endMs)}</span>
            </div>
            <input
              className="scrub"
              type="range"
              min={0}
              max={Math.max(1, clipDuration - 50)}
              step={50}
              value={clamp(playheadMs - activeClip.startMs, 0, Math.max(1, clipDuration - 50))}
              onChange={(event) => seekTo(activeClip.startMs + Number(event.target.value))}
              aria-label="Preview position"
            />
          </div>
          <p className="preview-note">
            Preview plays the original recording with the selected framing. Export writes a new MP4 and never changes the source.
          </p>
        </div>

        <div className="studio-controls">
          <div className="control-block">
            <div className="control-label"><span>Output format</span><small>{ASPECT_LABELS[aspect]}</small></div>
            <div className="segmented">
              {ASPECT_OPTIONS.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  className={aspect === option.value ? "selected" : ""}
                  disabled={!!activeBatchId}
                  onClick={() => chooseAspect(option.value)}
                >
                  {option.label}
                </button>
              ))}
            </div>
          </div>

          <div className="control-block">
            <div className="control-label"><span>Presentation</span><small>{MODE_OPTIONS.find((option) => option.value === effectiveMode)?.hint}</small></div>
            <div className="segmented">
              {MODE_OPTIONS.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  className={effectiveMode === option.value ? "selected" : ""}
                  disabled={aspect === "original" || !!activeBatchId}
                  onClick={() => setMode(option.value)}
                >
                  {option.label}
                </button>
              ))}
            </div>
            {aspect === "original" && <p className="control-hint">Original format keeps the source resolution and framing.</p>}
          </div>

          <div className={`control-block ${cropAvailable ? "" : "is-muted"}`}>
            <div className="control-label">
              <span>Framing window</span>
              <div className="framing-shortcuts">
                <button className="text-button" type="button" disabled={!cropAvailable || !!activeBatchId} onClick={() => setCropX(0)}>Left</button>
                <button className="text-button" type="button" disabled={!cropAvailable || !!activeBatchId} onClick={() => { setCropX(0.5); setCropY(0.5); }}>Center</button>
                <button className="text-button" type="button" disabled={!cropAvailable || !!activeBatchId} onClick={() => setCropX(1)}>Right</button>
              </div>
            </div>
            {cropAvailable && (
              <div className="crop-map">
                <video
                  ref={cropMapRef}
                  className="crop-map-video"
                  src={sourceUrl}
                  muted
                  playsInline
                  preload="metadata"
                  aria-hidden="true"
                  tabIndex={-1}
                />
                <span
                  className="crop-map-window"
                  style={{
                    left: `${(layout.crop.x / sourceWidth) * 100}%`,
                    top: `${(layout.crop.y / sourceHeight) * 100}%`,
                    width: `${(layout.crop.width / sourceWidth) * 100}%`,
                    height: `${(layout.crop.height / sourceHeight) * 100}%`,
                  }}
                />
                <span className="crop-map-label">
                  KEPT {layout.crop.width}×{layout.crop.height} · {cropKeptPercent}% OF FRAME
                </span>
              </div>
            )}
            <label className="slider-label"><span>Horizontal</span><output>{Math.round(cropX * 100)}%</output></label>
            <input
              type="range" min={0} max={1} step={0.01} value={cropX}
              disabled={!cropAvailable || !!activeBatchId}
              onChange={(event) => setCropX(Number(event.target.value))}
              aria-label="Horizontal crop position"
            />
            <label className="slider-label"><span>Vertical</span><output>{Math.round(cropY * 100)}%</output></label>
            <input
              type="range" min={0} max={1} step={0.01} value={cropY}
              disabled={!cropAvailable || !!activeBatchId}
              onChange={(event) => setCropY(Number(event.target.value))}
              aria-label="Vertical crop position"
            />
            <p className="control-hint">
              {cropAvailable
                ? "The dimmed area is cropped away; the bright window is what the exported 9:16 video shows. Nothing is stretched."
                : "Choose Fill screen to move the framing window. Preserve full keeps the entire gameplay frame."}
            </p>
            {activeClip.aiFocusX != null && activeClip.aiFocusY != null && (
              <div className="ai-focus">
                <button
                  className="ghost-button"
                  type="button"
                  disabled={!!activeBatchId}
                  onClick={() => {
                    setAspect("vertical");
                    setMode("crop");
                    setCropX(clamp(activeClip.aiFocusX ?? 0.5, 0, 1));
                    setCropY(clamp(activeClip.aiFocusY ?? 0.5, 0, 1));
                  }}
                >
                  Use AI framing · {Math.round((activeClip.aiFocusX ?? 0.5) * 100)}% / {Math.round((activeClip.aiFocusY ?? 0.5) * 100)}%
                </button>
                <span className="control-hint">Gemini suggested this focus point. It is a suggestion, not tracking — adjust it freely.</span>
              </div>
            )}
          </div>

          <div className="control-block">
            <div className="control-label">
              <span>Clip boundaries</span>
              <button
                className="text-button"
                type="button"
                onClick={resetBoundaries}
                disabled={!!activeBatchId || (activeClip.startMs === activeClip.detectedStartMs && activeClip.endMs === activeClip.detectedEndMs)}
              >
                Reset
              </button>
            </div>
            <div className="boundary-summary">
              <strong>{formatSeconds(clipDuration)}</strong>
              <span>{formatTime(activeClip.startMs)} – {formatTime(activeClip.endMs)}</span>
              <span className="peak-tag">PEAK {formatTime(detectingPeak)}</span>
            </div>
            <div className="timeline-track" aria-hidden="true">
              <span className="timeline-detected" style={{ left: `${detectedLeft}%`, width: `${detectedWidth}%` }} />
              <span className="timeline-band" style={{ left: `${bandLeft}%`, width: `${bandWidth}%` }} />
              <span className="timeline-peak" style={{ left: `${peakLeft}%` }} />
              <span className="timeline-playhead" style={{ left: `${playheadLeft}%` }} />
            </div>
            <label className="slider-label"><span>Start</span><output>{formatTime(activeClip.startMs)}</output></label>
            <input
              type="range" min={0} max={Math.max(0, maxStart)} step={50} value={activeClip.startMs}
              disabled={!!activeBatchId}
              onChange={(event) => changeStart(Number(event.target.value))}
              aria-label="Clip start"
            />
            <label className="slider-label"><span>End</span><output>{formatTime(activeClip.endMs)}</output></label>
            <input
              type="range" min={minEnd} max={Math.max(minEnd, maxEnd)} step={50} value={activeClip.endMs}
              disabled={!!activeBatchId}
              onChange={(event) => changeEnd(Number(event.target.value))}
              aria-label="Clip end"
            />
            <p className="control-hint">The detected peak stays inside the clip. Clips run from 1 second to 3 minutes.</p>
          </div>

          <div className="control-block">
            <div className="control-label">
              <span>Captions</span>
              <span className={`status-chip ${captionData.hasTranscript ? (captionData.available ? "is-ready" : "is-empty") : "is-off"}`}>
                {captionData.hasTranscript ? (captionData.available ? "SPEECH FOUND" : "NO SPEECH IN CLIP") : "UNAVAILABLE"}
              </span>
            </div>
            <label className={`toggle-row ${captionData.hasTranscript ? "" : "is-disabled"}`}>
              <input
                type="checkbox"
                checked={captionsEnabled}
                disabled={!captionData.hasTranscript || !!activeBatchId}
                onChange={(event) => setCaptionsEnabled(event.target.checked)}
              />
              <span>
                <strong>Burn in captions</strong>
                <small>
                  {captionData.hasTranscript
                    ? captionData.available
                      ? "Captions come from the saved local speech transcript."
                      : "No speech was detected in this range, so the export will have no captions."
                    : "Captions are unavailable because this analysis has no saved speech transcript."}
                </small>
              </span>
            </label>
          </div>

          <div className="control-block export-block">
            <div className="control-label"><span>Export</span><small>{clips.length} {clips.length === 1 ? "clip" : "clips"}</small></div>
            <div className="folder-row">
              <span className="folder-path" title={outputDir}>{outputDir || "No export folder selected"}</span>
              <button className="text-button" type="button" onClick={() => void chooseFolder()} disabled={!!activeBatchId}>Change</button>
            </div>
            <label className="slider-label"><span>Quality · CRF {crf}</span><output>{crf <= 17 ? "High" : crf <= 22 ? "Balanced" : "Small file"}</output></label>
            <input
              type="range" min={14} max={28} step={1} value={crf}
              disabled={!!activeBatchId}
              onChange={(event) => setCrf(Number(event.target.value))}
              aria-label="Video quality"
            />
            <div className="export-actions">
              <button
                className="start-button studio-export-button"
                type="button"
                onClick={() => void startExport()}
                disabled={!!activeBatchId || !outputDir}
              >
                {activeBatchId ? "Exporting…" : `Export ${clips.length} ${clips.length === 1 ? "short" : "shorts"}`}
                <span aria-hidden="true">{activeBatchId ? "···" : "↗"}</span>
              </button>
              {activeBatchId && <button className="cancel-button" type="button" onClick={() => void cancelExport()}>Stop</button>}
            </div>
            {activeBatchId && (
              <div className="batch-progress">
                <div className="progress-track"><span style={{ width: `${clamp(batchProgress, 0, 100)}%` }} /></div>
                <span>{Math.round(batchProgress)}%</span>
              </div>
            )}
            {exportError && <p className="error-message" role="alert">{exportError}</p>}
            {notice && <p className="success-note"><i />{notice}</p>}
          </div>
        </div>
      </div>

      {!!exports.length && (
        <div className="studio-exports">
          <div className="section-heading">
            <div>
              <p className="eyebrow">EXPORTED SHORTS</p>
              <h3>Recent exports</h3>
            </div>
            <button className="text-button" type="button" onClick={() => void refreshExports()}>Refresh</button>
          </div>
          <div className="export-list">
            {exports.map((record) => (
              <article className={`export-row status-${record.status}`} key={record.id}>
                <div className="export-row-main">
                  <strong>{record.title}</strong>
                  <span className="export-meta">
                    {ASPECT_LABELS[record.aspect]} · {MODE_LABELS[record.mode]} · {formatSeconds(record.endMs - record.startMs)}
                    {record.captions ? " · captions" : ""}
                  </span>
                  {record.outputPath && (
                    <span className="export-file" title={record.outputPath}>{record.outputPath.split(/[\\/]/).pop()}</span>
                  )}
                  {record.errorMessage && <span className="export-error">{record.errorMessage}</span>}
                </div>
                <div className="export-row-side">
                  <span className={`export-status status-${record.status}`}>{exportStatusLabel(record)}</span>
                  {exportIsActive(record) && (
                    <div className="progress-track export-progress"><span style={{ width: `${record.progress}%` }} /></div>
                  )}
                  <div className="export-actions-row">
                    {record.status === "completed" && (
                      <>
                        <button className="text-button" type="button" onClick={() => void openExportFile(record.id)}>Open</button>
                        <button className="text-button" type="button" onClick={() => void revealExportFile(record.id)}>Show folder</button>
                      </>
                    )}
                    {(record.status === "failed" || record.status === "cancelled") && !activeBatchId && (
                      <button className="text-button" type="button" onClick={() => void retryExport(record.id)}>Retry</button>
                    )}
                  </div>
                </div>
              </article>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
