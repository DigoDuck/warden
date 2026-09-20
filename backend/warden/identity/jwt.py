"""Agent and user identity: issue, verify and revoke short-lived RS256 JWTs.

See docs/adr/ADR-005-agent-identity.md for why RS256 (not HS256), why a stateful `jti` table
despite JWTs being meant to be stateless, and the algorithm-confusion attack `verify()` closes
by pinning `algorithms=["RS256"]` instead of trusting the token's own `alg` header.

Scope note (briefing §10): this module issues and validates tokens and brokers nothing else.
It does not decide *whether* a tool call is allowed (that is `policy`), and nothing in
`core/loop.py` calls it yet, this is a library with tests, not an enforced boundary.
"""

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from warden import audit
from warden.config import Settings, get_settings
from warden.models import IssuedToken, User

ISSUER = "warden"
AUDIENCE = "warden-tools"
# Pinned, never read from the token's own header: an attacker who controls `alg` controls
# which check runs (see the algorithm-confusion test in tests/test_identity.py). PyJWT still
# refuses "none" and any HS* algorithm here because they are simply not in this list, this is
# the entire mitigation, not a special case handled elsewhere.
ALGORITHM = "RS256"

# Briefing: "JWT 15 min" for an agent run. Enforced as a hard cap, not just a default, so a
# caller cannot accidentally (or maliciously) mint a long-lived agent credential.
AGENT_TTL_CAP_SECONDS = 900
# ponytail: the briefing does not state a user-token ceiling the way it does for agent tokens.
# One day is a placeholder good enough for a control plane with no session refresh yet; revisit
# once login sessions are designed.
USER_TTL_CAP_SECONDS = 86400

# backend/warden/identity/jwt.py -> parents[3] is the repository root. Computed from this
# file's own location, not the process cwd, because `make keys` and the API/worker run from
# different working directories and a cwd-relative default would point at two different files.
REPO_ROOT = Path(__file__).resolve().parents[3]


class InvalidToken(Exception):
    """The token is unusable: bad signature, malformed, expired, wrong audience/issuer,
    revoked, or signed with a `jti` this control plane never issued. Maps to HTTP 401.

    The message must never include the token itself, callers may log it.
    """


class InsufficientScope(Exception):
    """The token verified fine but does not carry the scope the caller requires. Maps to
    HTTP 403: this is an authorization gap, not an authentication failure.
    """


@dataclass(frozen=True)
class KeyPair:
    """A signing key plus the `kid` that names it.

    ponytail: a single key for now; `kid` exists so that key rotation becomes configuration
    (`verify()` accepting a mapping of kid to public key) instead of a refactor, once there is
    a second key to rotate to.
    """

    private_key: rsa.RSAPrivateKey
    public_key: rsa.RSAPublicKey
    kid: str


@dataclass(frozen=True)
class Claims:
    """What `verify()` hands back: never the raw JWT payload dict.

    `task_id` comes from the `issued_tokens` row, not from re-parsing `sub`: the row is the
    authoritative record of what a `jti` was issued for, and reparsing a string the token
    itself supplied would trust the token for something the database lookup already answers.
    """

    sub: str
    jti: uuid.UUID
    typ: str
    scopes: tuple[str, ...]
    task_id: uuid.UUID | None
    iat: datetime
    exp: datetime


