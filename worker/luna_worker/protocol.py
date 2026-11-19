from __future__ import annotations

import json
import sys
import threading
from typing import Any

_EMIT_LOCK = threading.Lock()


def emit(job_id: str, event_type: str, **data: Any) -> None:
    message = {"job_id": job_id, "type": event_type, **data}
    # ASCII escapes keep the JSONL protocol safe regardless of the host code page.
    line = json.dumps(message, separators=(",", ":"), ensure_ascii=True) + "\n"
    with _EMIT_LOCK:
        sys.stdout.write(line)
        sys.stdout.flush()


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)
