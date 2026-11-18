import { useCallback, useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";

type MediaInfo = {
  path: string;
  name: string;
  durationSeconds: number;
  sizeBytes: number;
  width: number | null;
  height: number | null;
  frameRate: string | null;
  videoCodec: string | null;
  audioCodec: string | null;
  audioTracks: number;
};

type Candidate = {
  startMs: number;
  peakMs: number;
  endMs: number;
  score: number;
  signals: string[];
  explanation: string;
  title: string;
  titleSource: string;
  clipPath: string | null;
};

type HistoryEntry = {
  id: string;
  sourceName: string;
  path: string;
  status: string;
  stage: string;
  progress: number;
  createdAt: string;
  errorMessage: string | null;
  notices: string[];
  candidates: Candidate[];
};

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

const idleAnalysis: ActiveAnalysis = {
  id: null,
  status: "idle",
  stage: "Ready when you are",
  progress: 0,
  candidates: [],
  error: null,
  notices: [],
};

const stageNames: Record<string, string> = {
  queued: "Waiting to start",
  inspecting: "Checking recording",
  analyzing_audio: "Analyzing audio",
  analyzing_video: "Analyzing video",
  transcribing: "Transcribing locally",
  ranking: "Combining event signals",
  rendering_clips: "Saving MP4 clips",
  completed: "Analysis complete",
  cancelling: "Stopping analysis",
  cancelled: "Analysis cancelled",
  failed: "Analysis failed",
};

function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds)) return "Unknown";
  const total = Math.floor(seconds);
  return `${String(Math.floor(total / 3600)).padStart(2, "0")}:${String(Math.floor((total % 3600) / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}

function formatTimestamp(milliseconds: number): string {
  const totalSeconds = Math.floor(milliseconds / 1000);
  const millis = milliseconds % 1000;
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(millis).padStart(3, "0")}`;
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
  const bufferedEvents = useRef(new Map<string, AnalysisEvent[]>());
  const activeJobId = useRef<string | null>(null);

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
    setAnalysis({ ...idleAnalysis, status: "starting", stage: "Starting local worker" });
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

  const active = ["starting", "queued", "running", "cancelling"].includes(analysis.status);
  const historySelection = history.find((item) => item.id === selectedHistoryId) ?? null;
  const displayedCandidates = historySelection?.candidates ?? analysis.candidates;
  const displayedStatus = historySelection?.status ?? analysis.status;
  const displayedNotices = historySelection?.notices ?? analysis.notices;

  return (
    <main className="shell">
      <header className="topbar">
        <a className="brand" href="#home" aria-label="Luna home"><span className="brand-mark">L</span><span>LUNA</span></a>
        <span className="local-badge"><i /> LOCAL WORKSPACE</span>
      </header>

      <section className="intro">
        <p className="eyebrow">STREAM ANALYZER <span>·</span> V0.1</p>
        <h1>Find the moments<br />worth keeping.</h1>
        <p className="lede">Analyze a recording on your computer and find the moments that stand out. Your original video stays where it is.</p>
      </section>

      <section className="source-panel" aria-labelledby="source-title">
        <div className="section-heading">
          <div><p className="eyebrow">01 / SOURCE</p><h2 id="source-title">Choose a recording</h2></div>
          <span className="format-note">MP4&nbsp; · &nbsp;MKV&nbsp; · &nbsp;WEBM</span>
        </div>
        {!media ? (
          <button className="dropzone" type="button" onClick={chooseMedia} disabled={busy || active}>
            <span className="upload-icon" aria-hidden="true">↑</span>
            <span className="drop-title">{busy ? "Inspecting recording…" : "Select a video from this device"}</span>
            <span className="drop-subtitle">Luna reads the file locally. It does not move or upload your recording.</span>
            <span className="select-button">{busy ? "PLEASE WAIT" : "BROWSE FILES"}</span>
          </button>
        ) : (
          <div className="media-summary">
            <div className="file-icon">▤</div>
            <div className="file-details"><strong title={media.name}>{media.name}</strong><span>{formatDuration(media.durationSeconds)} · {formatSize(media.sizeBytes)}</span></div>
            <div className="media-specs">
              <span>{media.width && media.height ? `${media.width} × ${media.height}` : "No video size"}</span>
              <span>{media.frameRate ?? "Unknown frame rate"}</span>
              <span className={media.audioTracks ? "audio-ok" : "audio-missing"}>{media.audioTracks ? `${media.audioTracks} audio track${media.audioTracks === 1 ? "" : "s"}` : "No audio track"}</span>
            </div>
            <button type="button" className="text-button" disabled={active} onClick={() => { setMedia(null); setError(null); }}>Change</button>
          </div>
        )}
        {error && <p className="error-message" role="alert">{error}</p>}
      </section>

      <section className="analysis-panel" aria-labelledby="analysis-title">
        <div className="section-heading analysis-heading">
          <div><p className="eyebrow">02 / ANALYSIS</p><h2 id="analysis-title">Local detection</h2></div>
          {active && <span className="live-pill"><i /> IN PROGRESS</span>}
        </div>
        <div className="engine-state">
          <span className={`state-dot ${analysis.status === "failed" ? "state-error" : ""}`} />
          <div><strong>{stageNames[analysis.stage] ?? analysis.stage}</strong><span>Adaptive audio + low-rate visual analysis</span></div>
          <span className="percent">{active ? `${analysis.progress}%` : "LOCAL"}</span>
        </div>
        <div className="progress-track" aria-label={`Analysis progress ${analysis.progress}%`}><span style={{ width: `${analysis.progress}%` }} /></div>
        <div className="analysis-actions">
          <button className="start-button" type="button" onClick={startAnalysis} disabled={!media || active || busy}>
            {active ? "ANALYSIS RUNNING" : "START ANALYSIS"}<span>↗</span>
          </button>
          {active && analysis.id && <button className="cancel-button" type="button" onClick={cancelAnalysis}>Cancel</button>}
        </div>
        {analysis.error && <p className="error-message" role="alert">{analysis.error}</p>}
      </section>

      <section className="results-section">
        <div className="section-heading">
          <div><p className="eyebrow">03 / RESULTS</p><h2>{historySelection ? historySelection.sourceName : "Detected moments"}</h2></div>
          <span className="format-note">{displayedCandidates.length} FOUND</span>
        </div>
        {displayedCandidates.length ? (
          <div className="candidate-list">
            {displayedCandidates.map((candidate, index) => (
              <article className="candidate-row" key={`${candidate.peakMs}-${index}`}>
                <span className="candidate-number">{String(index + 1).padStart(2, "0")}</span>
                <div className="candidate-title"><strong>{candidate.title}</strong><span>{candidate.titleSource === "local_model" ? "LOCAL AI TITLE" : "EVIDENCE TITLE"}</span></div>
                <div className="candidate-times">
                  <span><small>START</small>{formatTimestamp(candidate.startMs)}</span>
                  <span className="peak-time"><small>PEAK</small>{formatTimestamp(candidate.peakMs)}</span>
                  <span><small>END</small>{formatTimestamp(candidate.endMs)}</span>
                </div>
                <div className="candidate-score"><strong>{candidate.score}</strong><span>PRIORITY</span></div>
                <p className="candidate-explanation">{candidate.explanation}</p>
                <div className="signal-list">{candidate.signals.map((signal) => <span key={signal}>{signal.replaceAll("_", " ")}</span>)}</div>
                {candidate.clipPath && <p className="clip-saved">MP4 SAVED · {candidate.clipPath.split(/[\\/]/).pop()}</p>}
              </article>
            ))}
          </div>
        ) : (
          <div className="empty-results">
            <span className="empty-mark">—</span>
            <p>{displayedStatus === "completed" ? "No moments met the current detection threshold." : "Run an analysis to see timestamped moments here."}</p>
            <span>Scores rank detected evidence; they are not confidence probabilities.</span>
          </div>
        )}
        {!!displayedNotices.length && <ul className="notice-list">{displayedNotices.map((notice) => <li key={notice}>{notice}</li>)}</ul>}
      </section>

      <section className="history-section">
        <div className="section-heading">
          <div><p className="eyebrow">04 / HISTORY</p><h2>Previous analyses</h2></div>
          <span className="format-note">{history.length} SAVED</span>
        </div>
        {!history.length ? <p className="history-empty">Completed analyses are stored locally on this device.</p> : (
          <div className="history-list">
            {history.map((item) => (
              <button className={`history-item ${selectedHistoryId === item.id ? "selected" : ""}`} type="button" key={item.id}
                disabled={active} onClick={() => { setSelectedHistoryId(item.id); setMedia(null); }}>
                <span className={`history-status status-${item.status}`}>{item.status}</span>
                <span className="history-name">{item.sourceName}</span>
                <span className="history-detail">{item.candidates.length} moments · {new Date(item.createdAt).toLocaleDateString()}</span>
                <span className="history-arrow">↗</span>
              </button>
            ))}
            {selectedHistoryId && <button type="button" className="text-button clear-history" onClick={() => { setSelectedHistoryId(null); void refreshHistory(); }}>Close history</button>}
          </div>
        )}
      </section>

      <footer><span>LOCAL FIRST BY DESIGN</span><span>V0.1 · ANALYSIS FOUNDATION</span></footer>
    </main>
  );
}
