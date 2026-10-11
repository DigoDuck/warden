"""warden.telemetry: what may enter a span, and how a span behaves when something goes wrong.

The allow-list is the guarantee (ADR-033): a span attribute is either one of a short list of
reviewed keys with a redacted, bounded value, or it does not exist. These test the helper in
isolation; `test_tracing.py` proves it over a real task.
"""

from decimal import Decimal

import pytest
from opentelemetry.trace import StatusCode

from tests import fake_secrets
from tests.tracing_support import capture_spans
from warden import telemetry


@pytest.mark.parametrize(
    "key", ["file_content", "content", "args", "arguments", "output", "raw_content", "cmd", "x"]
)
def test_a_key_that_is_not_on_the_allow_list_is_refused(key: str) -> None:
    """Refused loudly, not dropped: a call site naming a new key is a decision somebody has to
    review (add it to ALLOWED_ATTRIBUTES), and silence would hide it until a trace was missing."""
    with pytest.raises(ValueError, match=key):
        telemetry.safe_attributes({key: "anything"})


def test_every_allowed_key_is_namespaced() -> None:
    assert telemetry.ALLOWED_ATTRIBUTES  # a skeleton with an empty list would pass the rest
    assert all(k.startswith("warden.") or k == "error.type" for k in telemetry.ALLOWED_ATTRIBUTES)


def test_values_keep_their_type_and_none_is_dropped() -> None:
    attrs = telemetry.safe_attributes(
        {
            "warden.iteration": 3,
            "warden.cost_usd": Decimal("0.0123"),
            "warden.resumed": True,
            "warden.model": "claude-opus-5-5",
            "warden.stop_reason": None,
        }
    )

    assert attrs == {
        "warden.iteration": 3,
        "warden.cost_usd": 0.0123,
        "warden.resumed": True,
        "warden.model": "claude-opus-5-5",
    }


def test_a_string_value_goes_through_the_redactor() -> None:
    secret = fake_secrets.github_pat()

    attrs = telemetry.safe_attributes({"warden.rules": f"never-read-secrets,{secret}"})

    assert secret not in str(attrs)
    assert attrs["warden.rules"] == "never-read-secrets,[redacted]"


def test_a_long_string_is_cut() -> None:
    attrs = telemetry.safe_attributes({"warden.rules": "r" * 5000})

    value = attrs["warden.rules"]
    assert isinstance(value, str)
    assert 0 < len(value) <= 300


def test_a_span_carries_only_its_safe_attributes() -> None:
    with capture_spans() as exporter:
        with telemetry.span("tool.execute", {"warden.tool": "read_file"}) as s:
            s.set({"warden.duration_ms": 12})

    (done,) = exporter.get_finished_spans()
    assert done.name == "tool.execute"
    assert dict(done.attributes or {}) == {"warden.tool": "read_file", "warden.duration_ms": 12}


def test_a_failing_span_records_the_exception_type_and_never_its_message() -> None:
    """The message of an exception is free text and routinely quotes arguments or file content
    (a ToolError says which path it refused). OpenTelemetry's default would copy it into an
    event; this must not."""
    secret = fake_secrets.github_pat()
    with capture_spans() as exporter:
        with pytest.raises(RuntimeError), telemetry.span("tool.execute"):
            raise RuntimeError(f"failed while reading {secret}")

    (done,) = exporter.get_finished_spans()
    assert done.status.status_code is StatusCode.ERROR
    assert dict(done.attributes or {})["error.type"] == "RuntimeError"
    assert done.events == ()
    assert secret not in str(done.status.description)


def test_an_exception_marked_expected_is_not_an_error() -> None:
    """Control flow (a task parking for approval) travels as an exception; it is not a failure."""

    class Parked(Exception):
        trace_expected = True

    with capture_spans() as exporter:
        with pytest.raises(Parked), telemetry.span("task.run"):
            raise Parked

    (done,) = exporter.get_finished_spans()
    assert done.status.status_code is StatusCode.UNSET


def test_ids_are_none_without_a_provider_and_hex_with_one() -> None:
    with telemetry.span("task.run") as s:
        assert s.ids() is None

    with capture_spans():
        with telemetry.span("task.run") as s:
            ids = s.ids()
    assert ids is not None
    trace_id, span_id = ids
    assert len(trace_id) == 32
    assert len(span_id) == 16
    int(trace_id, 16)
    int(span_id, 16)


def test_a_remote_parent_puts_a_new_span_in_the_same_trace() -> None:
    with capture_spans() as exporter:
        with telemetry.span("task.run") as first:
            ids = first.ids()
        assert ids is not None
        parent = telemetry.remote_parent(*ids)
        with telemetry.span("task.run", context=parent):
            pass

    root, resumed = exporter.get_finished_spans()
    assert resumed.context is not None and root.context is not None
    assert resumed.context.trace_id == root.context.trace_id
    assert resumed.parent is not None
    assert resumed.parent.span_id == root.context.span_id


def test_remote_parent_without_ids_is_none() -> None:
    assert telemetry.remote_parent(None, None) is None
    assert telemetry.remote_parent("not-hex", "zz") is None


def test_no_endpoint_means_no_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)

    assert telemetry.setup_tracing() is None


def test_an_endpoint_builds_a_provider_named_after_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "warden-test-worker")

    provider = telemetry.setup_tracing()
    try:
        assert provider is not None
        assert provider.resource.attributes["service.name"] == "warden-test-worker"
    finally:
        telemetry.configure(None)
        if provider is not None:
            provider.shutdown()