def _resolve_key_path(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else REPO_ROOT / path


def _kid_for(public_key: rsa.RSAPublicKey) -> str:
    """A short, stable label for a public key, derived from the key itself so it never needs
    separate bookkeeping to stay in sync with which key is actually in use.
    """
    der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(der).hexdigest()[:16]


def load_keys(settings: Settings | None = None) -> KeyPair:
    """Read the private key from disk and derive the public key and `kid` from it.

    Tests do not call this: they build an ephemeral `KeyPair` with `cryptography` directly, so
    neither the test suite nor CI needs a key file on disk.
    """
    resolved_settings = settings or get_settings()
    path = _resolve_key_path(resolved_settings.jwt_private_key_path)
    if not path.is_file():
        raise FileNotFoundError(
            f"JWT private key not found at {path}. Run `make keys` to generate one."
        )

    private_key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise TypeError(f"key at {path} is not an RSA private key")

    public_key = private_key.public_key()
    return KeyPair(private_key=private_key, public_key=public_key, kid=_kid_for(public_key))


def _validate_scopes(scopes: Sequence[str]) -> tuple[str, ...]:
    cleaned = tuple(scopes)
    if not cleaned:
        raise ValueError("scopes must not be empty")
    for scope in cleaned:
        if not isinstance(scope, str) or not scope:
            raise ValueError("scopes must be non-empty strings")
    return cleaned


def _validate_ttl(ttl_seconds: int, *, cap: int) -> None:
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    if ttl_seconds > cap:
        raise ValueError(f"ttl_seconds exceeds the cap of {cap} seconds")


async def _issue(
    session: AsyncSession,
    keys: KeyPair,
    *,
    subject: str,
    typ: str,
    task_id: uuid.UUID | None,
    scopes: Sequence[str],
    ttl_seconds: int,
    ttl_cap: int,
) -> str:
    """Shared body of `issue_agent_token`/`issue_user_token`: validate, sign, record, audit.

    `audit.append()` holds a transaction-scoped advisory lock until the *caller's* transaction
    commits (see its docstring). This function only flushes, it never commits, so the lock's
    lifetime is entirely up to whoever calls `issue_agent_token`/`issue_user_token`: call it
    from a short transaction, the same rule `append()` itself documents, not from inside a
    long-running agent-run transaction.
    """
    _validate_ttl(ttl_seconds, cap=ttl_cap)
    validated_scopes = _validate_scopes(scopes)

    jti = uuid.uuid4()
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=ttl_seconds)

    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": subject,
        "jti": str(jti),
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "scopes": list(validated_scopes),
        "typ": typ,
    }
    token = jwt.encode(claims, keys.private_key, algorithm=ALGORITHM, headers={"kid": keys.kid})

    session.add(
        IssuedToken(
            jti=jti,
            subject=subject,
            task_id=task_id,
            scopes=list(validated_scopes),
            expires_at=expires_at,
        )
    )
    await session.flush()

    # NEVER the encoded token in details: audit_log is meant to be readable by people who
    # should not be able to reconstruct a live credential from it.
    await audit.append(
        session,
        actor_type="system",
        actor_id="identity",
        action="token.issued",
        target_type="issued_token",
        target_id=str(jti),
        details={
            "jti": str(jti),
            "task_id": str(task_id) if task_id else None,
            "subject": subject,
            "scopes": list(validated_scopes),
        },
    )
    return token


async def issue_agent_token(
    session: AsyncSession,
    keys: KeyPair,
    *,
    task_id: uuid.UUID,
    scopes: Sequence[str],
    ttl_seconds: int = AGENT_TTL_CAP_SECONDS,
) -> str:
    """Mint a token for one agent run of one task. Subject is `agent:task:<task_id>`."""
    return await _issue(
        session,
        keys,
        subject=f"agent:task:{task_id}",
        typ="agent",
        task_id=task_id,
        scopes=scopes,
        ttl_seconds=ttl_seconds,
        ttl_cap=AGENT_TTL_CAP_SECONDS,
    )


async def issue_user_token(
    session: AsyncSession,
    keys: KeyPair,
    user: User,
    *,
    scopes: Sequence[str],
    ttl_seconds: int = USER_TTL_CAP_SECONDS,
) -> str:
    """Mint a token for a logged-in user. Subject is `user:<user_id>`, no `task_id`."""
    return await _issue(
        session,
        keys,
        subject=f"user:{user.id}",
        typ="user",
        task_id=None,
        scopes=scopes,
        ttl_seconds=ttl_seconds,
        ttl_cap=USER_TTL_CAP_SECONDS,
    )


