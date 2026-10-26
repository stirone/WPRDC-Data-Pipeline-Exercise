"""HTTP client that logs every attempt, including retries, to ops.fetch_attempt.

We deliberately don't use ckanapi: it raises a generic error on blank or
HTML responses and hides status, headers, body and retries, which are
exactly what we need to diagnose WPRDC's intermittent failures.

Responses are classified by what actually came back, not by HTTP status.
WPRDC sits behind CloudFront -> nginx -> CKAN, and a failing layer can
return a 200 with an empty body or an HTML error page.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import duckdb
import requests

from wprdc import config
from wprdc.db import utcnow

SNIPPET_CHARS = 500
LOGGED_HEADERS = (
    "server", "via", "x-cache", "x-amz-cf-id", "x-amz-cf-pop", "retry-after",
    "content-type", "content-length", "content-disposition", "date", "age",
    "cache-control",
)

OUTCOMES = {
    "ok",
    "empty_body",        # 2xx with zero bytes
    "html_body",         # HTML where JSON/CSV was expected (proxy/CDN error page)
    "non_json",          # unparseable body on a JSON endpoint
    "unexpected_body",   # parseable but wrong shape (e.g. CSV without expected header)
    "ckan_error",        # CKAN envelope with success=false
    "http_4xx",
    "http_5xx",
    "rate_limited",      # 429
    "timeout",
    "connection_error",
    "truncated",         # connection dropped mid-body
    "client_error",      # our own bug or local I/O failure; never retried
}
RETRYABLE = {
    "empty_body", "html_body", "non_json", "unexpected_body", "http_5xx",
    "rate_limited", "timeout", "connection_error", "truncated",
}


class FetchError(RuntimeError):
    def __init__(self, request_key: str, outcome: str, detail: str):
        super().__init__(f"{request_key}: {outcome} after retries ({detail})")
        self.request_key = request_key
        self.outcome = outcome


@dataclass
class Attempt:
    run_id: str
    request_key: str
    endpoint: str
    attempt_no: int
    url: str
    params: dict | None
    started_at: datetime
    method: str = "GET"
    ttfb_ms: float | None = None
    elapsed_ms: float | None = None
    http_status: int | None = None
    outcome: str = "client_error"
    error_class: str | None = None
    error_message: str | None = None
    content_type: str | None = None
    content_length: int | None = None
    bytes_received: int | None = None
    body_sha256: str | None = None
    body_snippet: str | None = None
    ckan_success: bool | None = None
    headers: dict[str, str] = field(default_factory=dict)
    will_retry: bool = False
    backoff_s: float | None = None


@dataclass
class JsonResponse:
    payload: dict
    body: bytes
    sha256: str


@dataclass
class DownloadResult:
    path: Path
    sha256: str
    bytes: int


def _looks_like_html(content_type: str | None, head: bytes) -> bool:
    if content_type and "html" in content_type.lower():
        return True
    return head.lstrip()[:15].lower().startswith((b"<!doctype", b"<html", b"<"))


def classify_json(status: int, content_type: str | None, body: bytes) -> tuple[str, dict | None]:
    """Return (outcome, parsed payload or None) for a CKAN action API response."""
    payload = None
    try:
        payload = json.loads(body) if body.strip() else None
    except ValueError:
        pass
    if status == 429:
        return "rate_limited", payload
    if status >= 500:
        return "http_5xx", payload
    if isinstance(payload, dict) and payload.get("success") is False:
        return "ckan_error", payload  # CKAN reports 4xx this way; not retryable
    if status >= 400:
        return "http_4xx", payload
    if not body.strip():
        return "empty_body", None
    if payload is None:
        return ("html_body" if _looks_like_html(content_type, body) else "non_json"), None
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return "unexpected_body", payload
    return "ok", payload


def classify_download(
    status: int, content_type: str | None, head: bytes, bytes_received: int,
    expected_prefix: bytes | None,
) -> str:
    """Classify a streamed file download from its status and first bytes."""
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "http_5xx"
    if status >= 400:
        return "http_4xx"
    if bytes_received == 0:
        return "empty_body"
    if _looks_like_html(content_type, head):
        return "html_body"
    if expected_prefix and not head.removeprefix(b"\xef\xbb\xbf").startswith(expected_prefix):
        return "unexpected_body"
    return "ok"


def _snippet(data: bytes) -> str:
    return data[: SNIPPET_CHARS * 4].decode("utf-8", errors="replace")[:SNIPPET_CHARS]


class LoggedHttp:
    def __init__(
        self,
        con: duckdb.DuckDBPyConnection,
        run_id: str,
        *,
        max_attempts: int = 5,
        base_backoff_s: float = 2.0,
        max_backoff_s: float = 60.0,
        timeout: tuple[float, float] = (10.0, 120.0),  # (connect, read between bytes)
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.con = con
        self.run_id = run_id
        self.max_attempts = max_attempts
        self.base_backoff_s = base_backoff_s
        self.max_backoff_s = max_backoff_s
        self.timeout = timeout
        self.sleep = sleep
        self.session = requests.Session()
        self.session.headers["User-Agent"] = config.USER_AGENT

    # -- public API -----------------------------------------------------------

    def get_json(self, endpoint: str, request_key: str, url: str,
                 params: dict | None = None) -> JsonResponse:
        def handle(resp: requests.Response, att: Attempt) -> JsonResponse | None:
            body = resp.content
            att.bytes_received = len(body)
            att.body_sha256 = hashlib.sha256(body).hexdigest()
            att.body_snippet = _snippet(body)
            att.outcome, payload = classify_json(resp.status_code, att.content_type, body)
            if isinstance(payload, dict) and "success" in payload:
                att.ckan_success = bool(payload["success"])
                if payload.get("error"):
                    att.error_message = json.dumps(payload["error"])[:2000]
            if att.outcome == "ok":
                return JsonResponse(payload, body, att.body_sha256)
            return None

        return self._request(endpoint, request_key, url, params, handle)

    def download(self, endpoint: str, request_key: str, url: str, dest: Path,
                 params: dict | None = None,
                 expected_prefix: bytes | None = None) -> DownloadResult:
        """Stream a body to dest, atomically: dest only appears if the attempt is ok."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")

        def handle(resp: requests.Response, att: Attempt) -> DownloadResult | None:
            digest = hashlib.sha256()
            head = b""
            n = 0
            try:
                with open(part, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        if len(head) < SNIPPET_CHARS * 4:
                            head += chunk[: SNIPPET_CHARS * 4 - len(head)]
                        digest.update(chunk)
                        fh.write(chunk)
                        n += len(chunk)
            finally:
                att.bytes_received = n
                att.body_snippet = _snippet(head)
            att.body_sha256 = digest.hexdigest()
            att.outcome = classify_download(
                resp.status_code, att.content_type, head, n, expected_prefix)
            if att.content_length is not None and n < att.content_length:
                att.outcome = "truncated"
            if att.outcome != "ok":
                part.unlink(missing_ok=True)
                return None
            os.replace(part, dest)
            return DownloadResult(dest, att.body_sha256, n)

        try:
            return self._request(endpoint, request_key, url, params, handle)
        finally:
            part.unlink(missing_ok=True)

    # -- retry loop -----------------------------------------------------------

    def _backoff(self, attempt_no: int, retry_after: str | None) -> float:
        if retry_after and retry_after.strip().isdigit():
            return min(float(retry_after), self.max_backoff_s)
        exp = self.base_backoff_s * 2 ** (attempt_no - 1)
        return min(self.max_backoff_s, exp) * random.uniform(0.5, 1.0)

    def _request(self, endpoint, request_key, url, params, handle):
        full_url = requests.Request("GET", url, params=params).prepare().url
        for attempt_no in range(1, self.max_attempts + 1):
            att = Attempt(self.run_id, request_key, endpoint, attempt_no,
                          full_url, params, utcnow())
            t0 = time.perf_counter()
            result = None
            unexpected: BaseException | None = None
            try:
                with self.session.get(full_url, timeout=self.timeout, stream=True) as resp:
                    att.ttfb_ms = (time.perf_counter() - t0) * 1000
                    att.http_status = resp.status_code
                    att.headers = {k.lower(): v for k, v in resp.headers.items()
                                   if k.lower() in LOGGED_HEADERS}
                    att.content_type = resp.headers.get("Content-Type")
                    cl = resp.headers.get("Content-Length")
                    att.content_length = int(cl) if cl and cl.isdigit() else None
                    result = handle(resp, att)
            except requests.exceptions.ChunkedEncodingError as exc:
                att.outcome = "truncated"
                att.error_class, att.error_message = type(exc).__name__, str(exc)[:2000]
            except requests.Timeout as exc:
                att.outcome = "timeout"
                att.error_class, att.error_message = type(exc).__name__, str(exc)[:2000]
            except requests.RequestException as exc:
                att.outcome = "connection_error"
                att.error_class, att.error_message = type(exc).__name__, str(exc)[:2000]
            except BaseException as exc:  # local bug / disk error / Ctrl-C: log, then re-raise
                att.outcome = "client_error"
                att.error_class, att.error_message = type(exc).__name__, str(exc)[:2000]
                unexpected = exc
            att.elapsed_ms = (time.perf_counter() - t0) * 1000

            retry = (unexpected is None and att.outcome in RETRYABLE
                     and attempt_no < self.max_attempts)
            att.will_retry = retry
            att.backoff_s = self._backoff(attempt_no, att.headers.get("retry-after")) if retry else None
            self._log(att)

            if unexpected is not None:
                raise unexpected
            if att.outcome == "ok":
                return result
            if not retry:
                raise FetchError(request_key, att.outcome,
                                 f"HTTP {att.http_status}, {att.error_message or att.body_snippet!r:.200}")
            self.sleep(att.backoff_s)
        raise AssertionError("unreachable")

    def _log(self, a: Attempt) -> None:
        h = a.headers
        self.con.execute(
            """
            INSERT INTO ops.fetch_attempt (
                run_id, request_key, endpoint, attempt_no, method, url, params,
                started_at, ttfb_ms, elapsed_ms, http_status, outcome, error_class,
                error_message, content_type, content_length, bytes_received,
                body_sha256, body_snippet, ckan_success, server, via, x_cache, cf_id,
                retry_after, response_headers, will_retry, backoff_s)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [a.run_id, a.request_key, a.endpoint, a.attempt_no, a.method, a.url,
             json.dumps(a.params) if a.params is not None else None,
             a.started_at, a.ttfb_ms, a.elapsed_ms, a.http_status, a.outcome,
             a.error_class, a.error_message, a.content_type, a.content_length,
             a.bytes_received, a.body_sha256, a.body_snippet, a.ckan_success,
             h.get("server"), h.get("via"), h.get("x-cache"), h.get("x-amz-cf-id"),
             h.get("retry-after"), json.dumps(h), a.will_retry, a.backoff_s],
        )
