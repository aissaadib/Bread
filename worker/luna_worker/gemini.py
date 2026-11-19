"""Google Gemini Developer API client for Luna's optional cloud analysis.

Only the documented REST endpoints of the Google Gemini Developer API are used
(https://ai.google.dev/gemini-api/docs). The API key travels in the
``x-goog-api-key`` header, never in a URL query string, so it cannot leak into
process listings or diagnostic output.

The transport is injectable so the whole client can be exercised by the test
suite without a network connection or a real key.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from . import ai

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"
UPLOAD_PATH = "/upload/v1beta/files"
API_PATH = "/v1beta"

#: Status codes that are worth retrying with backoff.
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, dict[str, str], bytes]]


class GeminiError(RuntimeError):
    def __init__(self, message: str, *, code: str = "gemini_error",
                 status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable


class MissingKeyError(GeminiError):
    def __init__(self, message: str = "No Gemini API key is stored on this device."):
        super().__init__(message, code="missing_key")


class RateLimitedError(GeminiError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message, code="rate_limited", status=status, retryable=False)


class QuotaExceededError(GeminiError):
    def __init__(self, message: str):
        super().__init__(message, code="quota_exceeded", retryable=False)


def _urllib_transport(method: str, url: str, headers: dict[str, str],
                      body: bytes | None) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as error:
        try:
            payload = error.read()
        except OSError:
            payload = b""
        return error.code, dict(error.headers.items()) if error.headers else {}, payload
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as error:
        raise GeminiError(f"Network error contacting Gemini: {error}", code="network",
                          retryable=True) from error


class GeminiClient:
    """Minimal, explicit client: upload one segment, ask one question, clean up."""

    def __init__(self, api_key: str, model: str | None = None, *,
                 base_url: str = DEFAULT_BASE_URL,
                 timeout_seconds: float = 150.0,
                 max_retries: int = 3,
                 transport: Transport | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        key = (api_key or "").strip()
        if not key:
            raise MissingKeyError()
        self._api_key = key
        self.model = (model or ai.DEFAULT_MODEL).strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = max(10.0, float(timeout_seconds))
        self.max_retries = max(0, int(max_retries))
        self._transport = transport or _urllib_transport
        self._sleep = sleep
        self.requests_made = 0

    # ------------------------------------------------------------------ HTTP
    def __repr__(self) -> str:  # never leak the key through repr()/logs
        return f"GeminiClient(model={self.model!r})"

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"x-goog-api-key": self._api_key}
        if extra:
            headers.update(extra)
        return headers

    def _call(self, method: str, url: str, *, body: bytes | None = None,
              headers: dict[str, str] | None = None,
              retry_on_429: bool = True) -> tuple[int, dict[str, str], bytes]:
        """Perform a request, retrying transient failures with exponential backoff."""
        delay = 1.5
        last_error: GeminiError | None = None
        for attempt in range(self.max_retries + 1):
            self.requests_made += 1
            try:
                status, response_headers, payload = self._transport(
                    method, url, self._headers(headers), body)
            except GeminiError as error:
                if not error.retryable or attempt >= self.max_retries:
                    raise
                last_error = error
                self._sleep(delay)
                delay = min(20.0, delay * 2)
                continue

            if status < 300:
                return status, response_headers, payload
            detail = _safe_error_text(payload)
            if status == 429:
                if not retry_on_429 or attempt >= self.max_retries:
                    if _is_quota(detail):
                        raise QuotaExceededError(
                            "Gemini reports the free-tier quota is exhausted. "
                            "Local analysis still works; try cloud analysis later." )
                    raise RateLimitedError(
                        f"Gemini rate limit reached. {detail}".strip(), status=status)
                retry_after = _retry_after_seconds(response_headers)
                self._sleep(retry_after if retry_after is not None else delay)
                delay = min(20.0, delay * 2)
                continue
            if status in RETRYABLE_STATUS and attempt < self.max_retries:
                self._sleep(delay)
                delay = min(20.0, delay * 2)
                continue
            if status in (401, 403):
                raise GeminiError(
                    "Gemini rejected the stored API key. Check that Cloud AI Analysis "
                    "is enabled and the key is valid.", code="invalid_key",
                    status=status)
            if status == 404:
                raise GeminiError(
                    f"Gemini model '{self.model}' is not available for this key. {detail}".strip(),
                    code="model_unavailable", status=status)
            raise GeminiError(f"Gemini request failed (HTTP {status}). {detail}".strip(),
                              code="http_error", status=status)
        raise last_error or GeminiError("Gemini request failed.", code="unknown")

    def _json(self, method: str, path: str, payload: dict[str, Any] | None = None,
              *, retry_on_429: bool = True) -> Any:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"} if body else {}
        _, _, raw = self._call(method, f"{self.base_url}{path}", body=body,
                               headers=headers, retry_on_429=retry_on_429)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GeminiError("Gemini returned a response that was not JSON.",
                              code="invalid_response") from error

    # ------------------------------------------------------------- endpoints
    def test_connection(self) -> dict[str, Any]:
        """Cheap authenticated call used by the Settings 'Test connection' button."""
        started = time.monotonic()
        data = self._json("GET", f"{API_PATH}/models/{self.model}")
        elapsed_ms = int((time.monotonic() - started) * 1000)
        name = data.get("name") or f"models/{self.model}"
        return {
            "ok": True,
            "model": name.split("/")[-1],
            "displayName": data.get("displayName"),
            "latencyMs": elapsed_ms,
        }

    def upload_file(self, path: str, mime_type: str, display_name: str) -> dict[str, Any]:
        """Resumable upload of one temporary segment; returns the file resource."""
        try:
            with open(path, "rb") as handle:
                size = len(handle.read())
            with open(path, "rb") as handle:
                init_headers = {
                    "X-Goog-Upload-Protocol": "resumable",
                    "X-Goog-Upload-Command": "start",
                    "X-Goog-Upload-Header-Content-Length": str(size),
                    "X-Goog-Upload-Header-Content-Type": mime_type,
                    "Content-Type": "application/json",
                }
                body = json.dumps({"file": {"display_name": display_name}}).encode("utf-8")
                _, headers, payload = self._call(
                    "POST", f"{self.base_url}{UPLOAD_PATH}", body=body, headers=init_headers)
                upload_url = _header(headers, "x-goog-upload-url")
                if not upload_url:
                    raise GeminiError("Gemini did not return an upload URL.",
                                      code="upload_failed")
                upload_headers = {
                    "X-Goog-Upload-Command": "upload, finalize",
                    "X-Goog-Upload-Offset": "0",
                    "Content-Length": str(size),
                    "Content-Type": mime_type,
                }
                _, _, final = self._call("POST", upload_url, body=handle.read(),
                                        headers=upload_headers)
        except OSError as error:
            raise GeminiError(f"The temporary segment could not be read: {error}",
                              code="upload_failed") from error
        try:
            data = json.loads(final.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GeminiError("Gemini returned an unreadable upload response.",
                              code="upload_failed") from error
        file_info = data.get("file") or {}
        if not file_info.get("name"):
            raise GeminiError("Gemini did not accept the uploaded segment.",
                              code="upload_failed")
        return file_info

    def wait_until_active(self, file_info: dict[str, Any], *,
                          timeout_seconds: float = 180.0,
                          poll_seconds: float = 2.0) -> dict[str, Any]:
        name = file_info.get("name", "")
        state = (file_info.get("state") or "").upper()
        deadline = time.monotonic() + max(5.0, timeout_seconds)
        while state not in ("ACTIVE", "") and time.monotonic() < deadline:
            self._sleep(poll_seconds)
            refreshed = self._json("GET", f"{API_PATH}/{name}")
            state = (refreshed.get("state") or "").upper()
            file_info = {**file_info, **refreshed}
            if state == "FAILED":
                raise GeminiError("Gemini could not process the uploaded segment.",
                                  code="upload_failed")
        if state not in ("ACTIVE", ""):
            raise GeminiError("Gemini did not finish processing the uploaded segment in time.",
                              code="upload_timeout")
        return file_info

    def delete_file(self, name: str) -> bool:
        """Best-effort remote cleanup; failures never break a local job."""
        if not name:
            return False
        try:
            self._call("DELETE", f"{self.base_url}{API_PATH}/{name}", retry_on_429=False)
            return True
        except GeminiError:
            return False

    def assess(self, file_uri: str, mime_type: str, prompt: str) -> str:
        payload = {
            "contents": [{
                "role": "user",
                "parts": [
                    {"file_data": {"mime_type": mime_type, "file_uri": file_uri}},
                    {"text": prompt},
                ],
            }],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
                "responseSchema": ai.RESPONSE_SCHEMA,
            },
        }
        data = self._json("POST", f"{API_PATH}/models/{self.model}:generateContent", payload)
        candidates = data.get("candidates") or []
        if not candidates:
            blocked = (data.get("promptFeedback") or {}).get("blockReason")
            raise GeminiError(
                f"Gemini returned no assessment{f' ({blocked})' if blocked else ''}.",
                code="no_candidate")
        parts = ((candidates[0].get("content") or {}).get("parts") or [])
        text = "".join(part.get("text", "") for part in parts if isinstance(part, dict)).strip()
        if not text:
            finish = candidates[0].get("finishReason") or "unknown"
            raise GeminiError(f"Gemini returned an empty assessment (finish: {finish}).",
                              code="empty_response")
        return text


def _header(headers: dict[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _retry_after_seconds(headers: dict[str, str]) -> float | None:
    raw = _header(headers, "retry-after")
    if not raw:
        return None
    try:
        return max(0.0, min(30.0, float(raw)))
    except ValueError:
        return None


def _is_quota(detail: str) -> bool:
    lowered = (detail or "").lower()
    return "quota" in lowered or "billing" in lowered or "exceeded" in lowered


def _safe_error_text(payload: bytes) -> str:
    """Truncated, key-free error detail for user-facing messages."""
    if not payload:
        return ""
    try:
        data = json.loads(payload.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return payload.decode("utf-8", "replace")[:300]
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        message = str(error.get("message") or "")
        return message[:300]
    return str(data)[:300]
