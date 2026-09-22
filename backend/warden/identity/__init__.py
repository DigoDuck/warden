"""Agent and user identity: RS256 JWT issuance, verification and revocation.

See docs/adr/ADR-005-agent-identity.md for why RS256, why a stateful `jti` table despite JWTs
being meant to be stateless, and what this module does not do yet: nothing in `core/loop.py`
calls it, and `identity/broker.py` (secret brokering) is a separate, later PR.
"""

from warden.identity.jwt import (
    AGENT_TTL_CAP_SECONDS,
    USER_TTL_CAP_SECONDS,
    Claims,
    InsufficientScope,
    InvalidToken,
    KeyPair,
    issue_agent_token,
    issue_user_token,
    load_keys,
    revoke,
    revoke_all_for_task,
    verify,
)

__all__ = [
    "AGENT_TTL_CAP_SECONDS",
    "USER_TTL_CAP_SECONDS",
    "Claims",
    "InsufficientScope",
    "InvalidToken",
    "KeyPair",
    "issue_agent_token",
    "issue_user_token",
    "load_keys",
    "revoke",
    "revoke_all_for_task",
    "verify",
]
