"""Spans for the control plane (ADR-033).

Two rules shape this module, and both exist because a trace backend is somewhere data goes to
be read by people who never had access to the task:

1. **A span attribute is on an allow-list or it does not exist.** `safe_attributes` is the only
   way a value gets onto a span (`span()` and `SpanHandle.set` both go through it). A key that is
   not in `ALLOWED_ATTRIBUTES` raises, so adding what a span carries is a reviewed one-line
   change here, never a side effect of passing a dict somewhere. There is deliberately no key for
   tool arguments, tool output, file content, model output or an argv: `args_hash` identifies a
   call without carrying it.
2. **Every string value is redacted, then bounded.** Defence in depth for the keys that are
   allowed (a rule id or a model name will not contain a token, but "will not" is what the
   redactor is for).

Telemetry never takes a task down. With no `OTEL_EXPORTER_OTLP_ENDPOINT` nothing is exported and
every call below is a no-op against OpenTelemetry's non-recording tracer. With one, spans leave
through a `BatchSpanProcessor`: `on_end` only enqueues, a background thread exports, and a
collector that is down costs a log line in that thread, not a delay or an exception in the loop.
"""

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from decimal import Decimal

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import NonRecordingSpan, Span, SpanContext, Status, StatusCode, TraceFlags

from warden.identity import broker

_TRACER_NAME = "warden"

# Long enough for a rule id list or a model name, short enough that a free-text value which
# slipped through cannot carry a file. The cut happens AFTER redaction: truncating first could
# split a token in half and hide it from the pattern.
MAX_STRING_CHARS = 200

# Every attribute any span may carry. Keep it short and boring; see rule 1 in the module docstring.
ALLOWED_ATTRIBUTES: frozenset[str] = frozenset(
    {
        # identity of the unit of work
        "warden.task_id",
        "warden.resumed",
        "warden.iteration",
        "warden.purpose",
        # model.generate / planner.plan / review (never the prompt, never raw_content)
        "warden.provider",
        "warden.model",
        "warden.stop_reason",
        "warden.tokens_in",
        "warden.tokens_out",
        "warden.cost_usd",
        "warden.latency_ms",
        # policy.evaluate
        "warden.tool",
        "warden.effect",
        "warden.rules",
        "warden.duration_us",
        # tool.execute (a hash identifies the call without carrying its arguments)
        "warden.args_hash",
        "warden.duration_ms",
        # sandbox.exec (never the argv, never the output)
        "warden.container_id",
        "warden.image",
        # verify.run / review / publish / task.run outcome
        "warden.kind",
        "warden.status",
        "warden.passed",
        "warden.iterations",
        # set by `span()` itself when the block raises: the exception's class, never its message
        "error.type",
    }
)

_AttributeValue = str | int | float | bool


def safe_attributes(attributes: Mapping[str, object]) -> dict[str, _AttributeValue]:
    """Validate and sanitise attributes for a span. See the module docstring, rule 1 and 2.

    `None` is dropped (OpenTelemetry has no null attribute and "not known yet" is not worth a
    key). A `Decimal` becomes a float: spans carry numbers for charts, the exact figure lives in
    the database. Anything that is not a scalar is a call-site bug and raises, because the usual
    way a list or dict reaches a span is somebody passing arguments through.
    """
    safe: dict[str, _AttributeValue] = {}
    for key, value in attributes.items():
        if key not in ALLOWED_ATTRIBUTES:
            raise ValueError(
                f"{key!r} is not an allowed span attribute; add it to ALLOWED_ATTRIBUTES "
                "(warden/telemetry/tracing.py) if it can never carry arguments, output or "
                "file content"
            )
        if value is None:
            continue
        if isinstance(value, bool | int | float):
            safe[key] = value
        elif isinstance(value, Decimal):
            safe[key] = float(value)
        elif isinstance(value, str):
            safe[key] = broker.redact(value)[:MAX_STRING_CHARS]
        else:
            raise TypeError(f"span attribute {key!r} must be a scalar, not {type(value).__name__}")
    return safe


