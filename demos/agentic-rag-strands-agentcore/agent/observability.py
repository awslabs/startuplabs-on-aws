"""OpenTelemetry observability setup for the RAG agent."""

import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.sdk.resources import Resource


def setup_observability(
    service_name: str = None,
    export_to_console: bool = True,
    export_to_otlp: bool = False,
) -> TracerProvider:
    """Configure OpenTelemetry tracing for the RAG agent.

    Strands Agents SDK emits OTEL spans automatically for:
    - Agent invocations (root span)
    - Model inference calls (with token usage, latency)
    - Tool executions (with parameters and results)
    - Event loop cycles

    This function sets up the exporter to capture those spans.

    Args:
        service_name: Service name for OTEL resource. Defaults to env var or 'agentic-rag-demo'.
        export_to_console: Whether to print spans to console (useful for local dev).
        export_to_otlp: Whether to export spans via OTLP (for production observability).

    Returns:
        Configured TracerProvider instance.
    """
    service_name = service_name or os.getenv("OTEL_SERVICE_NAME", "agentic-rag-demo")

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": "0.1.0",
            "deployment.environment": os.getenv("ENVIRONMENT", "development"),
        }
    )

    provider = TracerProvider(resource=resource)

    if export_to_console:
        console_processor = SimpleSpanProcessor(ConsoleSpanExporter())
        provider.add_span_processor(console_processor)

    if export_to_otlp:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )

            otlp_endpoint = os.getenv(
                "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"
            )
            otlp_exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
            otlp_processor = BatchSpanProcessor(otlp_exporter)
            provider.add_span_processor(otlp_processor)
        except ImportError:
            print(
                "Warning: OTLP exporter not available. "
                "Install opentelemetry-exporter-otlp for production tracing."
            )

    trace.set_tracer_provider(provider)
    return provider


def get_tracer(name: str = "agentic-rag") -> trace.Tracer:
    """Get a tracer instance for custom instrumentation.

    Args:
        name: Tracer name for span attribution.

    Returns:
        OpenTelemetry Tracer instance.
    """
    return trace.get_tracer(name)
