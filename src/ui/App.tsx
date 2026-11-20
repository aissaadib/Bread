import { useCallback, useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";
import Studio from "./Studio";
import type {
  AiConnectionResult,
  AiEvent,
  AiSettings,
  Candidate,
  HistoryEntry,
  MediaInfo,
  StudioClip,
} from "./types";

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
    title: candidate.aiTitle || candidate.title,
    peakMs: candidate.aiPeakMs ?? candidate.peakMs,
    startMs: candidate.startMs,
    endMs: candidate.endMs,
    detectedStartMs: candidate.startMs,
    detectedEndMs: candidate.endMs,
    aiFocusX: candidate.aiFocusX ?? null,
    aiFocusY: candidate.aiFocusY ?? null,
    aiEventType: candidate.aiEventType ?? null,
  };
}

const AI_STATUS_LABELS: Record<string, string> = {
  idle: "NOT RUN",
  disabled: "LOCAL ONLY",
  missing_key: "NO KEY",
  ready: "READY",
  running: "REVIEWING",
  completed: "COMPLETED",
  rate_limited: "RATE LIMITED",
  failed: "FAILED",
  cancelled: "STOPPED",
};

type AiRun = {
  jobId: string | null;
  status: string;
  message: string | null;
  assessed: number;
  total: number;
};

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
  const [studioExporting, setStudioExporting] = useState(false);
  const [ai, setAi] = useState<AiSettings | null>(null);
  const [aiBusy, setAiBusy] = useState(false);
  const [aiError, setAiError] = useState<string | null>(null);
  const [aiConnection, setAiConnection] = useState<AiConnectionResult | null>(null);
  const [aiRun, setAiRun] = useState<AiRun>({ jobId: null, status: "idle", message: null, assessed: 0, total: 0 });
  const bufferedEvents = useRef(new Map<string, AnalysisEvent[]>());
  const activeJobId = useRef<string | null>(null);
  const studioRef = useRef<HTMLDivElement | null>(null);
  const keyInputRef = useRef<HTMLInputElement | null>(null);

  const refreshHistory = useCallback(async () => {
    try {
      setHistory(await invoke<HistoryEntry[]>("list_history"));
    } catch {
      // The current analysis still works if history cannot be read temporarily.
    }
  }, []);

  useEffect(() => {
    let disposed = false;
    void invoke<AiSettings>("ai_settings")
      .then((settings) => {
        if (!disposed) setAi(settings);
      })
      .catch(() => undefined);
    return () => {
      disposed = true;
    };
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

  useEffect(() => {
    let disposed = false;
    let unlisten: (() => void) | undefined;
    void listen<AiEvent>("ai-event", ({ payload }) => {
      setAiRun((current) => {
        const jobId = payload.jobId ?? current.jobId;
        if (payload.type === "ai_started") {
          return {
            jobId,
            status: "running",
            message: `Reviewing ${payload.total ?? 0} window${payload.total === 1 ? "" : "s"} with ${payload.model ?? "Gemini"}`,
            assessed: 0,
            total: payload.total ?? 0,
          };
        }
        if (payload.type === "ai_progress") {
          return { ...current, jobId, status: "running" };
        }
        if (payload.type === "ai_item") {
          const rejected = payload.item?.rejected;
          return {
            ...current,
            jobId,
            assessed: current.assessed + (rejected ? 0 : 1),
            message: rejected ? payload.item?.message ?? current.message : current.message,
          };
        }
        if (payload.type === "ai_complete") {
          const status = payload.cancelled
            ? "cancelled"
            : payload.rateLimited
              ? "rate_limited"
              : (payload.assessed ?? 0) > 0
                ? "completed"
                : "failed";
          return {
            jobId,
            status,
            message: payload.message ?? current.message,
            assessed: payload.assessed ?? current.assessed,
            total: payload.total ?? current.total,
          };
        }
        if (payload.type === "error") {
          return { ...current, jobId, status: "failed", message: payload.message ?? "Cloud analysis failed." };
        }
        return current;
      });
      if (["ai_item", "ai_complete", "error"].includes(payload.type)) void refreshHistory();
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

  async function saveAiSettings(patch: Partial<Pick<AiSettings, "enabled" | "model" | "maxWindows" | "consentAccepted">>) {
    if (!ai) return;
    setAiError(null);
    setAiBusy(true);
    try {
      const next = await invoke<AiSettings>("save_ai_settings", {
        request: {
          enabled: patch.enabled ?? ai.enabled,
          model: patch.model ?? ai.model,
          maxWindows: patch.maxWindows ?? ai.maxWindows,
          consentAccepted: patch.consentAccepted ?? ai.consentAccepted,
        },
      });
      setAi(next);
    } catch (reason) {
      setAiError(friendlyError(reason));
    } finally {
      setAiBusy(false);
    }
  }

  async function saveAiKey() {
    const input = keyInputRef.current;
    const value = input?.value.trim() ?? "";
    if (!value) {
      setAiError("Paste your Gemini API key first.");
      return;
    }
    setAiBusy(true);
    setAiError(null);
    setAiConnection(null);
    try {
      const next = await invoke<AiSettings>("set_gemini_key", { key: value });
      setAi(next);
      if (input) input.value = "";
      setAiConnection({
        ok: true,
        model: next.model,
        message: "Key stored in Windows Credential Manager. Test the connection to confirm it.",
        latencyMs: null,
      });
    } catch (reason) {
      if (input) input.value = "";
      setAiError(friendlyError(reason));
    } finally {
      setAiBusy(false);
    }
  }

  async function clearAiKey() {
    setAiBusy(true);
    setAiError(null);
    setAiConnection(null);
    try {
      setAi(await invoke<AiSettings>("clear_gemini_key"));
    } catch (reason) {
      setAiError(friendlyError(reason));
    } finally {
      setAiBusy(false);
    }
  }

  async function testAiConnection() {
    setAiBusy(true);
    setAiError(null);
    setAiConnection(null);
    try {
      const result = await invoke<AiConnectionResult>("test_ai_connection");
      setAiConnection(result);
      if (!result.ok) setAiError(result.message);
    } catch (reason) {
      setAiError(friendlyError(reason));
    } finally {
      setAiBusy(false);
    }
  }

  async function startAiAnalysis() {
    if (!aiTargetId) return;
    setAiError(null);
    setAiRun({ jobId: aiTargetId, status: "running", message: "Starting cloud analysis…", assessed: 0, total: 0 });
    try {
      await invoke("start_ai_analysis", { jobId: aiTargetId });
      // The AI merge is persisted in the database, so display the same job from
      // history; the live analysis state only carries local worker events.
      setSelectedHistoryId(aiTargetId);
      await refreshHistory();
    } catch (reason) {
      const message = friendlyError(reason);
      setAiError(message);
      setAiRun({ jobId: aiTargetId, status: "failed", message, assessed: 0, total: 0 });
    }
  }

  async function cancelAiAnalysis() {
    if (!aiTargetId) return;
    try {
      await invoke("cancel_ai_analysis", { jobId: aiTargetId });
      setAiRun((current) => ({ ...current, status: "cancelled", message: "Stopping cloud analysis…" }));
    } catch (reason) {
      setAiError(friendlyError(reason));
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

  const aiTargetId = historySelection?.id ?? analysis.id;
  const aiTargetEntry = aiTargetId ? history.find((item) => item.id === aiTargetId) ?? null : null;
  const aiRuntime = aiRun.jobId === aiTargetId ? aiRun : null;
  const aiStatus = aiRuntime?.status ?? aiTargetEntry?.aiStatus ?? (ai?.enabled ? "ready" : "disabled");
  const aiMessage = aiRuntime?.message ?? aiTargetEntry?.aiMessage ?? null;
  const aiActive = aiStatus === "running";
  const aiTargetComplete = (aiTargetEntry?.status ?? analysis.status) === "completed" && !!displayedCandidates.length;
  const aiCanRun =
    !!aiTargetId && aiTargetComplete && !!ai?.enabled && !!ai.hasKey && !!ai.consentAccepted && !aiActive;

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
          <button className="dropzone" type="button" onClick={chooseMedia} disabled={busy || active || studioExporting}>
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
            <button type="button" className="text-button change-button" disabled={active || studioExporting} onClick={() => { setMedia(null); setStudio(null); setError(null); }}>Choose another</button>
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
          <button className="start-button" type="button" onClick={startAnalysis} disabled={!media || active || busy || studioExporting}>
            {active ? "Finding your moments…" : "Find my highlights"}<span aria-hidden="true">{active ? "···" : "↗"}</span>
          </button>
          {active && analysis.id && <button className="cancel-button" type="button" onClick={cancelAnalysis}>Stop</button>}
        </div>
        {analysis.error && <p className="error-message" role="alert">{analysis.error}</p>}
        {analysis.status === "completed" && !historySelection && <p className="success-note"><i /> Done. Your clips are saved in Downloads/{media?.name.replace(/\.[^.]+$/, "") ?? "your video name"}.</p>}
      </section>

      <section className="ai-section" aria-labelledby="ai-title">
        <div className="section-heading">
          <div className="section-title-wrap">
            <span className="step-number">AI</span>
            <div><p className="eyebrow">OPTIONAL CLOUD INTELLIGENCE</p><h2 id="ai-title">Cloud AI analysis</h2></div>
          </div>
          <span className={`ai-status status-${aiStatus}`}>{AI_STATUS_LABELS[aiStatus] ?? aiStatus}</span>
        </div>
        <p className="ai-privacy">{ai?.privacyNote ?? "Cloud analysis is optional. Luna's local analysis always works without it."}</p>

        <div className="ai-grid">
          <div className="ai-block">
            <label className="toggle-row">
              <input
                type="checkbox"
                checked={!!ai?.enabled}
                disabled={aiBusy || !ai || !ai.consentAccepted}
                onChange={(event) =>
                  void saveAiSettings(
                    event.target.checked
                      ? { enabled: true }
                      : { enabled: false, consentAccepted: false },
                  )
                }
              />
              <span>
                <strong>Send candidate segments to Gemini</strong>
                <small>Off by default. Only the short windows Luna selects are uploaded.</small>
              </span>
            </label>

            {ai && !ai.consentAccepted && (
              <div className="ai-consent">
                <p>Before using cloud analysis, confirm you understand what leaves this device:</p>
                <ul>
                  <li>Only the 10–30 second segments around detected moments are uploaded, never the whole recording.</li>
                  <li>Google processes them with the Gemini API; on the free tier submitted content may be used to improve Google’s products.</li>
                  <li>Uploaded segments are deleted from Google after each request where the API allows it.</li>
                </ul>
                <button className="ghost-button" type="button" disabled={aiBusy} onClick={() => void saveAiSettings({ consentAccepted: true, enabled: true })}>
                  I understand — enable Cloud AI
                </button>
              </div>
            )}
            {ai?.consentAccepted && (
              <p className="ai-hint">Cloud processing consent recorded. Turning the toggle off also clears this consent.</p>
            )}

            <div className="ai-field">
              <label htmlFor="ai-model">Model</label>
              <select
                id="ai-model"
                value={ai?.model ?? ""}
                disabled={aiBusy || !ai}
                onChange={(event) => void saveAiSettings({ model: event.target.value })}
              >
                {(ai?.supportedModels ?? []).map((model) => <option key={model} value={model}>{model}</option>)}
              </select>
            </div>

            <div className="ai-field">
              <label htmlFor="ai-windows">Windows per run · {ai?.maxWindows ?? 6}</label>
              <input
                id="ai-windows"
                type="range" min={1} max={12} step={1}
                value={ai?.maxWindows ?? 6}
                disabled={aiBusy || !ai}
                onChange={(event) => void saveAiSettings({ maxWindows: Number(event.target.value) })}
              />
              <small>Each window is one Gemini request. Higher values cost more of your quota.</small>
            </div>
          </div>

          <div className="ai-block">
            <div className="control-label">
              <span>Your Gemini API key</span>
              <span className={`status-chip ${ai?.hasKey ? "is-ready" : "is-empty"}`}>{ai?.hasKey ? "STORED SECURELY" : "NO KEY"}</span>
            </div>
            <p className="ai-hint">Where does the key go? Windows Credential Manager, read only by Luna’s native side. It is never written to the project, the database, logs or the page.</p>
            <div className="ai-key-row">
              <input
                ref={keyInputRef}
                type="password"
                placeholder={ai?.hasKey ? "Replace the stored key" : "Paste your Gemini API key"}
                autoComplete="off"
                spellCheck={false}
                disabled={aiBusy}
                aria-label="Gemini API key"
              />
              <button className="ghost-button" type="button" onClick={() => void saveAiKey()} disabled={aiBusy}>Save key</button>
              <button className="text-button" type="button" onClick={() => void clearAiKey()} disabled={aiBusy || !ai?.hasKey}>Clear</button>
            </div>
            <div className="ai-actions">
              <button className="ghost-button" type="button" onClick={() => void testAiConnection()} disabled={aiBusy || !ai?.hasKey}>
                {aiBusy ? "Testing…" : "Test connection"}
              </button>
              <span className={`ai-hint ${aiConnection && !aiConnection.ok ? "is-bad" : ""}`}>
                {aiConnection
                  ? `${aiConnection.message}${aiConnection.latencyMs ? ` (${aiConnection.latencyMs} ms)` : ""}`
                  : "Uses one model-metadata request. Nothing is uploaded."}
              </span>
            </div>
          </div>
        </div>

        <div className="ai-run">
          <button className="ghost-button ai-run-button" type="button" onClick={() => void startAiAnalysis()} disabled={!aiCanRun || aiBusy}>
            {aiActive ? "Reviewing with Gemini…" : "Review candidates with Gemini"}
          </button>
          {aiActive && <button className="text-button" type="button" onClick={() => void cancelAiAnalysis()}>Stop</button>}
          <span className="ai-hint">
            {aiMessage ?? (aiTargetComplete ? "Review the ranked moments with Gemini for semantic event labels." : "Finish a local analysis first.")}
          </span>
        </div>
        {aiError && <p className="error-message" role="alert">{aiError}</p>}
        {aiStatus === "rate_limited" && (
          <p className="inline-warning">Gemini rate-limited this run. Your local results are unchanged — try again later rather than retrying immediately.</p>
        )}
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
                  disabled={studioBusy || active || studioExporting}
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
                  <div className="candidate-title">
                    <strong>{candidate.aiTitle || candidate.title}</strong>
                    <span>{candidate.aiTitle ? "GEMINI TITLE" : candidate.titleSource === "local_model" ? "LOCAL AI TITLE" : "EVIDENCE TITLE"}</span>
                  </div>
                  <div className="candidate-times">
                    <span><small>CLIP</small>{formatTimestamp(candidate.startMs)} – {formatTimestamp(candidate.endMs)}</span>
                    <span className="peak-time"><small>BEST MOMENT</small>{formatTimestamp(candidate.peakMs)}</span>
                  </div>
                  <div className="candidate-score">
                    <strong>{candidate.combinedScore ?? candidate.score}<small>/100</small></strong>
                    <span>{candidate.combinedScore != null ? "RANKING SCORE" : "HIGHLIGHT SCORE"}</span>
                  </div>
                  <p className="candidate-explanation">{candidate.explanation}</p>
                  <div className="signal-list">
                    {candidate.signals.map((signal) => <span key={signal}>{signal.replaceAll("_", " ")}</span>)}
                    {candidate.aiEventType && <span className="signal-ai">{candidate.aiEventType.replaceAll("_", " ")} · gemini</span>}
                    {candidate.origin === "gemini" && <span className="signal-ai">found by gemini</span>}
                  </div>
                  {candidate.aiEventType && (
                    <p className="ai-evidence">
                      <strong>Gemini {candidate.aiScore != null ? `${candidate.aiScore}/100` : ""}</strong>
                      {candidate.aiReason ? ` — ${candidate.aiReason}` : ""}
                      {candidate.aiScore != null && ` · local evidence ${candidate.score}/100`}
                    </p>
                  )}
                  {candidate.clipPath && <p className="clip-saved"><i /> Saved · {candidate.clipPath.split(/[\\/]/).pop()}</p>}
                  <div className="candidate-actions">
                    <button
                      className="ghost-button"
                      type="button"
                      disabled={studioBusy || active || studioExporting}
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
            onExportingChange={setStudioExporting}
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
                disabled={active || studioExporting} onClick={() => { setSelectedHistoryId(item.id); setMedia(null); setStudio(null); }}>
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
