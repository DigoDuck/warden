"""GET /approvals, POST /approvals/{id}/approve, POST /approvals/{id}/reject.

No agent logic here (briefing §10), same boundary routes_tasks.py draws: this module reads
`approvals` and calls `core.approvals.decide_approval`, and maps its exceptions to HTTP
status codes. It never decides anything itself, that stays in `core/approvals.py` and, for
which calls need a human at all, the policy engine.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from warden.api.approval_schemas import ApprovalDecisionIn, ApprovalOut
from warden.api.deps import SessionDep, require_scope
from warden.core import approvals
from warden.identity import Claims
from warden.models import Approval

router = APIRouter(prefix="/approvals", tags=["approvals"])


def _out(row: Approval) -> ApprovalOut:
    return ApprovalOut(
        id=row.id,
        task_id=row.task_id,
        tool=row.tool,
        args_safe=row.args_safe,
        matched_rules=row.matched_rules,
        reason=row.reason,
        status=row.status,
        requested_at=row.requested_at,
    )


@router.get("", response_model=list[ApprovalOut])
async def list_approvals(
    session: SessionDep,
    claims: Annotated[Claims, Depends(require_scope("approvals:read"))],
    status: Annotated[str, Query()] = "pending",
) -> list[ApprovalOut]:
    rows = await session.scalars(
        select(Approval).where(Approval.status == status).order_by(Approval.requested_at)
    )
    return [_out(row) for row in rows]


def _user_id_of(claims: Claims) -> uuid.UUID:
    # Same contract and same defensive shape as routes_tasks.py's own helper: subjects this
    # narrow always parse for a real user token, so a failure here means a bug upstream, not
    # a client mistake (401, not 500).
    try:
        return uuid.UUID(claims.sub.split(":", 1)[1])
    except (IndexError, ValueError) as exc:
        raise HTTPException(401, "token subject is not a user id") from exc


async def _decide(
    session: AsyncSession,
    approval_id: uuid.UUID,
    claims: Claims,
    body: ApprovalDecisionIn,
    *,
    approve: bool,
) -> ApprovalOut:
    user_id = _user_id_of(claims)
    try:
        row = await approvals.decide_approval(
            session, approval_id, approve=approve, user_id=user_id, note=body.note
        )
    except approvals.ApprovalNotFound as exc:
        raise HTTPException(404, "approval not found") from exc
    except approvals.ApprovalAlreadyDecided as exc:
        raise HTTPException(409, "approval already decided") from exc
    except ValueError as exc:
        # decide_approval's own guard: rejecting without a non-empty note.
        raise HTTPException(422, str(exc)) from exc
    await session.commit()
    return _out(row)


@router.post("/{approval_id}/approve", response_model=ApprovalOut)
async def approve(
    approval_id: uuid.UUID,
    body: ApprovalDecisionIn,
    session: SessionDep,
    claims: Annotated[Claims, Depends(require_scope("approvals:decide"))],
) -> ApprovalOut:
    return await _decide(session, approval_id, claims, body, approve=True)


@router.post("/{approval_id}/reject", response_model=ApprovalOut)
async def reject(
    approval_id: uuid.UUID,
    body: ApprovalDecisionIn,
    session: SessionDep,
    claims: Annotated[Claims, Depends(require_scope("approvals:decide"))],
) -> ApprovalOut:
    return await _decide(session, approval_id, claims, body, approve=False)
