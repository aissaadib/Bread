# Manual highlight evaluation

Synthetic recordings in the test suite check event extraction, timestamps, cancellation, multi-track coalescing, model discovery, and result bounds. They are not evidence of useful real-world highlight detection. This document records the real-media checks performed for the v0.2 detector and separates measured observations from claims the sample cannot support.

## Labeled real gameplay

Recording: `C:\Users\aissa\Downloads\1026.mp4`

- 36.73 s, 1920×1080, 30 fps, Rocket League.
- Audio: AAC track that is digitally silent.
- Manual label from frame review: goal explosion visible at approximately **32.5–36.0 s**, strongest moment approximately **34.5 s**. This is a single reviewer label on a single recording, not a held-out benchmark set.

### Result

| Measure | Before v0.2 | Final packaged desktop app |
| --- | --- | --- |
| Candidates | 0 | 1 |
| Start | — | 18.000 s |
| Peak | — | 35.000 s |
| End | — | 36.733 s |
| Score | — | 31/100 |
| Signals | — | `visual_activity` |
| Matched labeled event | no | yes |
| Peak error vs manual 34.5 s | — | +0.5 s |
| Candidate contained the 32.5–36.0 s event | no | yes |

For this single sample: precision **1/1**, recall **1/1**, F1 **1.0**. These are descriptive counts, not a population estimate.

The 18.000 s start is the configured 15-second pre-roll before the first evidence sample at 33.000 s, not a detection of action at 18 s. The evidence samples were at 33.000, 34.000, and 35.000 s with magnitudes 0.585, 0.716, and 0.781. The peak is therefore the useful timestamp; the start/end define the export window.

The end is +0.733 s beyond the manual interval because the candidate is clamped to the recording duration and the goal aftermath continues to the file end.

### Runtime in the desktop app

- Import to visible results, polled through the packaged WebView UI: **5.18 s**.
- Persisted job interval: **4.72 s** (`created_at` to `updated_at`).
- Throughput: approximately **7.1–7.8× real time** for this 1080p30 clip including MP4 export.

## Silent-audio behavior

The AAC track in `1026.mp4` is digitally silent. Bread reported:

- “The audio track is effectively silent; audio and speech signals were skipped.”
- “Transcription was skipped because the audio track is effectively silent.”

No audio or speech evidence was fabricated, and the visual candidate remained eligible. This is the behavior that was missing in v0.1, where the recording returned no candidates at all.

## Optional speech path in the packaged app

A controlled 12 s clip was generated with a white visual flash at 6.0–7.3 s and synthesized speech, “Oh my god, that was insane!”, starting at 3.5 s. It was imported and analyzed through the same packaged desktop app.

- One candidate, score **100/100**.
- Signals: `audio_anomaly`, `speech_reaction`, `visual_activity`.
- Explanation included: “transcript reaction: Oh my God, that was insane.”
- Wall time: **3.87 s**.
- The worker reported `speech_detection: true` and `transcription: true`; Silero VAD loaded and Faster-Whisper used the local `faster-whisper-base.en` model with `local_files_only=True`.

This verifies that VAD and Whisper work inside the release application, including model discovery from the packaged worker layout. It does not measure transcription accuracy on noisy game audio.

## Negative and mixed-content samples

These files are not gameplay and are excluded from the gaming precision/recall counts. They were analyzed to check that menus and screen recordings do not crash the pipeline and to observe false-positive behavior.

| Sample | Content | Observed result |
| --- | --- | --- |
| Kenta `*_replay.mp4` files | Desktop/browser screen recordings | 0–3 visual candidates each, scores 30–40 |
| 60 s Kenta TikTok feed | Mixed social feed with music/visual cuts | 2 fused visual+audio candidates, scores 83 and 64 |

The screen recordings show that menu and desktop motion can still score. They do not provide gaming ground truth, so no accuracy number is derived from them.

## Synthetic long-recording check

