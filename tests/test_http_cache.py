import httpx
import pytest

from evidence_mcp.http_cache import (
    HttpFetcher,
    NotFound,
    RateLimited,
    RateLimiter,
    ResponseTooLarge,
    UpstreamError,
)

BASE = "https://sdmx.oecd.org/public/rest"


def make_fetcher(settings, handler):
    return HttpFetcher(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_second_identical_request_is_served_from_cache(settings):
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, content=b"hello")

    fetcher = make_fetcher(settings, handler)
    assert fetcher.get(f"{BASE}/x", ttl_seconds=60, max_bytes=100) == b"hello"
    assert fetcher.get(f"{BASE}/x", ttl_seconds=60, max_bytes=100) == b"hello"
    assert len(calls) == 1


def test_expired_cache_entry_is_refetched(settings):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, content=b"x")

    fetcher = make_fetcher(settings, handler)
    fetcher.get(f"{BASE}/x", ttl_seconds=60, max_bytes=100)
    fetcher.get(f"{BASE}/x", ttl_seconds=-1, max_bytes=100)
    assert len(calls) == 2


def test_rate_limiter_blocks_after_budget(tmp_path):
    now = [1000.0]
    limiter = RateLimiter(tmp_path / "rl.json", max_per_hour=2, clock=lambda: now[0])
    limiter.acquire()
    limiter.acquire()
    with pytest.raises(RateLimited):
        limiter.acquire()
    now[0] += 3601  # an hour later the window has moved on
    limiter.acquire()


def test_rate_limit_survives_restart(tmp_path):
    first = RateLimiter(tmp_path / "rl.json", max_per_hour=1)
    first.acquire()
    second = RateLimiter(tmp_path / "rl.json", max_per_hour=1)  # a new server process
    with pytest.raises(RateLimited):
        second.acquire()


def test_transient_503_is_retried(settings, monkeypatch):
    monkeypatch.setattr("evidence_mcp.http_cache.time.sleep", lambda s: None)
    responses = iter([httpx.Response(503), httpx.Response(200, content=b"ok")])
    fetcher = make_fetcher(settings, lambda request: next(responses))
    assert fetcher.get(f"{BASE}/y", ttl_seconds=60, max_bytes=100) == b"ok"


def test_404_raises_not_found(settings):
    fetcher = make_fetcher(settings, lambda request: httpx.Response(404))
    with pytest.raises(NotFound):
        fetcher.get(f"{BASE}/missing", ttl_seconds=60, max_bytes=100)


def test_oversized_response_is_refused(settings):
    fetcher = make_fetcher(settings, lambda request: httpx.Response(200, content=b"x" * 1000))
    with pytest.raises(ResponseTooLarge):
        fetcher.get(f"{BASE}/big", ttl_seconds=60, max_bytes=100)


def test_urls_outside_the_api_are_refused(settings):
    fetcher = make_fetcher(settings, lambda request: httpx.Response(200))
    with pytest.raises(UpstreamError):
        fetcher.get("https://attacker.example/steal", ttl_seconds=60, max_bytes=100)
    with pytest.raises(UpstreamError):  # prefix trick: same start, different host
        fetcher.get(f"{BASE}.attacker.example/x", ttl_seconds=60, max_bytes=100)
