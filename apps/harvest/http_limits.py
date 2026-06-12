"""
Outbound HTTP limits for Jarvis — global + per-host concurrency and SLO-friendly retries.

Each Celery worker process has its own gate instance; effective cluster concurrency
≈ worker_processes × JARVIS_HTTP_MAX_GLOBAL (and per-host × processes for same host).
"""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)


def _host_key(url: str) -> str:
    try:
        p = urlparse(url)
        return (p.netloc or "unknown").lower()
    except Exception:
        return "unknown"


class JarvisFetchGate:
    """
    Thread-safe: one global semaphore + one per hostname.

    Retries (transient only): timeouts, connection errors, HTTP 429, 502, 503, 504.
    Does not retry other 4xx/5xx (caller handles).
    """

    def __init__(
        self,
        max_global: int,
        max_per_host: int,
        retry_max: int,
        retry_base_sec: float,
    ):
        mg = max(1, int(max_global))
        mh = max(1, int(max_per_host))
        self._global = threading.BoundedSemaphore(mg)
        self._per_host_max = mh
        self._host_sem: dict[str, threading.BoundedSemaphore] = {}
        self._host_sem_lock = threading.Lock()
        self.retry_max = max(0, int(retry_max))
        self.retry_base_sec = max(0.05, float(retry_base_sec))

    def _sem_for_host(self, host: str) -> threading.BoundedSemaphore:
        with self._host_sem_lock:
            if host not in self._host_sem:
                self._host_sem[host] = threading.BoundedSemaphore(self._per_host_max)
            return self._host_sem[host]

    @staticmethod
    def _retry_after_seconds(resp: requests.Response) -> float:
        ra = resp.headers.get("Retry-After")
        if ra:
            try:
                return min(120.0, float(ra))
            except ValueError:
                pass
        return 2.0 + random.random() * 0.5

    def request(
        self,
        session: requests.Session,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response:
        # Safety net: never let a harvester call hang a worker forever. Callers
        # may still pass their own timeout; this only fills the gap when omitted.
        kwargs.setdefault("timeout", 30)
        host = _host_key(url)
        hsem = self._sem_for_host(host)
        self._global.acquire()
        try:
            hsem.acquire()
            try:
                return self._execute_with_retries(session, method, url, **kwargs)
            finally:
                hsem.release()
        finally:
            self._global.release()

    def _execute_with_retries(
        self,
        session: requests.Session,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response:
        m = method.upper()
        last_exc: Exception | None = None

        for attempt in range(self.retry_max + 1):
            try:
                if m == "GET":
                    r = session.get(url, **kwargs)
                elif m == "POST":
                    r = session.post(url, **kwargs)
                else:
                    raise ValueError(f"Unsupported method {method!r}")

                if r.status_code == 429:
                    if attempt < self.retry_max:
                        delay = self._retry_after_seconds(r)
                        logger.warning(
                            "Jarvis HTTP 429 %s — retry %s/%s after %.1fs",
                            _host_key(url), attempt + 1, self.retry_max, delay,
                        )
                        time.sleep(delay)
                        continue
                    return r

                if r.status_code in (502, 503, 504):
                    if attempt < self.retry_max:
                        delay = self.retry_base_sec * (2**attempt) + random.random() * 0.2
                        logger.warning(
                            "Jarvis HTTP %s %s — retry %s/%s after %.2fs",
                            r.status_code, _host_key(url), attempt + 1, self.retry_max, delay,
                        )
                        time.sleep(delay)
                        continue
                    return r

                return r

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                last_exc = e
                if attempt < self.retry_max:
                    delay = self.retry_base_sec * (2**attempt) + random.random() * 0.2
                    logger.warning(
                        "Jarvis HTTP %s: %s — retry %s/%s after %.2fs",
                        type(e).__name__, _host_key(url), attempt + 1, self.retry_max, delay,
                    )
                    time.sleep(delay)
                    continue
                raise

        if last_exc:
            raise last_exc
        raise RuntimeError("JarvisFetchGate: retry loop exhausted without response")
