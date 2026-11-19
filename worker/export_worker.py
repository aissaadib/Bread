from __future__ import annotations

import argparse
import json
import sys
import threading
import traceback
from pathlib import Path

from luna_worker import export, framing, media
from luna_worker.protocol import log


def send(event_type: str, **data) -> None:
    message = {"type": event_type, **data}
    sys.stdout.write(json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True, help="Path to the batch request JSON")
    args = parser.parse_args()

    try:
        request = json.loads(Path(args.request).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        send("error", message=f"The export request could not be read: {exc}")
        return 1
    finally:
        Path(args.request).unlink(missing_ok=True)

    batch_id = str(request.get("batchId", ""))
    items = request.get("items") or []
    if not batch_id or not items:
        send("error", message="The export request is missing its batch or clip items.", batchId=batch_id)
        return 1
    if len(items) > 50:
        send("error", message="A single export batch is limited to 50 clips.", batchId=batch_id)
        return 1

    cancelled = threading.Event()

    def read_controls() -> None:
        for line in sys.stdin:
            try:
                command = json.loads(line)
            except json.JSONDecodeError:
                continue
            if command.get("command") == "cancel" and command.get("batchId", batch_id) == batch_id:
                cancelled.set()
                return
        cancelled.set()

    threading.Thread(target=read_controls, daemon=True).start()

    aspect = str(request.get("aspect", "vertical"))
    mode = str(request.get("mode", "full_frame"))
    crop_x = float(request.get("cropX", 0.5))
    crop_y = float(request.get("cropY", 0.5))
    crf = int(request.get("crf", 21))
    captions_enabled = bool(request.get("captions", False))
    transcript = request.get("transcript") or []
    source = str(request.get("source", ""))
    output_dir = str(request.get("outputDir", ""))

    try:
        framing.compute_layout(1920, 1080, aspect, mode, crop_x, crop_y)
        info = media.inspect_media(source)
        Path(output_dir).mkdir(parents=True, exist_ok=True)
    except (ValueError, OSError, media.MediaError) as exc:
        send("error", message=str(exc), batchId=batch_id)
        return 1

    send("batch_started", batchId=batch_id, total=len(items))
    completed = 0
    failed = 0

    for index, item in enumerate(items):
        item_id = str(item.get("id", ""))
        if not item_id:
            continue
        if cancelled.is_set():
            send("item_cancelled", batchId=batch_id, itemId=item_id)
            continue
        title = str(item.get("title", "Highlight"))
        start_ms = int(item.get("startMs", 0))
        end_ms = int(item.get("endMs", 0))

        def on_started(path: str, current: str = item_id) -> None:
            send("item_started", batchId=batch_id, itemId=current, outputPath=path, stage="preparing",
                 overallProgress=round(index / len(items) * 100, 1))

        def on_progress(fraction: float, current: str = item_id, item_index: int = index) -> None:
            send(
                "item_progress", batchId=batch_id, itemId=current,
                progress=round(max(0.0, min(1.0, fraction)) * 100, 1),
                overallProgress=round((item_index + max(0.0, min(1.0, fraction))) / len(items) * 100, 1),
            )

        def on_stage(stage: str, current: str = item_id) -> None:
            send("item_stage", batchId=batch_id, itemId=current, stage=stage)

        try:
            destination = export.render_clip(
                source, info, output_dir, title, index + 1, start_ms, end_ms,
                aspect, mode, crop_x, crop_y, crf, captions_enabled, transcript,
                on_progress, cancelled, on_started, on_stage,
            )
            completed += 1
            send("item_completed", batchId=batch_id, itemId=item_id, outputPath=destination,
                 overallProgress=round((index + 1) / len(items) * 100, 1))
        except export.ExportCancelled:
            send("item_cancelled", batchId=batch_id, itemId=item_id)
            cancelled.set()
        except (export.ExportError, OSError, ValueError) as exc:
            failed += 1
            send("item_failed", batchId=batch_id, itemId=item_id, message=str(exc),
                 overallProgress=round((index + 1) / len(items) * 100, 1))
        except Exception as exc:  # Defensive: never leave a batch without a terminal event.
            failed += 1
            log(f"Unexpected export failure: {traceback.format_exc()}")
            send("item_failed", batchId=batch_id, itemId=item_id,
                 message=f"The clip could not be rendered ({type(exc).__name__}).",
                 overallProgress=round((index + 1) / len(items) * 100, 1))

    was_cancelled = cancelled.is_set()
    send("batch_completed", batchId=batch_id, cancelled=was_cancelled,
         completed=completed, failed=failed, total=len(items),
         overallProgress=None if was_cancelled else 100)
    return 2 if was_cancelled else 0


if __name__ == "__main__":
    raise SystemExit(main())
