# Luna — local stream analyzer

Luna is a Windows desktop app for analyzing gaming recordings and returning ranked highlight timestamps. V0.1 starts with local files. It does not download platform streams, edit video, or upload media.

## Stack

- Tauri 2 and Rust for the native desktop lifecycle, safe file inspection, and later SQLite-backed jobs.
- React 19, TypeScript strict mode, and Vite for the interface.
- A separate Python worker for the analysis pipeline.
- FFmpeg and FFprobe for media processing and metadata.
- SQLite for persistent sources, jobs, and candidates in a later foundation step.

This stack follows the LUNA brief: the frontend stays responsive, Rust owns native lifecycle and persistence, and Python modules can be tested without the desktop app. Local files are the initial source abstraction; authorized cloud/platform integrations are future work.

## Current foundation

The first screen selects a local MP4, MKV, or WebM and asks FFprobe for duration, resolution, frame rate, codecs, and audio availability. It preserves the original media. The analysis button is intentionally disabled until a real processing pipeline exists.

## Requirements

- Node.js 20.19+ or 22.12+.
- Rust stable and the Tauri Windows prerequisites, including WebView2 and Microsoft C++ Build Tools.
- FFmpeg/FFprobe on `PATH` for media inspection.
- Python 3.11+ will be needed when the worker milestone is connected.

## Run

```powershell
npm install
npm run tauri dev
```

Dependency installation and desktop compilation have not yet been verified in this workspace because package registry access was unavailable during initial setup. Do not treat the analysis workflow as implemented yet.
