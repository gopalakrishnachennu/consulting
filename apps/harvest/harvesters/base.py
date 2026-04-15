"""
BaseHarvester — Compliant HTTP foundation for all GoCareers harvesters.

Policies enforced on every request:
  1. Honest User-Agent — identifies as GoCareers-Bot, never spoofs a browser
  2. robots.txt respect — checks before any HTML scrape (cached per domain)
  3. Rate limiting     — min delay between requests, respects Retry-After
  4. Retry + backoff   — up to 3 attempts with exponential back-off (1s→2s→4s)
  5. Full audit log    — every HTTP call logged with method, URL, status, latency
  6. Timeout           — hard 15-second cap on every request
"""
import logging
import time
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# ─── Identity ─────────────────────────────────────────────────────────────────
# Honest bot user-agent. Never spoof a real browser.
BOT_USER_AGENT = "GoCareers-Bot/1.0 (+https://gocareers.io/bot; contact: admin@gocareers.io)"

# ─── Request policy ──────────────────────────────────────────────────────────
DEFAULT_TIMEOUT = 15        # seconds
MIN_DELAY_API = 1.0         # minimum seconds between API calls (per-company)
MIN_DELAY_SCRAPE = 4.0      # minimum seconds between HTML scrape calls
MAX_RETRIES = 3             # total attempts per request
BACKOFF_FACTOR = 2          # 1s → 2s → 4s
MAX_RETRY_AFTER = 120       # never wait more than 2 min for Retry-After

# ─── robots.txt cache ─────────────────────────────────────────────────────────
_robots_cache: dict[str, tuple[RobotFileParser, float]] = {}
ROBOTS_CACHE_TTL = 3600     # seconds (refresh per domain once per hour)


def _get_robots(base_url: str) -> RobotFileParser | None:
    """Fetch + cache robots.txt for a domain. Returns None on fetch failure."""
    parsed = urlparse(base_url)
    domain_key = f"{parsed.scheme}://{parsed.netloc}"
    now = time.monotonic()

    if domain_key in _robots_cache:
        rp, ts = _robots_cache[domain_key]
        if (now - ts) < ROBOTS_CACHE_TTL:
            return rp

    rp = RobotFileParser()
    robots_url = f"{domain_key}/robots.txt"
    try:
        rp.set_url(robots_url)
        rp.read()
        _robots_cache[domain_key] = (rp, now)
        return rp
    except Exception:
        # If robots.txt can't be fetched assume allowed, but log it
        logger.debug("robots.txt unreachable for %s — assuming allowed", domain_key)
        return None


def _check_robots_allowed(url: str) -> bool:
    """
    Return True if GoCareers-Bot is allowed to fetch `url` per robots.txt.
    For API endpoints (non-HTML) we skip the robots check and return True.
    """
    rp = _get_robots(url)
    if rp is None:
        return True  # can't read robots.txt → assume allowed
    return rp.can_fetch(BOT_USER_AGENT, url)