The benchmark script generated a 3-hour 160×90/1 fps MP4 with one audio+visual event at 90:00 and analyzed it with the current detector and config:

- Analysis wall time: **32.216 s**.
- Throughput: **335.24× real time**.
- Candidate count: **1**.
- Detected peak: **5,400,000 ms**, peak error **0 ms**.
- Sampled Python-plus-children peak RSS: **80.6 MiB**.

This is a low-complexity synthetic file; it measures throughput and timestamp plumbing, not game-highlight accuracy. Repeat with:

```powershell
python worker\tests\benchmark_synthetic.py
```

## Detection changes verified by tests

- **Visual-only candidates:** `test_visual_only_recording_produces_a_candidate` and the real `1026.mp4` run above. A strong frame change clears the visual magnitude gate without requiring audio.
- **Silent audio:** `test_digitally_silent_audio_is_not_flagged` and the `1026.mp4` notices.
- **Multiple audio tracks:** `test_duplicate_audio_track_observations_are_coalesced` verifies that observations from tracks 1 and 2 at the same timestamp become one candidate, the stronger magnitude survives, and metadata records `audio_tracks: [1, 2]`.
- **Adaptive audio baselines:** audio features use a 60 s long baseline and an 8 s short baseline with energy and timbre gates; `test_audio_features_capture_non_silent_input` covers feature extraction.
- **Reaction attribution:** `test_reaction_is_only_attached_to_the_nearest_event` verifies that a transcript reaction is not attached to a distant event.
- **Low-confidence visual noise:** `test_low_confidence_visual_noise_is_filtered` verifies the visual magnitude floor.
- **Packaged model discovery:** `test_whisper_model_discovery_walks_up_from_packaged_layout` and `test_whisper_model_discovery_honors_models_directory_override` cover the release-layout failure found during desktop testing.
- **Worker timeout policy:** the Rust logic test verifies a 120 s floor, 252 s for a 1-hour recording, and 756 s for a 3-hour recording. The worker heartbeat emits every 10 s during model initialization and transcription.

## Reproduction

Worker and build checks:

```powershell
npm run build
cargo check --manifest-path src-tauri/Cargo.toml
npm run test:worker
cargo test --manifest-path src-tauri/logic-tests/Cargo.toml
```

Desktop check:

1. Build with `npm run tauri build` or `cargo build --release --features custom-protocol --manifest-path src-tauri/Cargo.toml`.
2. Launch `run-bread.cmd`.
3. Import the recording, confirm the FPS label shows a single suffix (for example `30 fps`), and select “Find my highlights”.
4. Record the returned start, peak, end, score, signals, notices, and wall time before comparing with the manual label.

## Limitations

- **Single real gameplay sample.** The 1/1 precision and recall and +0.5 s peak error are observations on one 36.7 s recording. No claim of general accuracy is made.
- **Pre-roll affects start boundaries.** The 15 s pre-roll can make a clip start well before the action. The peak timestamp is the primary output, and the clip window is intentionally wider for context.
- **No real multi-track gameplay sample.** Multi-track coalescing is verified by unit tests, not by a measured multi-track recording.
- **Speech can still trigger audio anomalies.** VAD supplies positive speech evidence but does not suppress every loud commentary spike. The speech-heavy gameplay population remains unmeasured.
- **Generic detector.** Menus, alt-tabs, and scene cuts can score; quiet gameplay can be missed if motion and histogram change remain below the gates.
- **Optional models.** VAD and Whisper are functional in the project `.venv`, but are not bundled. A different Python environment needs the optional requirements and a local model.
- **Rust test harness.** On this machine’s GNU toolchain, Tauri-linked test binaries fail at process startup with `STATUS_ENTRYPOINT_NOT_FOUND`. `cargo check` and the release desktop build pass; the Tauri-free `src-tauri/logic-tests` crate passes 3/3 and is the verified test path.
- **No population metrics.** Precision, recall, boundary error, and runtime should be recomputed on a larger labeled set before tuning thresholds or claiming real-world accuracy.
