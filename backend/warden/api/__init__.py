"""Warden's first HTTP surface (briefing §14).

Boundary (briefing §10): this package validates input, authenticates users and exposes
state. It never decides whether a tool call is allowed (that is `policy`) and never drives
the agent loop directly (that is `core`); it only calls `core.queue.enqueue` to hand a task
to the worker that already exists.
"""

from warden.api.app import create_app

__all__ = ["create_app"]
