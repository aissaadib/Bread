import { useState } from "react";
import { invoke } from "@tauri-apps/api/core";
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
};

function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds)) return "Unknown";
  const total = Math.floor(seconds);
  return `${String(Math.floor(total / 3600)).padStart(2, "0")}:${String(Math.floor((total % 3600) / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}

function formatSize(bytes: number): string {
  const units = ["B", "KB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

export default function App() {
  const [media, setMedia] = useState<MediaInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function chooseMedia() {
    setError(null);
    const selection = await open({
      multiple: false,
      directory: false,
      filters: [{ name: "Video recordings", extensions: ["mp4", "mkv", "webm"] }],
    });
    if (typeof selection !== "string") return;

    setBusy(true);
    setMedia(null);
    try {
      const inspected = await invoke<MediaInfo>("inspect_media", { path: selection });
      setMedia(inspected);
    } catch (reason) {
      setError(typeof reason === "string" ? reason : "Could not inspect this recording.");
    } finally {
      setBusy(false);
    }
  }

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
          <button className="dropzone" type="button" onClick={chooseMedia} disabled={busy}>
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
              <span>{media.frameRate ?? "Unknown fps"}</span>
              <span className={media.audioCodec ? "audio-ok" : "audio-missing"}>{media.audioCodec ? "Audio available" : "No audio track"}</span>
            </div>
            <button type="button" className="text-button" onClick={() => { setMedia(null); setError(null); }}>Change</button>
          </div>
        )}
        {error && <p className="error-message" role="alert">{error}</p>}
      </section>

      <section className="analysis-panel" aria-labelledby="analysis-title">
        <div><p className="eyebrow">02 / ANALYSIS</p><h2 id="analysis-title">Analysis engine</h2></div>
        <div className="engine-state"><span className="state-dot" /><div><strong>Foundation in progress</strong><span>Media inspection is ready. Highlight analysis is the next milestone.</span></div></div>
        <button className="start-button" type="button" disabled title="Analysis pipeline is under construction">ANALYSIS COMING NEXT <span>↗</span></button>
      </section>

      <footer><span>LOCAL FIRST BY DESIGN</span><span>V0.1 · FOUNDATION</span></footer>
    </main>
  );
}
