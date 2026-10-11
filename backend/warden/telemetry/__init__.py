"""Tracing for the control plane (ADR-033). See `tracing.py`."""

from warden.telemetry.tracing import (
    ALLOWED_ATTRIBUTES,
    SpanHandle,
    configure,
    remote_parent,
    safe_attributes,
    setup_tracing,
    span,
)

__all__ = [
    "ALLOWED_ATTRIBUTES",
    "SpanHandle",
    "configure",
    "remote_parent",
    "safe_attributes",
    "setup_tracing",
    "span",
]
