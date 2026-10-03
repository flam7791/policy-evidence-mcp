"""Tracing: the SDK's tool-call span carries the access decision, never the query or passages."""

import pytest
from mcp import Client
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from evidence_mcp import tracing

EXPORTER = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
trace.set_tracer_provider(_provider)


@pytest.fixture(autouse=True)
def clear():
    EXPORTER.clear()
    yield


async def test_search_span_has_the_decision_and_no_content(server):
    async with Client(server) as client:
        await client.call_tool("search_documents", {"query": "travel economy class zebra-phrase"})
    spans = {s.name: s for s in EXPORTER.get_finished_spans()}
    span = spans["tools/call search_documents"]
    assert span.attributes["gen_ai.tool.name"] == "search_documents"
    assert span.attributes["evidence.ceiling"] == "internal"
    assert span.attributes["evidence.hits"] >= 0
    values = [str(v) for s in EXPORTER.get_finished_spans() for v in s.attributes.values()]
    assert not any("zebra-phrase" in v for v in values)


async def test_list_documents_span(server):
    async with Client(server) as client:
        await client.call_tool("list_documents", {})
    span = {s.name: s for s in EXPORTER.get_finished_spans()}["tools/call list_documents"]
    assert span.attributes["evidence.documents"] > 0


def test_configure_is_off_without_an_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    assert tracing.configure() is False
