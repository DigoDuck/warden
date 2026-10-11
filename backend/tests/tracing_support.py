"""An in-memory span exporter wired into `warden.telemetry`, for tests that look at spans."""

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from warden import telemetry


@contextmanager
def capture_spans() -> Iterator[InMemorySpanExporter]:
    """Route every span to memory for the duration of the block.

    `warden.telemetry.configure` instead of OpenTelemetry's global `set_tracer_provider`: the
    global one can be set once per process and then refuses, which would make the second test
    in a run silently trace into the first one's provider.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry.configure(provider)
    try:
        yield exporter
    finally:
        telemetry.configure(None)
        provider.shutdown()


def names(spans: list[ReadableSpan]) -> list[str]:
    return [s.name for s in spans]
