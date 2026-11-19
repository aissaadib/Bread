# Luna — local stream analyzer

Luna analyzes local gaming recordings, returns ranked timestamp suggestions, and turns any of them into a framed short-form MP4 for YouTube Shorts, TikTok, or Instagram Reels. It accepts local MP4, MKV, and WebM files and does not download platform streams or upload media.

## Architecture

- **Desktop:** Tauri 2, React 19, TypeScript strict mode, and Vite.
- **Native core:** Rust owns file inspection, worker lifecycle, job events, and SQLite persistence.
- **Analysis:** a separate Python JSONL worker uses FFmpeg for streaming audio extraction and low-rate video sampling. NumPy acceleration is optional; the worker still runs without it.
- **Persistence:** versioned SQLite migrations store sources, job state, the pinned analysis config, analysis version, candidates, evidence, explanations, and exported clip paths in the app's local data directory.

The frontend stays responsive while Rust reads worker events on a background thread. The worker does not write logs to its JSONL output; diagnostics go to stderr. A recording is referenced by path and is never copied or modified.

## What works

- Import and inspect local MP4/MKV/WebM recordings with FFprobe.
- Persist imported source metadata and deduplicate by canonical file path.
- Analyze every audio track at 16 kHz in one-second windows with a 60-second long baseline, an 8-second short baseline, energy gating, timbre-change gating, and a per-track summary.
- Detect visual activity at 1 fps and 160×90 using motion and histogram-change features against a local baseline. Strong visual evidence alone can create a candidate, so silent gameplay is eligible.
- Fuse audio, visual, VAD speech, and transcript-reaction evidence; coalesce duplicate track observations; attach a reaction only to the nearest event; clamp, merge, and rank candidates by score with a minimum separation.
- Return start, peak, and end timestamps. The peak is the strongest evidence sample; the clip window adds 15 seconds before and 10 seconds after the evidence cluster, capped at 45 seconds.
- Show live worker stages/progress, cancel an analysis, view results, and reopen saved analysis history.
- Export each detected segment as an MP4 in `Downloads/<source video name>/` with a descriptive filename.
- Open one highlight, or a batch of selected highlights, in the Short-form Studio and render a new short-form MP4 (see below).
- Recover unfinished jobs as failed after an app restart.

The score is a transparent ranking aid, not a probability of a true highlight. Visual-only candidates must clear a magnitude gate, and audio-only candidates need a stronger anomaly than fused audio-plus-visual evidence.

## Short-form Studio

Selecting **Create short** on a candidate, or ticking **Add to batch** on several and choosing **Shape N shorts**, opens the Short-form Studio on that analysis. The studio plays the original recording with the chosen composition, keeps the detected peak inside the clip, and renders a new MP4 without modifying the source.

- **Output presets:** Vertical 1080 × 1920 (default), Square 1080 × 1080, and Original resolution.
- **Presentation modes:**
  - **Full frame** (default) keeps the entire source frame — minimap, kill feed, HUD, and scoreboard included — and fills the rest of the canvas with a blurred, darkened cover of the same footage.
  - **Crop** fills the canvas without stretching and exposes horizontal/vertical crop positions, centered by default.
  - **Original** preserves the source shape inside the canvas with a dark background.
- **Boundaries:** start and end sliders clamp the clip to 1–180 seconds, keep the detected peak inside the window, and show the detected window plus the current selection on a timeline. **Reset** returns to the detected boundaries. The clip is never silently truncated.
- **Captions (optional):** burned in only from the saved local Faster-Whisper words/segments, grouped into at most two lines with real timestamps. The toggle is disabled when the analysis has no transcript, and a missing speech model never blocks export.
- **Export:** single or batch (up to 50 clips), a chosen output folder (default `Downloads/Luna Shorts/<source video name>/`), CRF 14–28, live per-clip progress, cancellation, retry for failed/cancelled items, and Open/Show folder for finished files. Rendered files are written as a hidden partial and renamed on success, so a cancelled or failed export never leaves a broken MP4.

The preview and the renderer share one geometry implementation, mirrored in `src/ui/framing.ts` and `worker/luna_worker/framing.py`; the TypeScript mirror is checked against the Python version across 216 preset/mode/crop/source combinations with zero mismatches.

Rendering runs in `worker/export_worker.py` (a second Python process, separate from analysis) and shells out to FFmpeg with libx264 `veryfast`, `-pix_fmt yuv420p`, AAC audio when the source has it, `+faststart`, and passthrough frame timing. Every job is persisted in the `clip_exports` table (migration `003_shortform_exports.sql`) with its source, candidate, original and adjusted timestamps, preset, mode, crop, caption flag, CRF, output path, and status.

