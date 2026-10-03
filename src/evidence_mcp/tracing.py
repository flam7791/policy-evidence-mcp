"""OpenTelemetry tracing: off unless configured, never carrying content.

The code depends only on the OpenTelemetry API, which does nothing until a tracer provider is
installed. `configure()` installs one when `OTEL_EXPORTER_OTLP_ENDPOINT` (or
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`) is set and the `tracing` extra is installed, exporting
spans over OTLP/HTTP to any collector: Jaeger, Grafana Tempo, Azure Monitor through the
OpenTelemetry Collector, and others.

Spans carry identifiers, model names, token counts, costs, latencies and decisions, following
the OpenTelemetry semantic conventions for generative AI (`gen_ai.*`) where they apply. They
never carry prompts, answers, document text or personal data.
"""

from __future__ import annotations

import logging
import os

from opentelemetry import trace

log = logging.getLogger(__name__)
tracer = trace.get_tracer("policy-evidence-mcp")


def configure(service_name: str = "policy-evidence-mcp") -> bool:
    """Install an OTLP exporter if an endpoint is configured. Returns True when tracing is on."""
    if not (
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    ):
        return False
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning("OTEL_EXPORTER_OTLP_ENDPOINT is set but the 'tracing' extra is not installed")
        return False
    name = os.environ.get("OTEL_SERVICE_NAME", service_name)
    provider = TracerProvider(resource=Resource.create({"service.name": name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    log.info("tracing on: service %s", name)
    return True
