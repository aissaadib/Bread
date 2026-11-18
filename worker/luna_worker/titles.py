from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request


def _clean(title: str) -> str:
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", title)
    title = re.sub(r"\s+", " ", title).strip(" .-_")
    return title[:72].rstrip(" .") or "Highlight"


def generate(candidate: dict, transcript: list[dict] | None = None) -> tuple[str, str]:
    model = os.environ.get("LUNA_TITLE_MODEL", "").strip()
    if model:
        endpoint = os.environ.get("LUNA_TITLE_ENDPOINT", "http://127.0.0.1:11434/api/generate")
        parsed = urllib.parse.urlparse(endpoint)
        if parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
            context = " ".join(
                str(item.get("text", "")).strip()
                for item in (transcript or [])
                if item.get("start_seconds", 0) * 1000 <= candidate["end_ms"]
                and item.get("end_seconds", 0) * 1000 >= candidate["start_ms"]
            )[:500]
            prompt = (
                "Create a short, accurate highlight title (3 to 8 words). "
                "Use only this transcript and evidence; never invent game events. "
                f"Evidence: {candidate.get('explanation', '')}. "
                f"Transcript: {context or 'No transcript available.'}"
            )
            payload = json.dumps({"model": model, "prompt": prompt, "stream": False, "options": {"temperature": 0.2}}).encode()
            request = urllib.request.Request(endpoint, data=payload, headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=8) as response:
                    result = json.loads(response.read().decode("utf-8"))
                title = _clean(str(result.get("response", "")))
                if title != "Highlight":
                    return title, "local_model"
            except (OSError, ValueError, KeyError, json.JSONDecodeError, urllib.error.URLError):
                pass

    peak_seconds = max(0, int(candidate["peak_ms"] / 1000))
    timestamp = f"{peak_seconds // 60:02d}-{peak_seconds % 60:02d}"
    signals = set(candidate.get("signals", []))
    if "audio_anomaly" in signals:
        title = f"Unexpected Audio Spike {timestamp}"
    elif "visual_activity" in signals:
        title = f"Visual Moment {timestamp}"
    else:
        title = f"Highlight {timestamp}"
    return _clean(title), "evidence_fallback"
