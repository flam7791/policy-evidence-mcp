"""HTTP access to the upstream API: disk cache, rate limit, retries and size cap.

Three concerns live here so that the rest of the code never talks to the network directly:

1. Cache. Statistical releases change slowly, so identical requests are answered from disk
   for a configurable time. This makes the server fast and keeps load on the provider low.
2. Rate limit. The OECD API allows about 60 requests per hour per client. The limiter counts
   real network calls in a sliding one-hour window (persisted to disk, so restarting the
   server does not reset it) and refuses politely before the provider would block us.
3. Safety. Only the configured base URL is ever requested (no arbitrary URLs from the model),
   responses are streamed with a hard byte cap, and transient errors are retried briefly.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Settings

log = logging.getLogger(__name__)

USER_AGENT = "policy-evidence-mcp/0.1"
RETRY_STATUSES = {429, 502, 503, 504}


class UpstreamError(RuntimeError):
    """The upstream API failed or refused the request. The message is shown to the model."""


class NotFound(UpstreamError):
    """The upstream API returned 404 (unknown dataflow, or no data for the query)."""


class RateLimited(UpstreamError):
    """The local hourly budget is used up; the caller should wait or narrow the request."""


class ResponseTooLarge(UpstreamError):
    """The response exceeded the byte cap; the caller should narrow the query."""


@dataclass(frozen=True)
class Ttl:
    """Cache lifetimes in seconds for each kind of request."""

    catalogue: int = 24 * 3600  # list of all dataflows
    structure: int = 7 * 24 * 3600  # dimensions and code lists
    data: int = 3600  # observations


DEFAULT_TTL = Ttl()


class DiskCache:
    """A tiny URL -> bytes cache. One file per entry, named by a hash of the URL."""

    def __init__(self, root: Path):
        self.root = root

    def _paths(self, url: str) -> tuple[Path, Path]:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.bin", self.root / f"{digest}.json"

    def get(self, url: str, ttl_seconds: int) -> bytes | None:
        body_path, meta_path = self._paths(url)
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if time.time() - meta["fetched_at"] > ttl_seconds:
                return None
            return body_path.read_bytes()
        except (FileNotFoundError, KeyError, ValueError):
            return None

    def put(self, url: str, body: bytes) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        body_path, meta_path = self._paths(url)
        body_path.write_bytes(body)
        meta_path.write_text(json.dumps({"url": url, "fetched_at": time.time()}), encoding="utf-8")


class RateLimiter:
    """Sliding one-hour window of request timestamps, persisted to a small JSON file."""

    WINDOW_SECONDS = 3600

    def __init__(self, state_file: Path, max_per_hour: int, clock=time.time):
        self.state_file = state_file
        self.max_per_hour = max_per_hour
        self.clock = clock
        self._lock = threading.Lock()

    def _load(self) -> list[float]:
        try:
            return [float(t) for t in json.loads(self.state_file.read_text(encoding="utf-8"))]
        except (FileNotFoundError, ValueError, TypeError):
            return []

    def acquire(self) -> None:
        """Record one request, or raise RateLimited if the hourly budget is used up."""
        with self._lock:
            now = self.clock()
            recent = [t for t in self._load() if now - t < self.WINDOW_SECONDS]
            if len(recent) >= self.max_per_hour:
                wait_minutes = int((self.WINDOW_SECONDS - (now - min(recent))) / 60) + 1
                raise RateLimited(
                    f"Hourly request budget ({self.max_per_hour}) reached. Try again in about "
                    f"{wait_minutes} minutes, or reuse results already retrieved."
                )
            recent.append(now)
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(recent), encoding="utf-8")


class HttpFetcher:
    """Fetches URLs under the configured base URL, through the cache and the rate limiter."""

    def __init__(
        self, settings: Settings, client: httpx.Client | None = None, ttl: Ttl = DEFAULT_TTL
    ):
        self.settings = settings
        self.ttl = ttl
        self.cache = DiskCache(settings.cache_dir / "http")
        self.limiter = RateLimiter(
            settings.cache_dir / "rate_limit.json", settings.rate_limit_per_hour
        )
        self.client = client or httpx.Client(
            timeout=settings.http_timeout_seconds,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=False,
        )

    def get(self, url: str, ttl_seconds: int, max_bytes: int, accept: str | None = None) -> bytes:
        if not url.startswith(self.settings.sdmx_base_url + "/"):
            # Defence in depth: tools only build URLs from the base URL, but never trust that.
            raise UpstreamError("Refusing to fetch a URL outside the configured API.")

        cached = self.cache.get(url, ttl_seconds)
        if cached is not None:
            log.debug("cache hit %s", url)
            return cached

        body = self._fetch_with_retries(url, max_bytes, accept)
        self.cache.put(url, body)
        return body

    def _fetch_with_retries(self, url: str, max_bytes: int, accept: str | None) -> bytes:
        headers = {"Accept": accept} if accept else {}
        attempts = 3
        for attempt in range(1, attempts + 1):
            self.limiter.acquire()  # every real network call counts against the budget
            log.info("GET %s (attempt %d)", url, attempt)
            try:
                with self.client.stream("GET", url, headers=headers) as response:
                    if response.status_code == 404:
                        raise NotFound("The API returned 404: no such dataflow, or no data.")
                    if response.status_code in RETRY_STATUSES and attempt < attempts:
                        delay = _retry_delay(response, attempt)
                        log.warning("HTTP %s, retrying in %.0fs", response.status_code, delay)
                        time.sleep(delay)
                        continue
                    if response.status_code >= 400:
                        raise UpstreamError(f"The API returned HTTP {response.status_code}.")
                    return _read_capped(response, max_bytes)
            except httpx.TimeoutException as exc:
                if attempt == attempts:
                    raise UpstreamError("The API did not respond in time.") from exc
            except httpx.TransportError as exc:
                if attempt == attempts:
                    raise UpstreamError(f"Network error reaching the API: {exc}") from exc
        raise UpstreamError("The API kept failing after retries.")


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After", "")
    if retry_after.isdigit():
        return min(float(retry_after), 10.0)
    return float(2 ** (attempt - 1))  # 1s, then 2s


def _read_capped(response: httpx.Response, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > max_bytes:
            raise ResponseTooLarge(
                f"The response is larger than {max_bytes // 1_000_000} MB. Narrow the query: "
                "fewer countries or indicators in the key, or a shorter period."
            )
        chunks.append(chunk)
    return b"".join(chunks)
