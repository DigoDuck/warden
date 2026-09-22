"""GET /audit/verify: recompute the hash chain and report whether it still holds."""

from typing import Annotated

from fastapi import APIRouter, Depends

from warden import audit
from warden.api.deps import SessionDep, require_scope
from warden.api.schemas import AuditVerifyOut
from warden.identity import Claims

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/verify", response_model=AuditVerifyOut)
async def verify_audit_log(
    session: SessionDep,
    claims: Annotated[Claims, Depends(require_scope("audit:read"))],
) -> AuditVerifyOut:
    result = await audit.verify(session)
    return AuditVerifyOut(
        ok=result.ok,
        rows_checked=result.rows_checked,
        broken_row_id=result.broken_row_id,
        reason=result.reason,
    )
