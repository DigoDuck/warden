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
from sqlalchemy import ColumnElement, select, update
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

    Everything that authorises comes from the `issued_tokens` row, not from re-parsing the
    payload: the row is the authoritative record of what a `jti` was issued for. `verify()`
    also refuses a token whose payload disagrees with its row, so by the time a `Claims`
    exists the two are known to say the same thing. `iat` is the one field read from the
    token alone: it grants nothing, and the row has no separate copy to compare it with.
    """

    sub: str
    jti: uuid.UUID
    typ: str
    scopes: tuple[str, ...]
    task_id: uuid.UUID | None
    iat: datetime
    exp: datetime


def _typ_of(subject: str) -> str:
    """`agent` or `user`, read off the subject this module itself wrote (`agent:task:<id>`,
    `user:<id>`). `issued_tokens` has no `typ` column, and the subject already says it, so
    this is how the row answers for `typ` without trusting the token's own claim.
    """
    return subject.split(":", 1)[0]


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
    # str satisfies Sequence[str] (mypy accepts it, and tuple() below would iterate it one
    # character at a time), so it needs an explicit check: a caller that forgets the list
    # brackets is the likeliest real mistake, and each resulting one-char "scope" would end up
    # written verbatim into `issued_tokens` and the append-only audit log.
    if isinstance(scopes, str):
        raise ValueError("scopes must be a sequence of strings, not a single string")
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

    # populate_existing=True, same reasoning as audit/log.py::verify(): without it, a session
    # that already has this row warm in its identity map (it issued the token, or verified it
    # earlier in this same session) gets back that cached Python object instead of what the
    # SELECT just fetched, so a revoke() committed on another connection would never be seen
    # here, defeating the entire point of a revocable, stateful `jti` (ADR-005).
    row = await session.get(IssuedToken, jti, populate_existing=True)
    # A signature that checks out but a jti this control plane has no record of issuing means
    # either the signing key leaked, or a bug minted a token outside this module. Either way
    # trusting the signature alone would be wrong: fail closed, the same as a revoked token.
    if row is None:
        raise InvalidToken("token jti is unknown to this control plane")
    if row.revoked_at is not None:
        raise InvalidToken("token has been revoked")

    # The token has to say exactly what was issued for this jti. A valid signature over a
    # live jti with anything else in it (wider scopes, another subject, a later expiry, a
    # different typ) was not minted by `_issue()`, which means the signing key is in someone
    # else's hands. Answering from the row and carrying on would quietly turn that forgery
    # into a working token with the original permissions; refusing it keeps an incident
    # looking like one. Comparing `exp` also closes what the signature check cannot: PyJWT
    # only knows the expiry the token claims, and a re-signed token claims whatever it likes.
    issued = {
        "sub": row.subject,
        "typ": _typ_of(row.subject),
        "scopes": list(row.scopes),
        "exp": int(row.expires_at.timestamp()),
    }
    if {name: payload.get(name) for name in issued} != issued:
        raise InvalidToken("token does not match what was issued for this jti")

    try:
        claims = Claims(
            sub=row.subject,
            jti=jti,
            typ=_typ_of(row.subject),
            scopes=tuple(row.scopes),
            task_id=row.task_id,
            iat=datetime.fromtimestamp(payload["iat"], tz=UTC),
            exp=row.expires_at,
        )
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        # PyJWT validates iat by calling int() on it, so a numeric *string* passes decode()
        # and only breaks here, in fromtimestamp(). Uncaught, a TypeError would escape
        # verify()'s documented InvalidToken/InsufficientScope contract as a 500.
        raise InvalidToken("token claims are malformed") from exc

    if required_scope is not None and required_scope not in claims.scopes:
        raise InsufficientScope(f"token lacks required scope {required_scope!r}")

    return claims


async def _revoke_where(session: AsyncSession, *conditions: ColumnElement[bool]) -> int:
    """Revoke every live token matching `conditions`, and audit each one that changed.

    One guarded UPDATE, not read-then-write: `revoked_at IS NULL` in the WHERE clause lets
    the database decide who made the state change. Reading the row first and checking
    `revoked_at` in Python gets it wrong twice over. A session that already holds the row
    sees its own cached copy (the identity-map trap `verify()` documents), and two sessions
    revoking at once both see NULL; either way the same revocation is audited twice, in a
    log that cannot be corrected afterwards, and the first `revoked_at` is overwritten.

    Same short-transaction rule as `_issue()` applies to `audit.append()` here.
    """
    changed = (
        await session.scalars(
            update(IssuedToken)
            .where(IssuedToken.revoked_at.is_(None), *conditions)
            .values(revoked_at=datetime.now(UTC))
            .returning(IssuedToken),
            # RETURNING hands back rows this session may already hold; without this it would
            # return the cached objects, still saying `revoked_at` is None.
            execution_options={"populate_existing": True},
        )
    ).all()
    for row in changed:
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
    return len(changed)


async def revoke(session: AsyncSession, jti: uuid.UUID) -> bool:
    """Revoke one token. Returns False only when `jti` has no row at all.

    Idempotent: revoking an already-revoked token is a no-op that still returns True and keeps
    the first `revoked_at`, it does not append a second "token.revoked" row for a state change
    that did not happen.
    """
    if await _revoke_where(session, IssuedToken.jti == jti):
        return True
    known = await session.scalar(select(IssuedToken.jti).where(IssuedToken.jti == jti))
    return known is not None


async def revoke_all_for_task(session: AsyncSession, task_id: uuid.UUID) -> int:
    """Revoke every still-live token issued for `task_id`. Used by incident containment
    (briefing §25: "revoga todos os jti da tarefa"). Returns how many rows changed.
    """
    return await _revoke_where(session, IssuedToken.task_id == task_id)