# The provider spans go to. Module state rather than OpenTelemetry's global
# `set_tracer_provider`, which can be set once per process and then silently ignores the next
# call: a test suite (and a worker restarting its tracing) needs to swap it.
_provider: TracerProvider | None = None


def configure(provider: TracerProvider | None) -> None:
    """Route spans to `provider`, or to nowhere (`None`)."""
    global _provider
    _provider = provider


def _tracer() -> trace.Tracer:
    return (_provider or trace.NoOpTracerProvider()).get_tracer(_TRACER_NAME)


def setup_tracing(service_name: str = "warden") -> TracerProvider | None:
    """Export spans over OTLP/HTTP when the standard environment asks for it.

    Reads the usual `OTEL_*` variables (the exporter itself reads the endpoint, headers and
    timeout; `OTEL_SERVICE_NAME` wins over `service_name`). No endpoint, or
    `OTEL_SDK_DISABLED=true`, means no tracing: returns None and every span stays a no-op.
    The caller owns the returned provider and should `shutdown()` it on exit to flush.
    """
    if os.environ.get("OTEL_SDK_DISABLED", "").lower() == "true":
        return None
    if not (
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    ):
        return None
    # `Resource.create` already reads OTEL_SERVICE_NAME; a name passed in here would override it.
    named = {} if os.environ.get("OTEL_SERVICE_NAME") else {"service.name": service_name}
    provider = TracerProvider(resource=Resource.create(named))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    configure(provider)
    return provider


class SpanHandle:
    """What a `span()` block gets: add attributes later, and read the ids to persist."""

    def __init__(self, otel_span: Span) -> None:
        self._span = otel_span

    def set(self, attributes: Mapping[str, object]) -> None:
        self._span.set_attributes(safe_attributes(attributes))

    def ids(self) -> tuple[str, str] | None:
        """(trace id, span id) as lowercase hex, or None when nothing is being recorded."""
        context = self._span.get_span_context()
        if not context.is_valid:
            return None
        return format(context.trace_id, "032x"), format(context.span_id, "016x")


def remote_parent(trace_id: str | None, span_id: str | None) -> Context | None:
    """A context whose current span is the one with these ids, for a span to hang under.

    This is how a task claimed a second time (after an approval, after a crash) continues the
    trace the first claim started: the first claim's root span id is stored on the task, and the
    new `task.run` is created as its child although the span object is long gone. "Remote"
    because, as far as the SDK is concerned, the parent lives in another process.
    """
    if not trace_id or not span_id:
        return None
    try:
        context = SpanContext(
            trace_id=int(trace_id, 16),
            span_id=int(span_id, 16),
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
    except ValueError:
        return None
    if not context.is_valid:
        return None
    return trace.set_span_in_context(NonRecordingSpan(context))


@contextmanager
def span(
    name: str, attributes: Mapping[str, object] | None = None, *, context: Context | None = None
) -> Iterator[SpanHandle]:
    """A span that is current for the block. `context` sets its parent explicitly.

    On an exception the span is marked ERROR with `error.type` only. OpenTelemetry's own default
    would also copy `str(exception)` into an event, and that text is free-form: a `ToolError`
    names the path it refused, a driver error quotes a statement. An exception that sets
    `trace_expected = True` (the loop's `_RunStopped`, which is how a task parks for approval)
    is control flow and leaves the status alone, as does anything that is not an `Exception`
    (cancellation).
    """
    with _tracer().start_as_current_span(
        name,
        context=context,
        attributes=safe_attributes(attributes or {}),
        record_exception=False,
        set_status_on_exception=False,
    ) as current:
        try:
            yield SpanHandle(current)
        except Exception as exc:
            if not getattr(exc, "trace_expected", False):
                current.set_attribute("error.type", type(exc).__name__)
                current.set_status(Status(StatusCode.ERROR))
            raise
