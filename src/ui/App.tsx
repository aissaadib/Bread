import { useCallback, useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";
import Studio from "./Studio";
import type { Candidate, HistoryEntry, MediaInfo, StudioClip } from "./types";

type AnalysisEvent = {
  job_id: string;
  type: string;
  stage?: string;
  progress?: number;
  message?: string;
  code?: string;
  candidate?: {
    start_ms: number;
    peak_ms: number;
    end_ms: number;
    score: number;
    signals: string[];
    explanation: string;
    title?: string;
    title_source?: string;
    clip_path?: string | null;
  };
  notices?: string[];
};

type ActiveAnalysis = {
  id: string | null;
  status: string;
  stage: string;
  progress: number;
  candidates: Candidate[];
  error: string | null;
  notices: string[];
};

type StudioSession = {
  jobId: string;
  path: string;
  media: MediaInfo;
  clips: StudioClip[];
  initialPositionMs: number;
};

const idleAnalysis: ActiveAnalysis = {
  id: null,
  status: "idle",
  stage: "Choose a video to get started",
  progress: 0,
  candidates: [],
  error: null,
  notices: [],
};

const stageNames: Record<string, string> = {
  queued: "Getting ready",
  inspecting: "Checking your video",
  analyzing_audio: "Listening for standout moments",
  analyzing_video: "Looking for visual changes",
  transcribing: "Checking speech locally",
  ranking: "Choosing the strongest moments",
  rendering_clips: "Saving your MP4 clips",
  completed: "Your highlights are ready",
  cancelling: "Stopping safely",
  cancelled: "Analysis stopped",
  failed: "Analysis needs attention",
};

function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds)) return "Unknown";
  const total = Math.floor(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remainder = total % 60;
  if (hours) return `${hours} hr ${minutes} min`;
  if (minutes) return `${minutes} min ${remainder} sec`;
  return `${remainder} sec`;
}

