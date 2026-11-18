# Luna — local stream analyzer

Luna analyzes local gaming recordings and returns ranked timestamp suggestions. V0.1 accepts local MP4, MKV, and WebM files. It does not download platform streams, edit video, render clips, or upload media.

## Architecture

- **Desktop:** Tauri 2, React 19, TypeScript strict mode, and Vite.
- **Native core:** Rust owns file inspection, worker lifecycle, job events, and SQLite persistence.
- **Analysis:** a separate Python JSONL worker uses FFmpeg for streaming audio extraction and low-rate video sampling. Frame comparison uses the Python standard library to avoid native-library import stalls inside the threaded worker.
- **Persistence:** versioned SQLite migration stores sources, job state, configuration, candidates, evidence, and explanations in the app's local data directory.

The frontend stays responsive while Rust reads worker events on a background thread. The worker does not write logs to its JSONL output; diagnostics go to stderr. A recording is referenced by path and is never copied or modified.

## What works

- Import and inspect local MP4/MKV/WebM recordings with FFprobe.
- Persist imported source metadata and deduplicate by canonical file path.
- Analyze mono 16 kHz audio in one-second windows with a rolling, local median/MAD baseline.
- Sample video at 0.5 fps and 160×90 resolution, then measure adjacent frame changes.
- Combine and rank audio events with speech, transcript, and visual evidence where available; clamp and merge timestamp candidates.
- Show live worker stages/progress, cancel an analysis, view results, and reopen saved analysis history.
- Recover unfinished jobs as failed after an app restart.

The score is a transparent ranking aid, not a probability of a true highlight. A large visual change alone is not enough to create a candidate.

## Synthetic long-recording check

On this Windows machine (20 logical processors, 15.7 GiB available to the runtime, Python 3.14, FFmpeg 9.0.2), the 3-hour 160×90/1 fps synthetic MP4 with a known audio + visual event completed analysis in **22.6 seconds**. The known event at 90:00 was returned at 90:00.500 (0.5-second error); one candidate was returned, measured throughput was 477.9× real time, and sampled Python-plus-child peak RSS was 204.2 MiB. This is a low-complexity synthetic media result, not a GTA VI/real-stream accuracy claim. Run `python worker/tests/benchmark_synthetic.py` to repeat it; the script cleans its generated file afterward. Peak RAM sampling uses optional `psutil`; when unavailable, the benchmark reports no memory value.

## Optional local speech models

The baseline audio/video path runs without AI packages. Silero VAD activates when the `silero-vad` package is installed. Faster-Whisper activates only when `LUNA_WHISPER_MODEL` points to a complete local CTranslate2 model folder. The app uses CPU int8 and `local_files_only=True`; it never downloads a model automatically. Optional package requirements are in `worker/requirements-optional.txt`.

Luna looks for Python 3.11+ in common Windows install locations and through the `py -3`, `python3`, and `python` commands. For a non-standard installation, set `LUNA_PYTHON` to the Python executable path before starting Luna. FFmpeg and FFprobe must be on `PATH`.

## Requirements

- Node.js 20.19+ or 22.12+.
- Rust stable and the Tauri Windows prerequisites: WebView2 and Microsoft C++ Build Tools.
- Python 3.11+ for the analysis worker.
- FFmpeg/FFprobe on `PATH`.

## Run on Windows

```powershell
npm ci
npm run tauri dev
```

After building a release, double-click `run-luna.cmd` to launch the desktop app with its database, WebView profile, temporary files, and model cache contained under `.runtime` in this project. Desktop apps started by Codex command sessions may be closed when the session ends; launching this script from Explorer runs it outside that session.

For a release executable:

```powershell
npm run tauri build
```

## Verify

```powershell
npm run build
cargo check --manifest-path src-tauri/Cargo.toml
npm run test:worker
```

The worker tests generate small synthetic files under `.test-output` and remove them after the run. Synthetic tests validate pipeline mechanics and timing tolerance; they do not measure real highlight quality. See [worker/EVALUATION.md](worker/EVALUATION.md) for a manual real-recording evaluation procedure.

## Current limits

- VAD and transcription depend on optional local packages/models. The app remains useful without them.
- Generic audio anomalies and frame changes are only clues; the initial detector is not game-specific and has no calibrated real-world accuracy.
- One analysis runs at a time. The worker decodes the source separately for audio and video.
- No actual clip trimming/export, subtitles, cloud sources, account integration, or platform stream downloading.
- The Windows release executable builds, but the UI, picker, and full Tauri-to-worker workflow have not been launched interactively. Compile checks and worker integration tests do not verify WebView behavior.
