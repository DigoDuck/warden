"""The one seam between a policy-approved tool call and the tool actually running (ADR-025).

Every call the loop dispatches after an ALLOW decision, including one allowed by a human's
approval, already got a fresh, task-bound agent token minted for it alone
(`core/loop.py`, checkpoint (c)). This module is where that token is spent: it re-verifies
the token is still live (`identity.verify`, a fresh read of `issued_tokens`, not the `Claims`
the loop already holds in memory), that it was minted for the task actually running it, and,
when the tool declares one, that it carries the scope the tool requires. Only then does the
call reach the registry.

A tool never runs on the strength of the policy decision alone; it runs on the strength of a
token the loop can revoke mid-run (`identity.revoke_all_for_task`). Kept out of
`core/loop.py` itself so "is this token good enough to run this tool" has one answer, and so
the loop's own tests do not have to fake a JWT to drive the happy path: `core/loop.py` mints
and immediately spends the token in the same call, and the failure modes below exist for
`tests/test_gateway.py` to drive directly, the same way nothing in `core/loop.py` ever hands
the broker a hand-built `Claims` either (`identity/broker.py`'s own docstring).
"""

from typing import Any
from uuid import UUID

import jwt as pyjwt
from sqlalchemy.ext.asyncio import AsyncSession

from warden import audit
from warden.identity.jwt import InvalidToken, KeyPair, verify
from warden.tools.registry import ToolContext, ToolError, ToolRegistry


class GatewayError(ToolError):
    """The gateway refused the call before any tool ran. The message always leads with the
    HTTP status a caller would map this to (401 or 403), built only from `identity`'s own
    exception text and this module's fixed reason strings, never from the token itself."""


def _best_effort_jti(token: str) -> str | None:
    """Read the `jti` claim without verifying anything, purely so a 401 audit row can name
    which credential was rejected. Never trust this for authorization: a token that fails
    `identity.verify` could be forged, and an attacker controls every byte of it, `jti`
    included. `identity.jwt.ALGORITHM` is pinned everywhere a token is actually trusted; this
    read explicitly turns verification off, which is why it lives only here, for logging.
    """
    try:
        payload = pyjwt.decode(
            token, options={"verify_signature": False, "verify_exp": False, "verify_aud": False}
        )
    except pyjwt.InvalidTokenError:
        return None
    jti = payload.get("jti")
    return jti if isinstance(jti, str) else None


async def _refuse(
    session: AsyncSession,
    *,
    task_id: UUID,
    tool: str,
    action: str,
    jti: str | None,
    reason: str,
    status: int,
) -> GatewayError:
    """Audit the refusal, then hand back the error the caller should raise.

    A plain helper, not a `raise` of its own: raising from inside a `try` this deep would
    read like the audit write itself failed. `session.flush()` inside `audit.append` is
    enough; the call site's own checkpoint is what commits it, same as every other audit row
    a refused call produces (`core/loop.py`'s own `policy.deny`).
    """
    await audit.append(
        session,
        actor_type="system",
        actor_id="gateway",
        action=action,
        target_type="task",
        target_id=str(task_id),
        details={"tool": tool, "jti": jti, "reason": reason},
    )
    return GatewayError(f"{status} {reason}")


async def execute(
    session: AsyncSession,
    keys: KeyPair,
    registry: ToolRegistry,
    name: str,
    arguments: dict[str, Any],
    *,
    token: str,
    task_id: UUID,
) -> str:
    """Verify `token`, then run `name` through `registry`.

    Raises `GatewayError`, a `ToolError`, instead of executing when the token does not hold
    up: the loop's existing `except ToolError` -> `is_error` tool_result handling
    (`core/loop.py::_run_tools`) covers this with no new branch, the same as any other tool
    refusal.
    """
    try:
        claims = await verify(session, keys, token)
    except InvalidToken as exc:
        raise await _refuse(
            session,
            task_id=task_id,
            tool=name,
            action="tool.auth_failed",
            jti=_best_effort_jti(token),
            reason=f"unauthorized: {exc}",
            status=401,
        ) from exc

    if claims.task_id != task_id:
        # `verify()` only proves the token is live and well-formed; it says nothing about
        # which task it was minted for. Tokens are per-call and per-task (core/loop.py), so
        # a claim bound to a different task is exactly as wrong as one nobody issued: the
        # running task must never be able to spend a credential minted for another one.
        raise await _refuse(
            session,
            task_id=task_id,
            tool=name,
            action="tool.auth_failed",
            jti=str(claims.jti),
            reason="unauthorized: token is not bound to this task",
            status=401,
        )

    required = registry.required_scope(name)
    if required is not None and required not in claims.scopes:
        raise await _refuse(
            session,
            task_id=task_id,
            tool=name,
            action="tool.forbidden",
            jti=str(claims.jti),
            reason=f"forbidden: token lacks required scope {required!r}",
            status=403,
        )

    context = ToolContext(claims=claims, session=session)
    return await registry.execute(name, arguments, context=context)
