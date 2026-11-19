# Python worker

`clipper_worker.py` is launched by the Rust host with a media path, job identifier, and the exact JSON analysis configuration stored in SQLite. Events are one JSON object per stdout line and always contain `job_id` and `type`. Progress, stage, candidate, completion, cancellation, and structured error events are supported. Worker diagnostics are written to stderr.

The host keeps the worker's stdin open as its control channel. A matching `{"command":"cancel","job_id":"…"}` request sets a cancellation token; the worker terminates the active FFmpeg process and reports a cancelled terminal event. Closing the host pipe also asks the worker to stop.

Pipeline modules are independent of Tauri:

- `luna_worker/media.py`: validates local sources and probes streams.
- `luna_worker/audio.py`: streamed PCM features and adaptive local anomaly detection, with optional Silero speech intervals.
- `luna_worker/video.py`: bounded low-resolution grayscale frame-difference analysis.
- `luna_worker/transcription.py`: optional local-only Faster-Whisper transcription.
- `luna_worker/ranking.py`: transparent signal fusion, timestamp boundaries, and deduplication.
- `luna_worker/pipeline.py`: stages these components and emits protocol events.
- `luna_worker/ai.py`: optional cloud layer's pure logic - window planning over local candidates, the assessment prompt and JSON schema, strict validation of model replies, evidence fusion and AI titles. No network access, fully unit-tested.
- `luna_worker/gemini.py`: the Google Gemini Developer API client (resumable Files API upload, `generateContent` with a response schema, bounded retries with backoff, 429/quota handling, remote file deletion). The transport is injectable, so tests never touch the network. The API key is sent only in the `x-goog-api-key` header, never in a URL.
- `ai_worker.py`: the cloud analysis process. Launched by the host only when the user enabled cloud analysis; it reads the API key from the first stdin line, cuts one segment per window, uploads, validates the reply, deletes the remote file, and reports JSONL events. Every failure is contained so the local job is unaffected.
- `config.json`: shared, versioned detector parameters plus the `ai` defaults (model, window budget, sampling, retry and fusion weights). Rust stores the same bytes in each job record and passes it to the worker.

Core operation requires Python's standard library plus FFmpeg/FFprobe. Optional model packages are listed in `requirements-optional.txt`; the app never downloads models. For CPU-only Faster-Whisper, set `LUNA_WHISPER_MODEL` to a local model directory.