## Measured verification

These are local measurements, not a calibrated accuracy claim. The full procedure and limitations are in [worker/EVALUATION.md](worker/EVALUATION.md).

- **Real gameplay, silent audio:** `1026.mp4` (36.73 s, 1920×1080, 30 fps, Rocket League goal at ~33–35 s). Before the detector changes it returned **0 candidates**. The final packaged desktop app returned **1 candidate**: start **18.000 s**, peak **35.000 s**, end **36.733 s**, score **31/100**, signal `visual_activity` only. The manual goal interval was ~32.5–36.0 s with a peak near 34.5 s, so the peak error was **+0.5 s**, the labeled event was fully contained, and no false positives were returned in the recording (1/1 precision and recall for this sample).
- **Desktop wall time:** 5.18 s including import-to-results polling and MP4 export; the persisted job timestamps span 4.72 s. Audio and speech were correctly reported as skipped because the AAC track is digitally silent.
- **Packaged speech path:** a 12 s synthetic clip with spoken “Oh my god, that was insane!” and a visual flash produced one candidate at score 100 with `Audio Anomaly`, `Speech Reaction`, and `Visual Activity`; the explanation included the transcribed reaction. Wall time was 3.87 s. This verifies Silero VAD and the local Faster-Whisper model inside the real desktop app, not only in Python.
- **Synthetic long recording:** the 3-hour 160×90/1 fps file with a known audio+visual event at 90:00 completed in **32.2 s** (335× real time), returned one candidate with **0 ms peak error**, and sampled **80.6 MiB** peak RSS for Python plus children. This is a low-complexity synthetic result, not a real-game accuracy claim. Run `python worker/tests/benchmark_synthetic.py` to repeat it.
- **Short-form rendering (v0.3):** `1026.mp4` (1920×1080, 30 fps, AAC) rendered from the detected candidate at 25.0–36.6 s in five configurations — Vertical full frame, Vertical crop at both horizontal extremes, Square crop, and Original. Every output was probed: H.264, `yuv420p`, 30/1 fps, AAC 44.1 kHz stereo, duration **11.600 s** (matching the requested range to the millisecond), at 1080×1920, 1080×1080, and 1920×1080 respectively. Extracted frames confirmed Full frame preserves both horizontal edges and the full HUD over a blurred cover, the two crop extremes show opposite sides of the frame without stretching, and Original keeps the native 16:9 framing. Render wall time was 2.0–10.1 s per 11.6 s clip on CPU.
- **Packaged v0.3 launch:** `luna.exe` started from the release build with the previous v0.2 database, migrated it from schema version 2 to 3 (`clip_exports` table plus `jobs.transcript_json`), and preserved all 10 jobs, 44 candidates, and completed analyses.
- **Non-gameplay negatives:** the Kenta “replay” files are desktop/browser screen recordings, not gameplay, and are excluded from gaming accuracy. They produced 0–3 visual candidates (scores 30–40); the 60 s TikTok feed produced two fused visual+audio candidates (scores 83 and 64). These show the detector’s behavior on menu/scene content, not whether it finds gaming highlights.

Only one real gaming recording was available, so precision and recall above are single-sample observations. Do not generalize them to a game library.

## Optional local speech models

The baseline audio/video path runs without AI packages. Silero VAD activates when the `silero-vad` package is installed. Faster-Whisper activates only when a complete local CTranslate2 model folder is found; Luna uses CPU int8 and `local_files_only=True` and never downloads a model automatically.

On this machine the optional stack lives in the project `.venv` (Python 3.12):

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r worker/requirements-optional.txt
```

Download a model separately, for example `Systran/faster-whisper-base.en`, into `.models/faster-whisper-base.en`. Model discovery checks `LUNA_WHISPER_MODEL` first, then `LUNA_MODELS_DIR`, then `.models` folders found by walking up from the working directory and from the worker's packaged location. This means the source checkout and a packaged build both find `D:\Bread\.models`; an installed copy can point `LUNA_MODELS_DIR` at its model folder.

Transcription is optional and is skipped when the selected audio is effectively silent or no model is available. The desktop host allows at least **120 seconds** of worker idle time (7% of recording duration for long files, capped by a 12-hour total runtime), and the worker emits 10-second heartbeats while a model loads or transcribes, so model initialization does not trip the idle timeout.

Luna looks for Python 3.11+ in common Windows install locations and through the `py -3`, `python3`, and `python` commands. For a non-standard installation, set `LUNA_PYTHON` to the Python executable path before starting Luna. FFmpeg and FFprobe must be on `PATH`.

## Optional local AI clip titles

For AI-generated clip titles, run a local Ollama-compatible service and set `LUNA_TITLE_MODEL` to an installed model name before starting Luna. The default endpoint is `http://127.0.0.1:11434/api/generate`; `LUNA_TITLE_ENDPOINT` can point to another localhost endpoint. Luna sends only the clip's short transcript and analysis evidence to that local service. If no model responds, Luna still saves the clip with a descriptive title based on the detected signals and timestamp. This title model is separate from Faster-Whisper transcription.

