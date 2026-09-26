"""Shared test fixtures. Nothing here touches the network: the SDMX API is replaced by an
httpx MockTransport that serves the files in tests/fixtures and counts requests."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from evidence_mcp.config import Settings
from evidence_mcp.http_cache import HttpFetcher
from evidence_mcp.retrieval import build_index, save_index
from evidence_mcp.server import create_server

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
SAMPLE_CORPUS = ROOT / "sample_corpus"


class FakeSdmxApi:
    """Serves fixture files for the three request types and records every URL requested."""

    def __init__(self):
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url.endswith("/dataflow/all"):
            return httpx.Response(200, content=(FIXTURES / "dataflows.xml").read_bytes())
        if "/dataflow/OECD.STI.STP/DSD_MSTI@DF_MSTI/1.3" in url:
            return httpx.Response(200, content=(FIXTURES / "structure_msti.xml").read_bytes())
        if "/data/OECD.STI.STP,DSD_MSTI@DF_MSTI,1.3/" in url:
            if "ITA" in url:  # a valid query with no observations
                return httpx.Response(404, text="NoRecordsFound")
            return httpx.Response(200, content=(FIXTURES / "data_msti.csv").read_bytes())
        return httpx.Response(404, text="NoResultsFound")


@pytest.fixture
def api() -> FakeSdmxApi:
    return FakeSdmxApi()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        cache_dir=tmp_path / "cache",
        index_path=tmp_path / "index.json",
        max_classification="internal",
        rate_limit_per_hour=50,
    )


@pytest.fixture
def fetcher(settings: Settings, api: FakeSdmxApi) -> HttpFetcher:
    return HttpFetcher(settings, client=httpx.Client(transport=httpx.MockTransport(api)))


@pytest.fixture
def sample_index(settings: Settings) -> Path:
    save_index(build_index(SAMPLE_CORPUS, settings.max_classification), settings.index_path)
    return settings.index_path


@pytest.fixture
def server(settings: Settings, fetcher: HttpFetcher, sample_index: Path):
    return create_server(settings, fetcher)
