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

from luna_worker import captions, export, framing, media


def run_ffmpeg(arguments: list[str], cwd: Path | None = None) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *arguments],
                   check=True, capture_output=True, cwd=cwd)


def probe(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def frame_rgb(path: Path, width: int, height: int, timestamp: float = 0.0) -> bytes:
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{timestamp:.3f}", "-i", str(path), "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        check=True, capture_output=True,
    )
    if len(result.stdout) < width * height * 3:
        raise AssertionError(f"Decoded frame is short: {len(result.stdout)} bytes")
    return result.stdout[: width * height * 3]


def region_mean(frame: bytes, width: int, y0: int, y1: int) -> tuple[float, float, float]:
    red = green = blue = 0
    count = 0
    stride = max(1, width // 90)
    for y in range(y0, y1, 6):
        row = y * width * 3
        for x in range(0, width, stride):
            index = row + x * 3
            red += frame[index]
            green += frame[index + 1]
            blue += frame[index + 2]
            count += 1
    return red / count, green / count, blue / count


def colored_pixels(frame: bytes, width: int, start_y: int, end_y: int, channel: int,
                   minimum: int = 120) -> int:
    count = 0
    stride = max(1, width // 100)
    for y in range(start_y, end_y, 6):
        row = y * width * 3
        for x in range(0, width, stride):
            red = frame[row + x * 3]
            green = frame[row + x * 3 + 1]
            blue = frame[row + x * 3 + 2]
            if channel == 0 and red > minimum and blue < 100:
                count += 1
            if channel == 2 and blue > minimum and red < 100:
                count += 1
    return count


class FramingTests(unittest.TestCase):
    def test_landscape_to_vertical_full_frame_keeps_every_edge(self) -> None:
        layout = framing.compute_layout(1920, 1080, "vertical", "full_frame")
        self.assertEqual((layout.canvas_width, layout.canvas_height), (1080, 1920))
        self.assertEqual(layout.foreground.width, 1080)
        self.assertAlmostEqual(layout.foreground.height, 608, delta=2)
        self.assertEqual(layout.foreground.x, 0)
        self.assertAlmostEqual(layout.foreground.y, 656, delta=2)
        self.assertGreater(layout.background.width, 1920)

    def test_landscape_to_vertical_crop_uses_center_band(self) -> None:
        center = framing.compute_layout(1920, 1080, "vertical", "crop", 0.5, 0.5)
        left = framing.compute_layout(1920, 1080, "vertical", "crop", 0.0, 0.5)
        right = framing.compute_layout(1920, 1080, "vertical", "crop", 1.0, 0.5)
        self.assertEqual(center.crop.height, 1080)
        self.assertAlmostEqual(center.crop.width, 608, delta=2)
        self.assertEqual(left.crop.x, 0)
        self.assertGreater(right.crop.x, center.crop.x)
        self.assertEqual(right.crop.x + right.crop.width, 1920)

    def test_square_crop_and_portrait_sources(self) -> None:
        square = framing.compute_layout(1920, 1080, "square", "crop", 0.5, 0.5)
        self.assertEqual((square.crop.width, square.crop.height), (1080, 1080))
        self.assertEqual(square.crop.x, 420)
        portrait = framing.compute_layout(1080, 1920, "vertical", "full_frame")
        self.assertEqual((portrait.foreground.width, portrait.foreground.height), (1080, 1920))
        self.assertFalse(portrait.has_vertical_crop)

    def test_original_preset_uses_source_resolution(self) -> None:
        layout = framing.compute_layout(2560, 1440, "original", "crop")
        self.assertEqual(layout.mode, "original")
        self.assertEqual((layout.canvas_width, layout.canvas_height), (2560, 1440))
        self.assertEqual((layout.foreground.width, layout.foreground.height), (2560, 1440))

    def test_filter_graphs_never_stretch_source(self) -> None:
        full = framing.build_filter_graph(framing.compute_layout(1920, 1080, "vertical", "full_frame"))
        self.assertIn("overlay=x=0:y=656", full)
        self.assertIn("boxblur", full)
        crop = framing.build_filter_graph(framing.compute_layout(1920, 1080, "square", "crop"))
        self.assertIn("crop=1080:1080:420:0", crop)
        original = framing.build_filter_graph(framing.compute_layout(1920, 1080, "original", "original"))
        self.assertIn("scale=1920:1080", original)


class CaptionTests(unittest.TestCase):
    def test_words_are_grouped_into_at_most_two_lines(self) -> None:
        words = [
            {"word": word, "start_seconds": index * 0.28, "end_seconds": index * 0.28 + 0.24}
            for index, word in enumerate(
                ("this", "was", "the", "most", "insane", "finish", "i", "have", "ever", "seen")
            )
        ]
        cues = captions.build_cues([
            {"start_seconds": 0.0, "end_seconds": 3.0, "text": "this was the most insane finish",
             "words": words},
        ], 0, 3000)
        self.assertTrue(cues)
        self.assertTrue(all(len(cue.lines) <= 2 for cue in cues))
        self.assertLessEqual(len(cues[0].lines), 2)

    def test_cues_are_clipped_to_the_export_window(self) -> None:
        cues = captions.build_cues([
            {"start_seconds": 5.0, "end_seconds": 7.0, "text": "before the clip"},
            {"start_seconds": 11.0, "end_seconds": 13.0, "text": "inside the clip"},
        ], 10_000, 20_000)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0].lines, ("inside the clip",))
        self.assertLess(cues[0].start, 1.1)


class RenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = Path(tempfile.mkdtemp(prefix="luna-export-tests-"))
        cls.landscape = cls.temp / "landscape.mp4"
        run_ffmpeg([
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=4",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(cls.landscape),
        ])
        cls.split = cls.temp / "split.mp4"
        run_ffmpeg([
            "-f", "lavfi", "-i", "color=c=red:s=320x180:r=30:d=2",
            "-vf", "drawbox=x=160:y=0:w=160:h=180:color=blue:t=fill",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(cls.split),
        ])
        cls.portrait = cls.temp / "portrait.mp4"
        run_ffmpeg([
            "-f", "lavfi", "-i", "testsrc2=size=180x320:rate=30:duration=2",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(cls.portrait),
        ])
        cls.large = cls.temp / "large.mp4"
        run_ffmpeg([
            "-f", "lavfi", "-i", "testsrc2=size=2560x1440:rate=30:duration=1.2",
            "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000:duration=1.2",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(cls.large),
        ])
        cls.dark = cls.temp / "dark.mp4"
        run_ffmpeg([
            "-f", "lavfi", "-i", "color=c=black:s=320x180:r=30:d=1.5",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(cls.dark),
        ])
        cls.long = cls.temp / "long.mp4"
        run_ffmpeg([
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=10",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(cls.long),
        ])

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.temp, ignore_errors=True)

    def render(self, source: Path, aspect: str, mode: str, name: str,
               start_ms: int = 0, end_ms: int = 1800, crop_x: float = 0.5,
               crop_y: float = 0.5, captions_enabled: bool = False,
               transcript: list[dict] | None = None,
               cancelled: threading.Event | None = None,
               on_progress=None) -> tuple[str, dict]:
        output = self.temp / name
        output.mkdir()
        info = media.inspect_media(str(source))
        progress = on_progress or (lambda _fraction: None)
        destination = export.render_clip(
            str(source), info, str(output), name, 1, start_ms, end_ms, aspect, mode,
            crop_x, crop_y, 21, captions_enabled, transcript,
            progress, cancelled or threading.Event(),
        )
        return destination, probe(Path(destination))

    def test_vertical_full_frame_render_has_expected_container_and_audio(self) -> None:
        destination, data = self.render(self.landscape, "vertical", "full_frame", "vertical-full")
        streams = data["streams"]
        video = next(stream for stream in streams if stream["codec_type"] == "video")
        audio = next(stream for stream in streams if stream["codec_type"] == "audio")
        self.assertEqual((video["width"], video["height"]), (1080, 1920))
        self.assertEqual(video["codec_name"], "h264")
        self.assertEqual(video["pix_fmt"], "yuv420p")
        self.assertEqual(audio["codec_name"], "aac")
        self.assertAlmostEqual(float(data["format"]["duration"]), 1.8, delta=0.25)
        self.assertTrue(Path(destination).is_file())

    def test_square_crop_render(self) -> None:
        _, data = self.render(self.landscape, "square", "crop", "square-crop")
        video = next(stream for stream in data["streams"] if stream["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"]), (1080, 1080))
        self.assertEqual(video["pix_fmt"], "yuv420p")

    def test_original_preset_render_keeps_source_resolution(self) -> None:
        _, data = self.render(self.landscape, "original", "original", "original-preset")
        video = next(stream for stream in data["streams"] if stream["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"]), (320, 180))

    def test_large_source_and_portrait_source(self) -> None:
        _, large = self.render(self.large, "vertical", "full_frame", "large-source", end_ms=1100)
        large_video = next(stream for stream in large["streams"] if stream["codec_type"] == "video")
        self.assertEqual((large_video["width"], large_video["height"]), (1080, 1920))
        _, portrait = self.render(self.portrait, "vertical", "original", "portrait-source", end_ms=1500)
        portrait_video = next(stream for stream in portrait["streams"] if stream["codec_type"] == "video")
        self.assertEqual((portrait_video["width"], portrait_video["height"]), (1080, 1920))

    def test_full_frame_preserves_both_horizontal_edges(self) -> None:
        destination, _ = self.render(self.split, "vertical", "full_frame", "full-frame-content")
        frame = frame_rgb(Path(destination), 1080, 1920)
        red = colored_pixels(frame, 1080, 700, 1200, 0)
        blue = colored_pixels(frame, 1080, 700, 1200, 2)
        self.assertGreater(red, 100)
        self.assertGreater(blue, 100)

    def test_crop_position_changes_which_edge_is_visible(self) -> None:
        left_path, _ = self.render(self.split, "vertical", "crop", "crop-left", crop_x=0.0)
        right_path, _ = self.render(self.split, "vertical", "crop", "crop-right", crop_x=1.0)
        left = frame_rgb(Path(left_path), 1080, 1920)
        right = frame_rgb(Path(right_path), 1080, 1920)
        self.assertGreater(colored_pixels(left, 1080, 300, 1600, 0), colored_pixels(left, 1080, 300, 1600, 2))
        self.assertGreater(colored_pixels(right, 1080, 300, 1600, 2), colored_pixels(right, 1080, 300, 1600, 0))

    def test_render_without_audio_has_no_audio_stream(self) -> None:
        _, data = self.render(self.dark, "square", "original", "no-audio", end_ms=1200)
        self.assertFalse(any(stream["codec_type"] == "audio" for stream in data["streams"]))

    def test_captions_are_burned_into_the_video(self) -> None:
        transcript = [{
            "start_seconds": 0.1, "end_seconds": 1.3, "text": "what a play", "words": [
                {"word": "what", "start_seconds": 0.1, "end_seconds": 0.35},
                {"word": "a", "start_seconds": 0.36, "end_seconds": 0.45},
                {"word": "play", "start_seconds": 0.46, "end_seconds": 0.75},
            ],
        }]
        plain_path, _ = self.render(self.dark, "vertical", "full_frame", "caption-off", end_ms=1300)
        caption_path, _ = self.render(self.dark, "vertical", "full_frame", "caption-on", end_ms=1300,
                                      captions_enabled=True, transcript=transcript)
        plain = frame_rgb(Path(plain_path), 1080, 1920, 0.5)
        captioned = frame_rgb(Path(caption_path), 1080, 1920, 0.5)
        white_plain = sum(1 for index in range(0, len(plain), 3 * 19)
                          if plain[index] > 200 and plain[index + 1] > 200 and plain[index + 2] > 200)
        white_caption = sum(1 for index in range(0, len(captioned), 3 * 19)
                            if captioned[index] > 200 and captioned[index + 1] > 200
                            and captioned[index + 2] > 200)
        self.assertGreater(white_caption, white_plain + 40)

    def test_cancellation_removes_the_partial_file(self) -> None:
        output = self.temp / "cancelled"
        output.mkdir()
        info = media.inspect_media(str(self.long))
        cancelled = threading.Event()

        def progress(fraction: float) -> None:
            if fraction > 0.03:
                cancelled.set()

        with self.assertRaises(export.ExportCancelled):
            export.render_clip(
                str(self.long), info, str(output), "Cancel test", 1, 0, 10_000,
                "vertical", "full_frame", 0.5, 0.5, 23, False, None,
                progress, cancelled,
            )
        self.assertEqual(list(output.glob("*.mp4")), [])
        self.assertEqual(list(output.glob(".*.mp4")), [])

    def test_export_worker_protocol_writes_a_real_file(self) -> None:
        output = self.temp / "worker-output"
        output.mkdir()
        request_path = self.temp / "request.json"
        request_path.write_text(json.dumps({
            "batchId": "batch-test-1",
            "source": str(self.landscape),
            "outputDir": str(output),
            "aspect": "vertical",
            "mode": "full_frame",
            "cropX": 0.5, "cropY": 0.5, "crf": 22, "captions": False,
            "transcript": [],
            "items": [{"id": "item-1", "title": "Worker Clip", "startMs": 0, "endMs": 1500}],
        }), encoding="utf-8")
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "worker" / "export_worker.py"), "--request", str(request_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace",
        )
        events = []
        try:
            assert process.stdout is not None
            for line in process.stdout:
                event = json.loads(line)
                events.append(event)
                if event["type"] in ("batch_completed", "error"):
                    break
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        completed = next(event for event in events if event["type"] == "item_completed")
        self.assertTrue(Path(completed["outputPath"]).is_file())
        self.assertTrue(any(event["type"] == "batch_completed" for event in events))
        self.assertFalse(request_path.exists())


if __name__ == "__main__":
    unittest.main()
