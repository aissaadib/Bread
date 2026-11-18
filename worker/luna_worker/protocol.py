from __future__ import annotations

import json
import sys
from typing import Any


def emit(job_id: str, event_type: str, **data: Any) -> None:
    message = {"job_id": job_id, "type": event_type, **data}
    sys.stdout.write(json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n")
    sys.stdout.flush()


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)