## Requirements

- Node.js 20.19+ or 22.12+.
- Rust stable and the Tauri Windows prerequisites: WebView2 and the Microsoft C++ build tools (or a compatible GNU toolchain).
- Python 3.11+ for the analysis worker.
- FFmpeg/FFprobe on `PATH`.

## Run on Windows

```powershell
npm ci
npm run tauri dev
```

For a release build and launch, use the Tauri CLI so the production `custom-protocol` feature is enabled:

```powershell
npm run tauri build
.\run-luna.cmd
```

If you invoke Cargo directly, you must enable the feature yourself. Without it Tauri builds a development binary that loads `http://127.0.0.1:1420` instead of the embedded UI:

```powershell
cargo build --release --features custom-protocol --manifest-path src-tauri/Cargo.toml
```

After building, double-click `run-luna.cmd` to launch the desktop app with its database, WebView profile, temporary files, and model cache contained under `.runtime` in this project. Desktop apps started by command sessions may be closed when the session ends; launching this script from Explorer runs it outside that session.

## Verify

```powershell
npm run build
cargo check --manifest-path src-tauri/Cargo.toml
npm run test:worker
cargo test --manifest-path src-tauri/logic-tests/Cargo.toml
```

`npm run test:worker` runs 37 Python tests. Besides detection, they render real files through the short-form pipeline and assert on FFprobe output and decoded pixels: canvas dimensions per preset, `yuv420p`, audio presence/absence, Full-frame edge preservation, crop position at both extremes, burned-in captions, cancellation cleanup, and the export worker's JSONL protocol. The Rust logic tests compile the same `src-tauri/src/logic.rs` used by the desktop crate and cover extension filtering, frame-rate formatting, job IDs, worker idle-timeout policy, and short-form setting validation (including that the Original preset forces source framing).

On this Windows GNU toolchain, a Tauri-linked Rust test binary fails at process startup with `STATUS_ENTRYPOINT_NOT_FOUND` (`0xc0000139`), while the desktop binary builds and runs normally. The `logic-tests` crate is the verified alternative: `cargo test --manifest-path src-tauri/logic-tests/Cargo.toml` passes 3/3 without linking Tauri. Do not report the Tauri test harness as passing on this machine.

## Current limits

- Only one real gaming recording was available; precision/recall and peak error are single-sample measurements, not a calibrated benchmark.
- The 15-second pre-roll is deliberate context, not a detection boundary. A short goal can therefore have a much earlier start than the action; the peak is the primary timestamp.
- The detector is generic, not game-specific. Menus, alt-tabs, and large scene changes can score; quiet gameplay can still be missed if motion and histogram change stay below the gates.
- Multi-track coalescing and adaptive audio baselines are covered by synthetic/unit tests, but no real multi-track gameplay recording was available for measurement.
- Speech is used as positive evidence. VAD does not suppress every audio anomaly caused by commentary, so speech-heavy streams can still surface loud-talk candidates; this was not tuned on a labeled speech-heavy gameplay set.
- VAD and transcription depend on the optional local packages/models. The app remains useful without them.
- One analysis runs at a time. The worker decodes the source separately for audio and video.
- Captions exist only as burned-in text generated from saved transcription. There is no editable subtitle track, cloud source, account integration, or platform stream downloading. Clip title quality depends on the separate optional local-token model; evidence titles are used when it is unavailable.
- Short-form framing is deterministic cropping and letterboxing, not AI reframing: it never tracks a subject inside the frame.
- Exports always use CPU libx264; GPU encoders are not offered, so long batches are slower on weak hardware than an NVENC build would be.
- One short-form export batch runs at a time, and only one analysis runs at a time.
- The Rust Tauri-linked unit-test harness remains blocked by the toolchain issue described above; the desktop build and the Tauri-free logic tests are verified.