def _make_session() -> requests.Session:
    """Create a requests Session with retry-on-network-error, connection pooling."""
    session = requests.Session()
    # Only retry on network/connection errors, NOT on 4xx/5xx
    # (we handle those manually with backoff)
    retry = Retry(
        total=0,        # we do manual retry in _request_with_retry
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class BaseHarvester(ABC):
    """
    Abstract base for all GoCareers platform harvesters.

    Subclasses implement fetch_jobs() using self._get() / self._post().
    All policy (UA, robots, rate-limit, retry, logging) lives here.
    """

    platform_slug: str = ""
    is_scraper: bool = False        # True for HTML scrapers (stricter rules)

    def __init__(self):
        self._session = _make_session()
        self._session.headers.update({
            "User-Agent": BOT_USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self._last_request_at: float = 0.0

    # ── Public interface ──────────────────────────────────────────────────────

    @abstractmethod
    def fetch_jobs(
        self, company, tenant_id: str, since_hours: int = 24
    ) -> list[dict[str, Any]]:
        """Return list of raw job dicts for a company. Never raises — returns [] on error."""
        raise NotImplementedError

    # ── Internal HTTP helpers ─────────────────────────────────────────────────

    def _enforce_rate_limit(self):
        """Ensure minimum delay between consecutive requests."""
        delay = MIN_DELAY_SCRAPE if self.is_scraper else MIN_DELAY_API
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < delay:
            time.sleep(delay - elapsed)

    def _get(
        self,
        url: str,
        params: dict | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        check_robots: bool = False,
    ) -> dict | list:
        return self._request_with_retry("GET", url, params=params, timeout=timeout, check_robots=check_robots)

    def _post(
        self,
        url: str,
        json_data: dict,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> dict | list:
        return self._request_with_retry("POST", url, json_data=json_data, timeout=timeout)

    def _request_with_retry(
        self,
        method: str,
        url: str,
        params: dict | None = None,
        json_data: dict | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        check_robots: bool = False,
    ) -> dict | list:
        """
        Execute HTTP request with:
          - robots.txt gating (if check_robots=True or is_scraper=True)
          - minimum rate-limit delay
          - up to MAX_RETRIES attempts with exponential backoff
          - Retry-After header respect
          - full audit logging
        """
        # robots.txt gate
        if check_robots or self.is_scraper:
            if not _check_robots_allowed(url):
                logger.warning(
                    "[HARVEST] robots.txt BLOCKED %s %s — skipping", method, url
                )
                return {"error": "robots.txt disallowed"}

        # Rate-limit delay
        self._enforce_rate_limit()

        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            t0 = time.monotonic()
            try:
                kwargs: dict = {"timeout": timeout}
                if params:
                    kwargs["params"] = params
                if json_data is not None:
                    kwargs["json"] = json_data
                    self._session.headers["Content-Type"] = "application/json"

                resp = self._session.request(method, url, **kwargs)
                latency_ms = int((time.monotonic() - t0) * 1000)
                self._last_request_at = time.monotonic()

                logger.info(
                    "[HARVEST] %s %s → %s (%dms) attempt=%d/%d",
                    method, url, resp.status_code, latency_ms, attempt, MAX_RETRIES,
                )

                # Rate-limited by server
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", BACKOFF_FACTOR ** attempt))
                    retry_after = min(retry_after, MAX_RETRY_AFTER)
                    logger.warning(
                        "[HARVEST] 429 rate-limited — waiting %ds before retry", retry_after
                    )
                    time.sleep(retry_after)
                    continue

                # Server errors — retry with backoff
                if resp.status_code >= 500:
                    backoff = BACKOFF_FACTOR ** attempt
                    logger.warning(
                        "[HARVEST] %s server error — backoff %ds", resp.status_code, backoff
                    )
                    time.sleep(backoff)
                    last_error = f"HTTP {resp.status_code}"
                    continue

                # Client error (404, 403 etc) — don't retry
                if resp.status_code >= 400:
                    logger.warning(
                        "[HARVEST] %s client error for %s — not retrying", resp.status_code, url
                    )
                    return {"error": f"HTTP {resp.status_code}"}

                resp.raise_for_status()

                # Remove Content-Type so it doesn't bleed into GET requests
                self._session.headers.pop("Content-Type", None)
                return resp.json()

            except requests.exceptions.Timeout:
                backoff = BACKOFF_FACTOR ** attempt
                logger.warning(
                    "[HARVEST] Timeout on %s (attempt %d/%d) — backoff %ds",
                    url, attempt, MAX_RETRIES, backoff,
                )
                time.sleep(backoff)
                last_error = "Timeout"

            except requests.exceptions.ConnectionError as exc:
                backoff = BACKOFF_FACTOR ** attempt
                logger.warning(
                    "[HARVEST] ConnectionError %s (attempt %d/%d) — backoff %ds",
                    url, attempt, MAX_RETRIES, backoff,
                )
                time.sleep(backoff)
                last_error = str(exc)[:120]

            except Exception as exc:
                logger.error("[HARVEST] Unexpected error %s %s: %s", method, url, exc)
                return {"error": str(exc)[:200]}

        logger.error("[HARVEST] All %d attempts failed for %s: %s", MAX_RETRIES, url, last_error)
        return {"error": last_error or "max retries exceeded"}
