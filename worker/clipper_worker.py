from __future__ import annotations

import argparse
import json
import sys
import threading

from luna_worker.pipeline import analyze
from luna_worker.protocol import emit, log


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--config-json", required=True)
    parser.add_argument("--output-dir")
    args = parser.parse_args()
    try:
        config = json.loads(args.config_json)
    except json.JSONDecodeError as exc:
        parser.error(f"invalid analysis configuration: {exc}")
    cancelled = threading.Event()

    def read_controls() -> None:
        for line in sys.stdin:
            try:
                command = json.loads(line)
            except json.JSONDecodeError:
                continue
            if command.get("job_id") == args.job_id and command.get("command") == "cancel":
                cancelled.set()
                return
        # EOF means the desktop host closed while this worker was running.
        cancelled.set()

    threading.Thread(target=read_controls, daemon=True).start()

    def send(event_type: str, **data) -> None:
        emit(args.job_id, event_type, **data)

    try:
        analyze(args.path, cancelled, send, config, args.output_dir)
        return 0
    except InterruptedError as exc:
        send("cancelled", message=str(exc))
        return 2
    except Exception as exc:
        log(f"Worker failure: {type(exc).__name__}: {exc}")
        send("error", code=getattr(exc, "code", "analysis_failed"), message=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
