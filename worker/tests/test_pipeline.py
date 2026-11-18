from __future__ import annotations

import os
import json
import shutil
import subprocess
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
import sys

sys.path.insert(0, str(ROOT / "worker"))

from luna_worker.audio import _window_features
from luna_worker.media import MediaError, inspect_media
from luna_worker.pipeline import analyze
from luna_worker.ranking import rank
from luna_worker.titles import generate

ARTIFACTS = ROOT / ".test-output"


class AnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cls.recording = ARTIFACTS / "known-event.mp4"
        filter_audio = "volume='if(between(t,5,6),1,0.02)':eval=frame"
        command = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=12",
            "-filter_complex", f"color=c=black:s=320x180:r=15:d=5[b1];color=c=white:s=320x180:r=15:d=2[w];color=c=black:s=320x180:r=15:d=5[b2];[b1][w][b2]concat=n=3:v=1:a=0[v];[0:a]{filter_audio}[a]",
            "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(cls.recording),
        ]
        subprocess.run(command, check=True, capture_output=True, env=os.environ.copy())
        cls.silent_recording = ARTIFACTS / "silent.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            "color=c=black:s=160x90:r=2:d=4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(cls.silent_recording),
        ], check=True, capture_output=True, env=os.environ.copy())

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(ARTIFACTS, ignore_errors=True)

    def test_audio_features_capture_non_silent_input(self) -> None:
        import array

        raw = array.array("h", [1000, -1000, 0, 500]).tobytes()
        rms, peak, zcr = _window_features(raw)
        self.assertGreater(rms, -40)
        self.assertGreater(peak, 0.03)
        self.assertGreater(zcr, 0)

    def test_invalid_media_has_a_structured_error(self) -> None:
        with self.assertRaises(MediaError) as caught:
            inspect_media(str(ARTIFACTS / "missing.mp4"))
        self.assertEqual(caught.exception.code, "source_missing")

        corrupt = ARTIFACTS / "corrupt.mp4"
        corrupt.write_bytes(b"not a video")
        with self.assertRaises(MediaError) as caught:
            inspect_media(str(corrupt))
        self.assertEqual(caught.exception.code, "invalid_media")

    def test_rank_clamps_boundaries_and_preserves_evidence(self) -> None:
        results = rank(
            [{"timestamp_ms": 2_000, "signal": "audio_anomaly", "magnitude": 0.8,
              "metadata": {"delta_db": 18.0}}], [], [], 10.0,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["start_ms"], 0)
        self.assertEqual(results[0]["peak_ms"], 2_000)
        self.assertEqual(results[0]["end_ms"], 10_000)
        self.assertIn("audio_anomaly", results[0]["signals"])
        self.assertGreater(results[0]["score"], 0)

    def test_nearby_audio_peaks_are_merged_into_one_candidate(self) -> None:
        peaks = [
            {"timestamp_ms": time_ms, "signal": "audio_anomaly", "magnitude": 0.7,
             "metadata": {"delta_db": 15.0}}
            for time_ms in (20_000, 23_000)
        ]
        results = rank(peaks, [], [], 60.0)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(results[0]["evidence"]), 2)
        self.assertLessEqual(results[0]["end_ms"] - results[0]["start_ms"], 45_000)

    def test_end_to_end_pipeline_finds_injected_audio_and_visual_event(self) -> None:
        emitted: list[tuple[str, dict]] = []
        analyze(str(self.recording), threading.Event(), lambda kind, **data: emitted.append((kind, data)))
        completed = next(data for kind, data in emitted if kind == "complete")
        candidates = [data["candidate"] for kind, data in emitted if kind == "candidate"]
        self.assertEqual(completed["candidate_count"], len(candidates))
        self.assertGreaterEqual(len(candidates), 1)
        candidate = min(candidates, key=lambda item: abs(item["peak_ms"] - 5_500))
        self.assertLessEqual(abs(candidate["peak_ms"] - 5_500), 1_500)
        self.assertIn("audio_anomaly", candidate["signals"])
        self.assertIn("visual_activity", candidate["signals"])
        self.assertLessEqual(candidate["start_ms"], candidate["peak_ms"])
        self.assertGreaterEqual(candidate["end_ms"], candidate["peak_ms"])
        self.assertLessEqual(candidate["end_ms"], 12_000)
        self.assertLessEqual(candidate["end_ms"] - candidate["start_ms"], 45_000)

    def test_pipeline_saves_named_mp4_clips(self) -> None:
        output_dir = ARTIFACTS / "clips"
        output_dir.mkdir(exist_ok=True)
        model = os.environ.pop("LUNA_TITLE_MODEL", None)
        try:
            emitted: list[tuple[str, dict]] = []
            analyze(str(self.recording), threading.Event(), lambda kind, **data: emitted.append((kind, data)),
                    output_dir=str(output_dir))
        finally:
            if model is not None:
                os.environ["LUNA_TITLE_MODEL"] = model
        candidates = [data["candidate"] for kind, data in emitted if kind == "candidate"]
        completed = next(data for kind, data in emitted if kind == "complete")
        self.assertTrue(candidates)
        self.assertTrue(all(Path(item["clip_path"]).is_file() for item in candidates))
        self.assertTrue(all(item["clip_path"].endswith(".mp4") for item in candidates))
        self.assertTrue(all(item["title"] in Path(item["clip_path"]).name for item in candidates))
        self.assertTrue(any("Local AI title model" in note for note in completed["notices"]))

    def test_title_fallback_is_descriptive_and_filesystem_safe(self) -> None:
        title, source = generate({"peak_ms": 61_000, "signals": ["audio_anomaly"],
                                  "explanation": 'Audio: 90% <> "unexpected"'}, [])
        self.assertEqual(source, "evidence_fallback")
        self.assertEqual(title, "Unexpected Audio Spike 01-01")
        self.assertFalse(any(character in title for character in '<>:"/\\|?*'))

    def test_cancellation_stops_audio_work(self) -> None:
        cancelled = threading.Event()
        cancelled.set()
        emitted: list[tuple[str, dict]] = []
        with self.assertRaises(InterruptedError):
            analyze(str(self.recording), cancelled, lambda kind, **data: emitted.append((kind, data)))

    def test_video_without_audio_completes_with_a_clear_notice(self) -> None:
        emitted: list[tuple[str, dict]] = []
        analyze(str(self.silent_recording), threading.Event(), lambda kind, **data: emitted.append((kind, data)))
        completed = next(data for kind, data in emitted if kind == "complete")
        self.assertEqual(completed["candidate_count"], 0)
        self.assertTrue(any("No audio track" in notice for notice in completed["notices"]))

    def test_worker_jsonl_protocol_carries_job_ids_and_terminal_result(self) -> None:
        from luna_worker import DEFAULT_CONFIG

        process = subprocess.Popen(
            [sys.executable, str(ROOT / "worker" / "clipper_worker.py"), "--path", str(ARTIFACTS / "missing.mp4"),
             "--job-id", "protocol-test", "--config-json", json.dumps(DEFAULT_CONFIG)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", env=os.environ.copy(),
        )
        events: list[dict] = []
        reader = threading.Thread(target=lambda: events.extend(json.loads(line) for line in process.stdout), daemon=True)
        reader.start()
        exit_code = process.wait(timeout=10)
        process.stdin.close()
        reader.join(timeout=5)
        process.stdout.close()
        self.assertEqual(exit_code, 1)
        self.assertTrue(events)
        self.assertTrue(all(event["job_id"] == "protocol-test" for event in events))
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(events[-1]["code"], "source_missing")

    def test_worker_process_completes_video_analysis_with_control_pipe_open(self) -> None:
        from luna_worker import DEFAULT_CONFIG

        process = subprocess.Popen(
            [sys.executable, str(ROOT / "worker" / "clipper_worker.py"), "--path", str(self.recording),
             "--job-id", "video-worker-test", "--config-json", json.dumps(DEFAULT_CONFIG)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", env=os.environ.copy(),
        )
        events: list[dict] = []
        reader = threading.Thread(target=lambda: events.extend(json.loads(line) for line in process.stdout), daemon=True)
        reader.start()
        exit_code = process.wait(timeout=30)
        process.stdin.close()
        reader.join(timeout=5)
        process.stdout.close()
        self.assertEqual(exit_code, 0)
        self.assertTrue(events)
        self.assertTrue(all(event["job_id"] == "video-worker-test" for event in events))
        self.assertEqual(events[-1]["type"], "complete")
        self.assertGreater(events[-1]["candidate_count"], 0)

    def test_worker_control_channel_cancels_the_requested_job(self) -> None:
        from luna_worker import DEFAULT_CONFIG

        job_id = "cancel-protocol-test"
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "worker" / "clipper_worker.py"), "--path", str(self.recording),
             "--job-id", job_id, "--config-json", json.dumps(DEFAULT_CONFIG)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", env=os.environ.copy(),
        )
        process.stdin.write(json.dumps({"command": "cancel", "job_id": job_id}) + "\n")
        process.stdin.flush()
        output, _ = process.communicate(timeout=30)
        events = [json.loads(line) for line in output.splitlines()]
        self.assertEqual(process.returncode, 2)
        self.assertEqual(events[-1]["type"], "cancelled")


if __name__ == "__main__":
    unittest.main()
