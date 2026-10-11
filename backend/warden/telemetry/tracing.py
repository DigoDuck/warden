"""Spans for the control plane. Skeleton: the signatures exist, nothing is recorded yet."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from opentelemetry.context import Context
from opentelemetry.sdk.trace import TracerProvider

ALLOWED_ATTRIBUTES: frozenset[str] = frozenset()


def safe_attributes(attributes: Mapping[str, object]) -> dict[str, str | int | float | bool]:
    return {}


def configure(provider: TracerProvider | None) -> None:
    return None


def setup_tracing(service_name: str = "warden") -> TracerProvider | None:
    return None


class SpanHandle:
    def set(self, attributes: Mapping[str, object]) -> None:
        return None

    def ids(self) -> tuple[str, str] | None:
        return None


def remote_parent(trace_id: str | None, span_id: str | None) -> Context | None:
    return None


@contextmanager
def span(
    name: str, attributes: Mapping[str, object] | None = None, *, context: Context | None = None
) -> Iterator[SpanHandle]:
    yield SpanHandle()