function formatTimestamp(milliseconds: number): string {
  const totalSeconds = Math.floor(milliseconds / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours) return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function formatFrameRate(frameRate: string): string {
  const cleaned = frameRate.trim().replace(/\s*fps$/i, "").trim();
  if (!cleaned) return "";
  if (cleaned.includes("/")) {
    const [numerator, denominator] = cleaned.split("/").map(Number);
    if (Number.isFinite(numerator) && Number.isFinite(denominator) && denominator !== 0) {
      const rate = Math.round((numerator / denominator) * 100) / 100;
      return `${rate} fps`;
    }
  }
  const rate = Number.parseFloat(cleaned);
  if (Number.isFinite(rate) && rate > 0) {
    return `${Math.round(rate * 100) / 100} fps`;
  }
  return `${cleaned} fps`;
}

function formatSize(bytes: number): string {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function friendlyError(reason: unknown): string {
  return typeof reason === "string" ? reason : "Something went wrong. Please try again.";
}

function candidateKey(candidate: Candidate, index: number): string {
  return `${candidate.peakMs}-${index}`;
}

function toStudioClip(candidate: Candidate, index: number): StudioClip {
  return {
    key: candidateKey(candidate, index),
    candidateId: candidate.id ?? null,
    title: candidate.title,
    peakMs: candidate.peakMs,
    startMs: candidate.startMs,
    endMs: candidate.endMs,
    detectedStartMs: candidate.startMs,
    detectedEndMs: candidate.endMs,
  };
}

function applyEvent(current: ActiveAnalysis, event: AnalysisEvent): ActiveAnalysis {
  switch (event.type) {
    case "stage":
    case "progress":
      return {
        ...current,
        status: "running",
        stage: event.stage ?? current.stage,
        progress: event.progress ?? current.progress,
      };
    case "candidate": {
      if (!event.candidate) return current;
      const candidate = event.candidate;
      return {
        ...current,
        candidates: [...current.candidates.filter((item) => item.peakMs !== candidate.peak_ms), {
          startMs: candidate.start_ms,
          peakMs: candidate.peak_ms,
          endMs: candidate.end_ms,
          score: candidate.score,
          signals: candidate.signals,
          explanation: candidate.explanation,
          title: candidate.title ?? "Highlight",
          titleSource: candidate.title_source ?? "evidence_fallback",
          clipPath: candidate.clip_path ?? null,
        }].sort((a, b) => a.startMs - b.startMs),
      };
    }
    case "complete":
      return { ...current, status: "completed", stage: "completed", progress: 100, notices: event.notices ?? [] };
    case "cancelled":
      return { ...current, status: "cancelled", stage: "cancelled" };
    case "error":
      return { ...current, status: "failed", stage: "failed", error: event.message ?? "The analysis worker stopped unexpectedly." };
    default:
      return current;
  }
}

export default function App() {
  const [media, setMedia] = useState<MediaInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [analysis, setAnalysis] = useState<ActiveAnalysis>(idleAnalysis);
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [selectedHistoryId, setSelectedHistoryId] = useState<string | null>(null);
  const [selectedKeys, setSelectedKeys] = useState<string[]>([]);
  const [studio, setStudio] = useState<StudioSession | null>(null);
  const [studioBusy, setStudioBusy] = useState(false);
  const bufferedEvents = useRef(new Map<string, AnalysisEvent[]>());
  const activeJobId = useRef<string | null>(null);
  const studioRef = useRef<HTMLDivElement | null>(null);

  const refreshHistory = useCallback(async () => {
    try {
      setHistory(await invoke<HistoryEntry[]>("list_history"));
    } catch {
      // The current analysis still works if history cannot be read temporarily.
    }
  }, []);

  useEffect(() => {
    void refreshHistory();
    let unlisten: (() => void) | undefined;
    let disposed = false;
    void listen<AnalysisEvent>("analysis-event", ({ payload }) => {
      if (activeJobId.current !== payload.job_id) {
        const buffered = bufferedEvents.current.get(payload.job_id) ?? [];
        buffered.push(payload);
        bufferedEvents.current.set(payload.job_id, buffered.slice(-300));
        while (bufferedEvents.current.size > 5) {
          const oldest = bufferedEvents.current.keys().next().value;
          if (oldest) bufferedEvents.current.delete(oldest);
          else break;
        }
      }
      setAnalysis((current) => current.id === payload.job_id ? applyEvent(current, payload) : current);
      if (["complete", "cancelled", "error"].includes(payload.type)) void refreshHistory();
    }).then((stop) => {
      if (disposed) stop();
      else unlisten = stop;
    });
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [refreshHistory]);

  const historySelection = history.find((item) => item.id === selectedHistoryId) ?? null;
  const displayedCandidates = historySelection?.candidates ?? analysis.candidates;
  const displayedStatus = historySelection?.status ?? analysis.status;
  const displayedNotices = historySelection?.notices ?? analysis.notices;

  useEffect(() => {
    setSelectedKeys([]);
  }, [selectedHistoryId, analysis.id]);

  useEffect(() => {
    if (studio) studioRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [studio]);

  async function chooseMedia() {
    setError(null);
    let selection: string | string[] | null;
    try {
      selection = await open({
        multiple: false,
        directory: false,
        filters: [{ name: "Video recordings", extensions: ["mp4", "mkv", "webm"] }],
      });
    } catch (reason) {
      setError(friendlyError(reason));
      return;
    }
    if (typeof selection !== "string") return;

    setBusy(true);
    setMedia(null);
    setAnalysis(idleAnalysis);
    setSelectedHistoryId(null);
    setStudio(null);
    try {
      const inspected = await invoke<MediaInfo>("register_media", { path: selection });
      setMedia(inspected);
      await refreshHistory();
    } catch (reason) {
      setError(friendlyError(reason));
    } finally {
      setBusy(false);
    }
  }

  async function startAnalysis() {
    if (!media || busy) return;
    setError(null);
    setSelectedHistoryId(null);
    setStudio(null);
    setAnalysis({ ...idleAnalysis, status: "starting", stage: "queued" });
    activeJobId.current = null;
    try {
      const id = await invoke<string>("start_analysis", { path: media.path });
      const events = bufferedEvents.current.get(id) ?? [];
      bufferedEvents.current.delete(id);
      activeJobId.current = id;
      setAnalysis(events.reduce(applyEvent, { ...idleAnalysis, id, status: "running", stage: "queued" }));
      await refreshHistory();
    } catch (reason) {
      setAnalysis({ ...idleAnalysis, status: "failed", stage: "failed", error: friendlyError(reason) });
      await refreshHistory();
    }
  }

  async function cancelAnalysis() {
    if (!analysis.id) return;
    try {
      await invoke("cancel_analysis", { jobId: analysis.id });
      setAnalysis((current) => ({ ...current, status: "cancelling", stage: "cancelling" }));
    } catch (reason) {
      setAnalysis((current) => ({ ...current, error: friendlyError(reason) }));
    }
  }

  function toggleSelection(key: string) {
    setSelectedKeys((current) =>
      current.includes(key) ? current.filter((item) => item !== key) : [...current, key],
    );
  }

  async function openStudio(candidates: Candidate[], initialPositionMs?: number) {
    if (!candidates.length) return;
    const jobId = historySelection?.id ?? analysis.id;
    const path = historySelection?.path ?? media?.path ?? null;
    if (!jobId || !path) {
      setError("Finish an analysis before opening the short-form studio.");
      return;
    }
    setError(null);
    setStudioBusy(true);
    try {
      const inspected =
        media && media.path === path ? media : await invoke<MediaInfo>("register_media", { path });
      setSelectedHistoryId(null);
      setMedia(inspected);
      setStudio({
        jobId,
        path,
        media: inspected,
        clips: candidates.map(toStudioClip),
        initialPositionMs: initialPositionMs ?? candidates[0].peakMs,
      });
    } catch (reason) {
      setError(friendlyError(reason));
    } finally {
      setStudioBusy(false);
    }
  }

  const selectedCandidates = displayedCandidates.filter((candidate, index) =>
    selectedKeys.includes(candidateKey(candidate, index)),
  );

  const active = ["starting", "queued", "running", "cancelling"].includes(analysis.status);

  return (
    <main className="shell">
      <header className="topbar">
        <a className="brand" href="#home" aria-label="Luna home"><span className="brand-mark">L</span><span className="brand-copy"><strong>LUNA</strong><small>STREAM CLIP FINDER</small></span></a>
        <span className="local-badge"><i /> YOUR VIDEOS STAY ON THIS DEVICE</span>
      </header>

      <section className="intro">
        <div className="intro-copy">
          <p className="eyebrow">MADE FOR YOUR STREAM RECORDINGS</p>
          <h1>Find the moments<br />worth sharing.</h1>
          <p className="lede">Choose a recording and Luna will find standout moments, then shape any one of them into a vertical short.</p>
        </div>
        <aside className="how-card" aria-label="How Luna works">
          <p className="eyebrow">THREE SIMPLE STEPS</p>
          <div className="how-step"><span>1</span><div><strong>Choose</strong><small>Pick a video from your computer</small></div></div>
          <div className="how-step"><span>2</span><div><strong>Find</strong><small>Luna scans for standout moments</small></div></div>
          <div className="how-step"><span>3</span><div><strong>Shape</strong><small>Frame and export a short-form MP4</small></div></div>
        </aside>
      </section>

      <section className="source-panel" aria-labelledby="source-title">
        <div className="section-heading">
          <div className="section-title-wrap"><span className="step-number">01</span><div><p className="eyebrow">YOUR VIDEO</p><h2 id="source-title">Choose a recording</h2></div></div>
          <span className="format-note">MP4 · MKV · WEBM</span>
        </div>
        {!media ? (
          <button className="dropzone" type="button" onClick={chooseMedia} disabled={busy || active}>
            <span className="upload-icon" aria-hidden="true">↑</span>
            <span className="drop-title">{busy ? "Checking your video…" : "Choose a video to get started"}</span>
            <span className="drop-subtitle">Select an MP4, MKV, or WebM recording from your computer.</span>
            <span className="select-button">{busy ? "PLEASE WAIT" : "CHOOSE VIDEO"}<span aria-hidden="true">↗</span></span>
            <span className="privacy-note"><i /> Your original video stays where it is. Luna works locally.</span>
          </button>
        ) : (
          <div className="media-summary">
            <div className="file-icon" aria-hidden="true">▶</div>
            <div className="file-details"><strong title={media.name}>{media.name}</strong><span>{formatDuration(media.durationSeconds)} · {formatSize(media.sizeBytes)}</span></div>
            <div className="media-specs">
              {media.width && media.height && <span>{media.width} × {media.height}</span>}
              {media.frameRate && <span>{formatFrameRate(media.frameRate)}</span>}
              <span className={media.audioTracks ? "audio-ok" : "audio-missing"}>{media.audioTracks ? `Audio included` : "No audio track"}</span>
            </div>
            <button type="button" className="text-button change-button" disabled={active} onClick={() => { setMedia(null); setStudio(null); setError(null); }}>Choose another</button>
          </div>
        )}
        {media && !media.audioTracks && <p className="inline-note">This recording has no audio. Luna can still look for visual changes.</p>}
        {error && <p className="error-message" role="alert">{error}</p>}
      </section>

      <section className="analysis-panel" aria-labelledby="analysis-title">
        <div className="section-heading analysis-heading">
          <div className="section-title-wrap"><span className="step-number">02</span><div><p className="eyebrow">FIND YOUR MOMENTS</p><h2 id="analysis-title">Ready to scan</h2></div></div>
          {active && <span className="live-pill"><i /> WORKING</span>}
        </div>
        <div className={`engine-state ${active ? "is-active" : ""} ${analysis.status === "failed" ? "has-error" : ""}`}>
          <span className={`state-dot ${analysis.status === "failed" ? "state-error" : ""}`} />
          <div><strong>{stageNames[analysis.stage] ?? analysis.stage}</strong><span>{media ? "Audio and video are checked on your device." : "Choose a video above before starting."}</span></div>
          {active && <span className="percent">{analysis.progress}%</span>}
        </div>
        <div className={`progress-track ${active ? "progress-active" : ""}`} role="progressbar" aria-label="Analysis progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={analysis.progress}><span style={{ width: `${analysis.progress}%` }} /></div>
        <div className="analysis-actions">
          <button className="start-button" type="button" onClick={startAnalysis} disabled={!media || active || busy}>
            {active ? "Finding your moments…" : "Find my highlights"}<span aria-hidden="true">{active ? "···" : "↗"}</span>
          </button>
          {active && analysis.id && <button className="cancel-button" type="button" onClick={cancelAnalysis}>Stop</button>}
        </div>
        {analysis.error && <p className="error-message" role="alert">{analysis.error}</p>}
        {analysis.status === "completed" && !historySelection && <p className="success-note"><i /> Done. Your clips are saved in Downloads/{media?.name.replace(/\.[^.]+$/, "") ?? "your video name"}.</p>}
      </section>

      <section className="results-section">
        <div className="section-heading">
          <div className="section-title-wrap"><span className="step-number">03</span><div><p className="eyebrow">YOUR CLIPS</p><h2>{historySelection ? historySelection.sourceName : "Highlights"}</h2></div></div>
          {!!displayedCandidates.length && (
            <div className="results-actions">
              {!!selectedCandidates.length && (
                <button
                  className="ghost-button"
                  type="button"
                  disabled={studioBusy || active}
                  onClick={() => void openStudio(selectedCandidates, selectedCandidates[0].peakMs)}
                >
                  Shape {selectedCandidates.length} {selectedCandidates.length === 1 ? "short" : "shorts"}
                </button>
              )}
              <span className="result-count">{displayedCandidates.length} {displayedCandidates.length === 1 ? "clip" : "clips"}</span>
            </div>
          )}
        </div>
        {displayedCandidates.length ? (
          <>
          <p className="results-hint">Clips are saved as MP4 files in your Downloads folder. Higher scores mean stronger detected changes, not guaranteed highlights.</p>
          <div className="candidate-list">
            {displayedCandidates.map((candidate, index) => {
              const key = candidateKey(candidate, index);
              const selected = selectedKeys.includes(key);
              return (
                <article className={`candidate-row ${selected ? "is-selected" : ""}`} key={key}>
                  <span className="candidate-number">{String(index + 1).padStart(2, "0")}</span>
                  <div className="candidate-title"><strong>{candidate.title}</strong><span>{candidate.titleSource === "local_model" ? "LOCAL AI TITLE" : "EVIDENCE TITLE"}</span></div>
                  <div className="candidate-times">
                    <span><small>CLIP</small>{formatTimestamp(candidate.startMs)} – {formatTimestamp(candidate.endMs)}</span>
                    <span className="peak-time"><small>BEST MOMENT</small>{formatTimestamp(candidate.peakMs)}</span>
                  </div>
                  <div className="candidate-score"><strong>{candidate.score}<small>/100</small></strong><span>HIGHLIGHT SCORE</span></div>
                  <p className="candidate-explanation">{candidate.explanation}</p>
                  <div className="signal-list">{candidate.signals.map((signal) => <span key={signal}>{signal.replaceAll("_", " ")}</span>)}</div>
                  {candidate.clipPath && <p className="clip-saved"><i /> Saved · {candidate.clipPath.split(/[\\/]/).pop()}</p>}
                  <div className="candidate-actions">
                    <button
                      className="ghost-button"
                      type="button"
                      disabled={studioBusy || active}
                      onClick={() => void openStudio([candidate], candidate.peakMs)}
                    >
                      Create short
                    </button>
                    <label className="candidate-select">
                      <input
                        type="checkbox"
                        checked={selected}
                        disabled={studioBusy || active}
                        onChange={() => toggleSelection(key)}
                      />
                      <span>Add to batch</span>
                    </label>
                  </div>
                </article>
              );
            })}
          </div>
          </>
        ) : (
          <div className="empty-results">
            <span className="empty-mark" aria-hidden="true">✳</span>
            <strong>{displayedStatus === "completed" ? "No standout moments found this time" : "Your highlights will show up here"}</strong>
            <span>{displayedStatus === "completed" ? "Try another recording with clear audio or noticeable scene changes." : "Choose a recording, then select “Find my highlights”."}</span>
          </div>
        )}
        {!!displayedNotices.length && <ul className="notice-list">{displayedNotices.map((notice) => <li key={notice}>{notice}</li>)}</ul>}
      </section>

      {studio && (
        <div ref={studioRef}>
          <Studio
            key={`${studio.jobId}-${studio.clips.map((clip) => clip.key).join("|")}`}
            jobId={studio.jobId}
            sourcePath={studio.path}
            media={studio.media}
            clips={studio.clips}
            initialPositionMs={studio.initialPositionMs}
            onClose={() => setStudio(null)}
          />
        </div>
      )}

      <section className="history-section">
        <div className="section-heading">
          <div><p className="eyebrow">PICK UP WHERE YOU LEFT OFF</p><h2>Recent recordings</h2></div>
          {!!history.length && <span className="format-note">{history.length} SAVED</span>}
        </div>
        {!history.length ? <p className="history-empty">Your past analyses will appear here on this device.</p> : (
          <div className="history-list">
            {history.map((item) => (
              <button className={`history-item ${selectedHistoryId === item.id ? "selected" : ""}`} type="button" key={item.id}
                disabled={active} onClick={() => { setSelectedHistoryId(item.id); setMedia(null); setStudio(null); }}>
                <span className={`history-status status-${item.status}`}>{item.status === "completed" ? "Ready" : item.status === "failed" ? "Needs attention" : item.status}</span>
                <span className="history-name">{item.sourceName}</span>
                <span className="history-detail">{item.candidates.length} moments · {new Date(item.createdAt).toLocaleDateString()}</span>
                <span className="history-arrow">↗</span>
              </button>
            ))}
            {selectedHistoryId && <button type="button" className="text-button clear-history" onClick={() => { setSelectedHistoryId(null); void refreshHistory(); }}>Back to current recording</button>}
          </div>
        )}
      </section>

      <footer><span><i /> YOUR FILES STAY ON YOUR DEVICE</span><span>LUNA · STREAM CLIP FINDER</span></footer>
    </main>
  );
}
