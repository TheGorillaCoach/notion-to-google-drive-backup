"""Thin wrapper around notion-client: global rate limiting, retries, pagination."""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Callable, Iterator

import httpx
from notion_client import APIErrorCode, APIResponseError, Client
from notion_client.errors import HTTPResponseError, RequestTimeoutError

log = logging.getLogger(__name__)

# Notion allows an average of 3 requests/second per integration.
DEFAULT_RATE = 2.8
MAX_RETRIES = 7

_RETRYABLE_CODES = {
    APIErrorCode.RateLimited,
    APIErrorCode.InternalServerError,
    APIErrorCode.ServiceUnavailable,
    APIErrorCode.ConflictError,
}


class _Throttle:
    """Thread-safe spacing of requests to stay under the rate limit."""

    def __init__(self, rate: float) -> None:
        self._interval = 1.0 / rate
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self._interval
        delay = start - now
        if delay > 0:
            time.sleep(delay)

    def pause(self, seconds: float) -> None:
        """Push back everyone (used after a 429)."""
        with self._lock:
            self._next = max(self._next, time.monotonic() + seconds)


class NotionAPI:
    def __init__(self, token: str, rate: float = DEFAULT_RATE) -> None:
        self.client = Client(auth=token, timeout_ms=60_000)
        self._throttle = _Throttle(rate)
        self.request_count = 0
        self._count_lock = threading.Lock()

    # -- core -------------------------------------------------------------
    def call(self, fn: Callable[..., Any], **kwargs: Any) -> Any:
        for attempt in range(1, MAX_RETRIES + 1):
            self._throttle.wait()
            with self._count_lock:
                self.request_count += 1
            try:
                return fn(**kwargs)
            except APIResponseError as e:
                if e.code not in _RETRYABLE_CODES or attempt == MAX_RETRIES:
                    raise
                retry_after = _retry_after(e)
                wait = retry_after or min(60, 2 ** attempt) + random.random()
                if e.code == APIErrorCode.RateLimited:
                    self._throttle.pause(wait)
                log.warning("Notion %s; retrying in %.1fs (attempt %d)", e.code, wait, attempt)
                time.sleep(wait)
            except (RequestTimeoutError, HTTPResponseError, httpx.TransportError) as e:
                if attempt == MAX_RETRIES:
                    raise
                wait = min(60, 2 ** attempt) + random.random()
                log.warning("Network error %s; retrying in %.1fs", type(e).__name__, wait)
                time.sleep(wait)
        raise RuntimeError("unreachable")

    def paginate(self, fn: Callable[..., Any], **kwargs: Any) -> Iterator[dict]:
        cursor = None
        while True:
            args = dict(kwargs, page_size=100)
            if cursor:
                args["start_cursor"] = cursor
            resp = self.call(fn, **args)
            yield from resp.get("results", [])
            if not resp.get("has_more"):
                return
            cursor = resp.get("next_cursor")

    # -- endpoints used by the exporter -------------------------------------
    def search_all(self) -> Iterator[dict]:
        return self.paginate(self.client.search)

    def query_data_source(self, data_source_id: str) -> Iterator[dict]:
        return self.paginate(self.client.data_sources.query, data_source_id=data_source_id)

    def block_children(self, block_id: str) -> list[dict]:
        return list(self.paginate(self.client.blocks.children.list, block_id=block_id))

    def retrieve_block(self, block_id: str) -> dict:
        return self.call(self.client.blocks.retrieve, block_id=block_id)

    def list_users(self) -> list[dict]:
        return list(self.paginate(self.client.users.list))


def _retry_after(e: APIResponseError) -> float | None:
    headers = getattr(e, "headers", None) or {}
    try:
        value = headers.get("retry-after") or headers.get("Retry-After")
        return float(value) if value else None
    except (TypeError, ValueError):
        return None
