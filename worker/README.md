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
- `config.json`: shared, versioned detector parameters. Rust stores the same bytes in each job record and passes them to the worker.

Core operation requires Python's standard library plus FFmpeg/FFprobe. Optional model packages are listed in `requirements-optional.txt`; the app never downloads models. For CPU-only Faster-Whisper, set `LUNA_WHISPER_MODEL` to a local model directory.