async def verify(
    session: AsyncSession,
    keys: KeyPair,
    token: str,
    *,
    required_scope: str | None = None,
) -> Claims:
    """Validate a token's signature and claims, then its standing in `issued_tokens`.

    Two failure modes, deliberately kept apart: a token that is not valid at all
    (`InvalidToken`, HTTP 401) versus a valid token that lacks a scope it is asked to prove
    (`InsufficientScope`, HTTP 403). The signature check runs first: there is no point asking
    what a forged token is authorized to do.
    """
    try:
        payload = jwt.decode(
            token,
            keys.public_key,
            # `algorithms` pinned to a single value, see the module-level ALGORITHM comment.
            algorithms=[ALGORITHM],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "jti", "sub", "aud", "iss"]},
        )
    except jwt.InvalidTokenError as exc:
        # Deliberately generic: naming which check failed (signature vs. expiry vs. audience)
        # tells an attacker probing the endpoint which part of a forged token to fix next.
        raise InvalidToken("token failed signature or claim validation") from exc

    try:
        jti = uuid.UUID(payload["jti"])
    except (ValueError, TypeError, AttributeError) as exc:
        raise InvalidToken("token jti is not a valid uuid") from exc

    row = await session.get(IssuedToken, jti)
    # A signature that checks out but a jti this control plane has no record of issuing means
    # either the signing key leaked, or a bug minted a token outside this module. Either way
    # trusting the signature alone would be wrong: fail closed, the same as a revoked token.
    if row is None:
        raise InvalidToken("token jti is unknown to this control plane")
    if row.revoked_at is not None:
        raise InvalidToken("token has been revoked")

    claims = Claims(
        sub=payload["sub"],
        jti=jti,
        typ=payload.get("typ", ""),
        scopes=tuple(payload.get("scopes", [])),
        task_id=row.task_id,
        iat=datetime.fromtimestamp(payload["iat"], tz=UTC),
        exp=datetime.fromtimestamp(payload["exp"], tz=UTC),
    )

    if required_scope is not None and required_scope not in claims.scopes:
        raise InsufficientScope(f"token lacks required scope {required_scope!r}")

    return claims


async def _mark_revoked(session: AsyncSession, row: IssuedToken) -> None:
    """Shared by `revoke()` and `revoke_all_for_task()`: set `revoked_at` once and audit it.

    Same short-transaction rule as `_issue()` applies to `audit.append()` here.
    """
    row.revoked_at = datetime.now(UTC)
    await audit.append(
        session,
        actor_type="system",
        actor_id="identity",
        action="token.revoked",
        target_type="issued_token",
        target_id=str(row.jti),
        details={
            "jti": str(row.jti),
            "task_id": str(row.task_id) if row.task_id else None,
            "subject": row.subject,
            "scopes": row.scopes,
        },
    )


async def revoke(session: AsyncSession, jti: uuid.UUID) -> bool:
    """Revoke one token. Returns False only when `jti` has no row at all.

    Idempotent: revoking an already-revoked token is a no-op that still returns True and keeps
    the first `revoked_at`, it does not append a second "token.revoked" row for a state change
    that did not happen.
    """
    row = await session.get(IssuedToken, jti)
    if row is None:
        return False
    if row.revoked_at is None:
        await _mark_revoked(session, row)
        await session.flush()
    return True


async def revoke_all_for_task(session: AsyncSession, task_id: uuid.UUID) -> int:
    """Revoke every still-live token issued for `task_id`. Used by incident containment
    (briefing §25: "revoga todos os jti da tarefa"). Returns how many rows changed.
    """
    rows = (
        await session.scalars(
            select(IssuedToken).where(
                IssuedToken.task_id == task_id, IssuedToken.revoked_at.is_(None)
            )
        )
    ).all()
    for row in rows:
        await _mark_revoked(session, row)
    await session.flush()
    return len(rows)
