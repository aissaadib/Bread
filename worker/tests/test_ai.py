"""Cloud AI layer tests. No network access and no real API key are used."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "worker"))

import ai_worker
from luna_worker import ai, media
from luna_worker.gemini import (
    GeminiClient,
    GeminiError,
    MissingKeyError,
    RateLimitedError,
)

ARTIFACTS = ROOT / ".test-output" / "ai"


def run_ffmpeg(arguments: list[str]) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *arguments],
                   check=True, capture_output=True)


def valid_reply(**overrides):
    payload = {
        "is_highlight": True,
        "event_type": "goal",
        "interest_score": 91,
        "peak_offset_seconds": 12.4,
        "reason": "A goal is scored after a strong offensive play.",
        "mostly_menu": False,
        "visual_focus": {"x": 0.52, "y": 0.48},
    }
    payload.update(overrides)
    return json.dumps(payload)


WINDOW = {"start_ms": 100_000, "end_ms": 122_000, "index": 0,
          "origin": "candidate", "candidate_id": 7, "local_score": 31,
          "local_signals": ["visual_activity"]}


class AssessmentParsingTests(unittest.TestCase):
    def test_valid_reply_becomes_absolute_timestamps(self) -> None:
        parsed = ai.parse_assessment(valid_reply(), WINDOW)
        self.assertTrue(parsed["is_highlight"])
        self.assertEqual(parsed["event_type"], "goal")
        self.assertEqual(parsed["interest_score"], 91)
        self.assertEqual(parsed["peak_ms"], 112_400)
        self.assertAlmostEqual(parsed["focus_x"], 0.52)

    def test_fenced_json_is_accepted(self) -> None:
        parsed = ai.parse_assessment(f"```json\n{valid_reply()}\n```", WINDOW)
        self.assertEqual(parsed["peak_ms"], 112_400)

    def test_invalid_json_is_rejected(self) -> None:
        with self.assertRaises(ai.AssessmentError) as caught:
            ai.parse_assessment("not json at all", WINDOW)
        self.assertEqual(caught.exception.code, "invalid_json")

    def test_missing_timestamp_on_a_highlight_is_rejected(self) -> None:
        with self.assertRaises(ai.AssessmentError) as caught:
            ai.parse_assessment(valid_reply(peak_offset_seconds=None), WINDOW)
        self.assertEqual(caught.exception.code, "missing_peak")

    def test_out_of_range_timestamps_are_clamped_to_the_segment(self) -> None:
        late = ai.parse_assessment(valid_reply(peak_offset_seconds=9999), WINDOW)
        early = ai.parse_assessment(valid_reply(peak_offset_seconds=-50), WINDOW)
        self.assertEqual(late["peak_ms"], WINDOW["end_ms"])
        self.assertEqual(early["peak_ms"], WINDOW["start_ms"])

    def test_out_of_range_interest_score_is_clamped(self) -> None:
        parsed = ai.parse_assessment(valid_reply(interest_score=10_000), WINDOW)
        self.assertEqual(parsed["interest_score"], 100)

    def test_bad_focus_is_dropped_not_trusted(self) -> None:
        parsed = ai.parse_assessment(valid_reply(visual_focus={"x": 4.2, "y": -1}), WINDOW)
        self.assertIsNone(parsed["focus_x"])
        self.assertIsNone(parsed["focus_y"])

    def test_unknown_event_type_becomes_other(self) -> None:
        parsed = ai.parse_assessment(valid_reply(event_type="rocket_league_goal"), WINDOW)
        self.assertEqual(parsed["event_type"], "other")

    def test_non_highlight_keeps_no_peak(self) -> None:
        parsed = ai.parse_assessment(
            valid_reply(is_highlight=False, event_type="none", interest_score=4,
                        peak_offset_seconds=None),
            WINDOW,
        )
        self.assertFalse(parsed["is_highlight"])
        self.assertIsNone(parsed["peak_ms"])
        self.assertIsNone(ai.build_title(parsed))

    def test_titles_come_only_from_model_output(self) -> None:
        parsed = ai.parse_assessment(valid_reply(), WINDOW)
        title = ai.build_title(parsed)
        self.assertIn("Goal", title)
        self.assertIn("strong offensive play", title)


class FusionTests(unittest.TestCase):
    def test_local_score_is_untouched_without_ai(self) -> None:
        self.assertEqual(ai.fuse(31, None), 31)

    def test_ai_highlight_raises_a_strong_local_candidate(self) -> None:
        parsed = ai.parse_assessment(valid_reply(interest_score=90), WINDOW)
        self.assertEqual(ai.fuse(60, parsed), 72)

    def test_visual_only_highlight_is_not_discarded(self) -> None:
        parsed = ai.parse_assessment(valid_reply(interest_score=80), WINDOW)
        self.assertGreater(ai.fuse(31, parsed), 31)

    def test_ai_rejection_demotes_but_keeps_the_local_candidate(self) -> None:
        parsed = ai.parse_assessment(
            valid_reply(is_highlight=False, event_type="none", interest_score=5,
                        peak_offset_seconds=None),
            WINDOW,
        )
        self.assertEqual(ai.fuse(80, parsed), 36)


class WindowPlanningTests(unittest.TestCase):
    def test_candidates_are_prioritised_and_windows_are_bounded(self) -> None:
        candidates = [
            {"id": index + 1, "start_ms": index * 60_000, "peak_ms": index * 60_000 + 30_000,
             "end_ms": index * 60_000 + 45_000, "score": 80 - index, "signals": ["visual_activity"]}
            for index in range(8)
        ]
        windows = ai.plan_windows(candidates, 600_000, {"ai": {"max_windows": 4,
                                                               "max_sample_windows": 1}})
        self.assertLessEqual(len(windows), 4)
        self.assertTrue(all(window["end_ms"] > window["start_ms"] for window in windows))
        self.assertEqual(len({window["index"] for window in windows}), len(windows))
        self.assertEqual(windows[0]["origin"], "candidate")

    def test_sampled_windows_reach_beyond_detected_candidates(self) -> None:
        windows = ai.plan_windows([], 900_000, {"ai": {"max_windows": 3,
                                                       "max_sample_windows": 2}})
        self.assertEqual(len(windows), 2)
        self.assertTrue(all(window["origin"] == "sample" for window in windows))
        self.assertGreater(windows[1]["start_ms"], windows[0]["start_ms"])

    def test_overlapping_candidates_do_not_produce_duplicate_windows(self) -> None:
        candidates = [
            {"id": 1, "start_ms": 10_000, "peak_ms": 20_000, "end_ms": 30_000, "score": 70},
            {"id": 2, "start_ms": 11_000, "peak_ms": 21_000, "end_ms": 31_000, "score": 69},
        ]
        windows = ai.plan_windows(candidates, 120_000, {"ai": {"max_windows": 5,
                                                               "max_sample_windows": 0}})
        self.assertEqual(len(windows), 1)

    def test_host_camel_case_candidates_keep_their_real_timestamps(self) -> None:
        # The Rust host sends camelCase; a mistake here silently analyses 0s.
        windows = ai.plan_windows(
            [{"id": 4, "startMs": 25_000, "peakMs": 35_000, "endMs": 36_733,
              "score": 31, "signals": ["visual_activity"]}],
            36_733,
            {"ai": {"max_windows": 1, "max_sample_windows": 0, "window_seconds": 22}},
        )
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["candidate_id"], 4)
        self.assertEqual(windows[0]["local_score"], 31)
        self.assertEqual(windows[0]["end_ms"], 36_733)
        self.assertGreater(windows[0]["start_ms"], 24_000)
        self.assertLessEqual(windows[0]["peak_ms"], 36_733)

    def test_flat_options_are_not_silently_replaced_by_defaults(self) -> None:
        # plan_windows receives an already-normalised dict; re-wrapping it used
        # to reset max_windows/sampling to the defaults.
        options = ai.settings({"ai": {"max_windows": 3, "max_sample_windows": 0}})
        self.assertEqual(options["max_windows"], 3)
        self.assertEqual(ai.settings(options)["max_windows"], 3)
        self.assertEqual(ai.settings(options)["max_sample_windows"], 0)
        windows = ai.plan_windows(
            [{"id": index, "start_ms": index * 60_000, "peak_ms": index * 60_000 + 5_000,
              "end_ms": index * 60_000 + 20_000, "score": 90 - index}
             for index in range(6)],
            696_000,
            options,
        )
        self.assertEqual(len(windows), 3)

    def test_window_settings_are_sanitised(self) -> None:
        options = ai.settings({"ai": {"max_windows": 999, "window_seconds": -5,
                                      "max_retries": 99}})
        self.assertEqual(options["max_windows"], 12)
        self.assertEqual(options["window_seconds"], 6.0)
        self.assertEqual(options["max_retries"], 6)


class FakeClient:
    """Deterministic stand-in for GeminiClient used by worker tests."""

    def __init__(self, replies=None, *, upload_error: Exception | None = None,
                 assess_error: Exception | None = None, delete_ok: bool = True):
        self.model = "gemini-test"
        self.uploaded: list[str] = []
        self.deleted: list[str] = []
        self.prompts: list[str] = []
        self._replies = replies if replies is not None else [valid_reply()]
        self._upload_error = upload_error
        self._assess_error = assess_error
        self._delete_ok = delete_ok

    def upload_file(self, path, mime_type, display_name):
        if self._upload_error:
            raise self._upload_error
        self.uploaded.append(display_name)
        return {"name": f"files/{len(self.uploaded)}", "uri": "https://example.invalid/files/1"}

    def wait_until_active(self, file_info, **_kwargs):
        return {**file_info, "state": "ACTIVE"}

    def assess(self, file_uri, mime_type, prompt):
        self.prompts.append(prompt)
        if self._assess_error:
            raise self._assess_error
        index = min(len(self.prompts) - 1, len(self._replies) - 1)
        return self._replies[index]

    def delete_file(self, name):
        self.deleted.append(name)
        return self._delete_ok


class ClientBehaviourTests(unittest.TestCase):
    def test_key_is_never_placed_in_the_url_or_repr(self) -> None:
        seen: list[str] = []

        def transport(method, url, headers, body):
            seen.append(url)
            self.assertEqual(headers.get("x-goog-api-key"), "secret-key")
            return 200, {}, b'{"name":"models/gemini-3.8-flash","displayName":"Gemini"}'

        client = GeminiClient("secret-key", "gemini-3.8-flash", transport=transport)
        self.assertNotIn("secret-key", repr(client))
        result = client.test_connection()
        self.assertTrue(result["ok"])
        self.assertNotIn("secret-key", " ".join(seen))

    def test_missing_key_is_reported_before_any_request(self) -> None:
        with self.assertRaises(MissingKeyError):
            GeminiClient("   ")

    def test_rate_limit_is_retried_with_backoff(self) -> None:
        calls: list[str] = []

        def transport(method, url, headers, body):
            calls.append(url)
            if len(calls) == 1:
                return 429, {"Retry-After": "0"}, b'{"error":{"message":"rate limit"}}'
            return 200, {}, b'{"candidates":[{"content":{"parts":[{"text":"{}"}]}}]}'

        client = GeminiClient("k", transport=transport, max_retries=2, sleep=lambda _s: None)
        self.assertEqual(client.assess("uri", "video/mp4", "prompt"), "{}")
        self.assertEqual(len(calls), 2)

    def test_exhausted_rate_limit_raises_rate_limited(self) -> None:
        def transport(method, url, headers, body):
            return 429, {}, b'{"error":{"message":"resource exhausted"}}'

        client = GeminiClient("k", transport=transport, max_retries=1, sleep=lambda _s: None)
        with self.assertRaises(GeminiError) as caught:
            client.assess("uri", "video/mp4", "prompt")
        self.assertIn(caught.exception.code, ("rate_limited", "quota_exceeded"))

    def test_quota_exhaustion_does_not_crash(self) -> None:
        def transport(method, url, headers, body):
            return 429, {}, b'{"error":{"message":"quota exceeded for free tier"}}'

        client = GeminiClient("k", transport=transport, max_retries=3, sleep=lambda _s: None)
        with self.assertRaises(RateLimitedError):
            client.assess("uri", "video/mp4", "prompt")

    def test_bad_key_is_reported_clearly(self) -> None:
        def transport(method, url, headers, body):
            return 403, {}, b'{"error":{"message":"API key not valid"}}'

        client = GeminiClient("k", transport=transport, max_retries=2, sleep=lambda _s: None)
        with self.assertRaises(GeminiError) as caught:
            client.test_connection()
        self.assertEqual(caught.exception.code, "invalid_key")

    def test_upload_uses_the_documented_resumable_flow(self) -> None:
        upload_url = "https://upload.example.invalid/session"
        calls: list[tuple[str, str]] = []

        def transport(method, url, headers, body):
            calls.append((method, url))
            if url.endswith("/upload/v1beta/files"):
                self.assertEqual(headers.get("X-Goog-Upload-Command"), "start")
                return 200, {"X-Goog-Upload-URL": upload_url}, b""
            if url == upload_url:
                self.assertEqual(headers.get("X-Goog-Upload-Command"), "upload, finalize")
                return 200, {}, b'{"file":{"name":"files/abc","uri":"files/abc","state":"ACTIVE"}}'
            return 200, {}, b'{"name":"files/abc","state":"ACTIVE"}'

        temporary = ARTIFACTS / "upload.mp4"
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(b"0123456789")
        client = GeminiClient("k", transport=transport)
        info = client.upload_file(str(temporary), "video/mp4", "segment.mp4")
        self.assertEqual(info["name"], "files/abc")
        self.assertEqual([call[0] for call in calls], ["POST", "POST"])

    def test_delete_failure_is_not_fatal(self) -> None:
        def transport(method, url, headers, body):
            return 500, {}, b"{}"

        client = GeminiClient("k", transport=transport, max_retries=0, sleep=lambda _s: None)
        self.assertFalse(client.delete_file("files/abc"))


class EntryPointTests(unittest.TestCase):
    """The script itself must run: a missing `__main__` guard once hid every fix."""

    def test_script_starts_reads_the_key_and_reports_a_bad_request(self) -> None:
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "worker" / "ai_worker.py"),
             "--request", str(ARTIFACTS / "missing-request.json")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
        )
        try:
            stdout, _ = process.communicate(json.dumps({"apiKey": "k"}) + "\n", timeout=60)
        finally:
            process.kill()
        events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        self.assertEqual(process.returncode, 1)
        self.assertEqual(events[0]["code"], "bad_request")


class WorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cls.source = ARTIFACTS / "segment-source.mp4"
        run_ffmpeg([
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=40",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=40",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(cls.source),
        ])

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(ARTIFACTS, ignore_errors=True)

    def request(self, **overrides):
        payload = {
            "jobId": "job-ai-test",
            "source": str(self.source),
            "durationMs": 40_000,
            "model": "gemini-3.8-flash",
            "ai": {"max_windows": 1, "max_sample_windows": 0, "window_seconds": 10,
                   "segment_seconds": 12},
            "candidates": [{"id": 3, "start_ms": 5_000, "peak_ms": 12_000,
                            "end_ms": 20_000, "score": 55, "signals": ["visual_activity"]}],
        }
        payload.update(overrides)
        return payload

    def run_worker(self, request, key="test-key", client=None, cancelled=None):
        cancelled = cancelled or threading.Event()
        factory = (lambda *_args, **_kwargs: client) if client is not None else None
        with self._capture() as events:
            code = ai_worker.run(request, key, cancelled, client_factory=factory)
        return code, events

    class _capture:
        def __enter__(self):
            self.events: list[dict] = []
            self.original = ai_worker.send

            def fake(event_type, **data):
                self.events.append({"type": event_type, **data})

            ai_worker.send = fake
            return self.events

        def __exit__(self, *_exc):
            ai_worker.send = self.original
            return False

    def test_successful_window_emits_evidence_and_cleans_up(self) -> None:
        client = FakeClient()
        code, events = self.run_worker(self.request(), client=client)
        self.assertEqual(code, 0)
        item = next(event for event in events if event["type"] == "ai_item")["item"]
        self.assertEqual(item["candidateId"], 3)
        self.assertTrue(item["isHighlight"])
        self.assertEqual(item["eventType"], "goal")
        self.assertIn("Goal", item["aiTitle"])
        self.assertGreater(item["combinedScore"], 0)
        self.assertEqual(client.deleted, ["files/1"])
        self.assertTrue(any(event["type"] == "ai_complete" for event in events))
        self.assertIn("gaming highlight", client.prompts[0])

    def test_upload_failure_leaves_no_remote_or_temporary_file(self) -> None:
        client = FakeClient(upload_error=GeminiError("upload broke", code="upload_failed"))
        code, events = self.run_worker(self.request(), client=client)
        item = next(event for event in events if event["type"] == "ai_item")["item"]
        self.assertTrue(item["rejected"])
        self.assertEqual(item["code"], "upload_failed")
        self.assertEqual(client.deleted, [])
        self.assertEqual(list(ARTIFACTS.glob("window-*.mp4")), [])
        self.assertEqual(code, 0)

    def test_assessment_cleanup_still_happens_on_failure(self) -> None:
        client = FakeClient(assess_error=GeminiError("model failed", code="http_error"))
        _, events = self.run_worker(self.request(), client=client)
        self.assertEqual(client.deleted, ["files/1"])
        self.assertTrue(next(e for e in events if e["type"] == "ai_item")["item"]["rejected"])

    def test_invalid_json_is_recorded_as_a_rejection(self) -> None:
        client = FakeClient(replies=["this is not json"])
        _, events = self.run_worker(self.request(), client=client)
        item = next(event for event in events if event["type"] == "ai_item")["item"]
        self.assertEqual(item["code"], "invalid_json")
        self.assertEqual(client.deleted, ["files/1"])

    def test_rate_limit_stops_early_without_failing_the_batch(self) -> None:
        client = FakeClient(assess_error=RateLimitedError("quota used up"))
        code, events = self.run_worker(self.request(), client=client)
        complete = next(event for event in events if event["type"] == "ai_complete")
        self.assertTrue(complete["rateLimited"])
        self.assertEqual(code, 1)
        self.assertEqual(client.deleted, ["files/1"])

    def test_missing_key_reports_missing_key(self) -> None:
        code, events = self.run_worker(self.request(), key="")
        self.assertEqual(code, 1)
        self.assertEqual(events[-1]["code"], "missing_key")

    def test_cancellation_emits_no_assessment(self) -> None:
        cancelled = threading.Event()
        cancelled.set()
        client = FakeClient()
        code, events = self.run_worker(self.request(), client=client, cancelled=cancelled)
        self.assertEqual(code, 2)
        self.assertFalse(any(event["type"] == "ai_item" for event in events))
        self.assertEqual(client.uploaded, [])
        complete = next(event for event in events if event["type"] == "ai_complete")
        self.assertTrue(complete["cancelled"])

    def test_no_candidates_still_completes_cleanly(self) -> None:
        code, events = self.run_worker(
            self.request(candidates=[], ai={"max_windows": 2, "max_sample_windows": 0}),
            client=FakeClient(),
        )
        self.assertEqual(code, 0)
        self.assertTrue(any(event["type"] == "ai_complete" for event in events))

    def test_segment_extraction_produces_a_playable_mp4(self) -> None:
        info = media.inspect_media(str(self.source))
        window = {"start_ms": 2_000, "end_ms": 8_000, "index": 0}
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "window-00.mp4"
            ai_worker.extract_segment(str(self.source), info, window, destination, 480)
            self.assertTrue(destination.is_file())
            probed = media.inspect_media(str(destination))
            self.assertLessEqual(probed["display_width"], 480)
            self.assertTrue(probed["has_audio"])
            self.assertAlmostEqual(probed["duration_seconds"], 6.0, delta=0.4)


if __name__ == "__main__":
    unittest.main()
