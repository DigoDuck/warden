"""Agent/user identity: issuing, verifying and revoking RS256 JWTs.

`keys` is an ephemeral RSA pair built with `cryptography` directly (see `_kid_for`, imported
from the module under test so both sides compute the same `kid`), never `identity.load_keys()`
reading a file: neither this suite nor CI needs a key on disk. `issued_tokens` has a real
foreign key to `tasks`, so an agent token needs a real task row, `task_id` builds one.
"""

import base64
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from warden import audit, identity
from warden.config import Settings
from warden.identity.jwt import ALGORITHM, AUDIENCE, ISSUER, KeyPair, _kid_for
from warden.models import AuditLog, IssuedToken, Task, User


@pytest.fixture(scope="session")
def keys() -> KeyPair:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    return KeyPair(private_key=private_key, public_key=public_key, kid=_kid_for(public_key))


@pytest.fixture
async def user(session: AsyncSession) -> User:
    row = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="submitter")
    session.add(row)
    await session.flush()
    return row


@pytest.fixture
async def task_id(session: AsyncSession, user: User) -> uuid.UUID:
    row = Task(user_id=user.id, spec="test task", idempotency_key=str(uuid.uuid4()))
    session.add(row)
    await session.flush()
    return row.id


def _valid_claims(**overrides: object) -> dict[str, object]:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": f"agent:task:{uuid.uuid4()}",
        "jti": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=15)).timestamp()),
        "scopes": ["repo:read"],
        "typ": "agent",
    }
    claims.update(overrides)
    return claims


def _sign(claims: dict[str, object], keys: KeyPair) -> str:
    return jwt.encode(claims, keys.private_key, algorithm=ALGORITHM, headers={"kid": keys.kid})


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


# --- issue() / verify() round trip --------------------------------------------------------


async def test_round_trip_issue_then_verify(
    session: AsyncSession, keys: KeyPair, task_id: uuid.UUID
) -> None:
    token = await identity.issue_agent_token(
        session, keys, task_id=task_id, scopes=["repo:read", "repo:write"]
    )

    claims = await identity.verify(session, keys, token)

    assert claims.sub == f"agent:task:{task_id}"
    assert claims.typ == "agent"
    assert claims.scopes == ("repo:read", "repo:write")
    assert claims.task_id == task_id

    row = await session.get(IssuedToken, claims.jti)
    assert row is not None
    assert row.revoked_at is None
    assert row.scopes == ["repo:read", "repo:write"]
    # The token encodes `exp` as a whole-second unix timestamp; the row keeps the
    # microsecond-precision value `_issue()` actually computed. A second of slack covers that
    # truncation without the assertion caring about literal equality of two different clocks.
    assert abs((row.expires_at - claims.exp).total_seconds()) < 1


async def test_issue_user_token_round_trip(
    session: AsyncSession, keys: KeyPair, user: User
) -> None:
    token = await identity.issue_user_token(session, keys, user, scopes=["ui:read"])

    claims = await identity.verify(session, keys, token)

    assert claims.sub == f"user:{user.id}"
    assert claims.typ == "user"
    assert claims.task_id is None


# --- forged and malformed tokens: all InvalidToken -----------------------------------------


async def test_alg_none_is_refused(session: AsyncSession, keys: KeyPair) -> None:
    token = jwt.encode(_valid_claims(), key="", algorithm="none")
    with pytest.raises(identity.InvalidToken):
        await identity.verify(session, keys, token)


async def test_algorithm_confusion_hs256_with_the_public_key_pem_is_refused(
    session: AsyncSession, keys: KeyPair
) -> None:
    """The classic RS256-to-HS256 downgrade: a verifier that read `alg` from the token and
    used the RS256 public key as an HMAC secret would "verify" this, because the public key
    is, well, public. Pinning `algorithms=["RS256"]` in `verify()` is what actually stops it,
    PyJWT rejects the HS256 header before any signature comparison happens.

    Built by hand because PyJWT itself refuses to *sign* HS256 with a PEM-shaped key.
    """
    public_pem = keys.public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    header_b64 = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload_b64 = _b64url(json.dumps(_valid_claims(), separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{payload_b64}".encode()
    signature = hmac.new(public_pem, signing_input, hashlib.sha256).digest()
    token = f"{header_b64}.{payload_b64}.{_b64url(signature)}"

    with pytest.raises(identity.InvalidToken):
        await identity.verify(session, keys, token)


async def test_signed_by_a_different_key_is_refused(session: AsyncSession, keys: KeyPair) -> None:
    other_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(_valid_claims(), other_private_key, algorithm=ALGORITHM)

    with pytest.raises(identity.InvalidToken):
        await identity.verify(session, keys, token)


async def test_an_expired_token_is_refused(session: AsyncSession, keys: KeyPair) -> None:
    """Negative-skew timestamps, not `sleep()`: a 30-minute-old token that already expired
    15 minutes ago, computed once, not waited for.
    """
    now = datetime.now(UTC)
    claims = _valid_claims(
        iat=int((now - timedelta(minutes=30)).timestamp()),
        exp=int((now - timedelta(minutes=15)).timestamp()),
    )
    token = _sign(claims, keys)

    with pytest.raises(identity.InvalidToken):
        await identity.verify(session, keys, token)


async def test_wrong_audience_is_refused(session: AsyncSession, keys: KeyPair) -> None:
    token = _sign(_valid_claims(aud="someone-elses-tools"), keys)
    with pytest.raises(identity.InvalidToken):
        await identity.verify(session, keys, token)


async def test_wrong_issuer_is_refused(session: AsyncSession, keys: KeyPair) -> None:
    token = _sign(_valid_claims(iss="not-warden"), keys)
    with pytest.raises(identity.InvalidToken):
        await identity.verify(session, keys, token)


async def test_missing_jti_is_refused(session: AsyncSession, keys: KeyPair) -> None:
    claims = _valid_claims()
    del claims["jti"]
    token = _sign(claims, keys)

    with pytest.raises(identity.InvalidToken):
        await identity.verify(session, keys, token)


async def test_garbage_string_is_refused(session: AsyncSession, keys: KeyPair) -> None:
    with pytest.raises(identity.InvalidToken):
        await identity.verify(session, keys, "not-a-jwt-at-all")


# --- revocation and the fail-closed unknown-jti check ---------------------------------------


async def test_a_revoked_token_is_refused(
    session: AsyncSession, keys: KeyPair, task_id: uuid.UUID
) -> None:
    token = await identity.issue_agent_token(session, keys, task_id=task_id, scopes=["repo:read"])
    claims = await identity.verify(session, keys, token)

    assert await identity.revoke(session, claims.jti) is True

    with pytest.raises(identity.InvalidToken):
        await identity.verify(session, keys, token)


async def test_an_unknown_jti_is_refused(session: AsyncSession, keys: KeyPair) -> None:
    """Well-formed and correctly signed, but its `jti` has no row in `issued_tokens`: exactly
    what a leaked signing key would produce. `verify()` must fail closed here, not trust the
    signature alone.
    """
    token = _sign(_valid_claims(), keys)
    with pytest.raises(identity.InvalidToken):
        await identity.verify(session, keys, token)


async def test_revoking_an_unknown_jti_returns_false(session: AsyncSession) -> None:
    assert await identity.revoke(session, uuid.uuid4()) is False


async def test_revoking_twice_keeps_the_first_timestamp(
    session: AsyncSession, keys: KeyPair, task_id: uuid.UUID
) -> None:
    token = await identity.issue_agent_token(session, keys, task_id=task_id, scopes=["repo:read"])
    claims = await identity.verify(session, keys, token)

    assert await identity.revoke(session, claims.jti) is True
    row = await session.get(IssuedToken, claims.jti)
    assert row is not None
    first_revoked_at = row.revoked_at
    assert first_revoked_at is not None

    assert await identity.revoke(session, claims.jti) is True
    await session.refresh(row)
    assert row.revoked_at == first_revoked_at


async def test_revoke_all_for_task_revokes_only_that_tasks_tokens(
    session: AsyncSession, keys: KeyPair, task_id: uuid.UUID, user: User
) -> None:
    other_task = Task(user_id=user.id, spec="other task", idempotency_key=str(uuid.uuid4()))
    session.add(other_task)
    await session.flush()

    await identity.issue_agent_token(session, keys, task_id=task_id, scopes=["repo:read"])
    await identity.issue_agent_token(session, keys, task_id=task_id, scopes=["repo:read"])
    await identity.issue_agent_token(session, keys, task_id=other_task.id, scopes=["repo:read"])

    count = await identity.revoke_all_for_task(session, task_id)

    assert count == 2
    this_tasks_rows = (
        await session.scalars(select(IssuedToken).where(IssuedToken.task_id == task_id))
    ).all()
    assert len(this_tasks_rows) == 2
    assert all(row.revoked_at is not None for row in this_tasks_rows)

    other_tasks_rows = (
        await session.scalars(select(IssuedToken).where(IssuedToken.task_id == other_task.id))
    ).all()
    assert len(other_tasks_rows) == 1
    assert other_tasks_rows[0].revoked_at is None


# --- scopes -----------------------------------------------------------------------------


async def test_missing_scope_raises_insufficient_scope_not_invalid_token(
    session: AsyncSession, keys: KeyPair, task_id: uuid.UUID
) -> None:
    token = await identity.issue_agent_token(session, keys, task_id=task_id, scopes=["repo:read"])

    with pytest.raises(identity.InsufficientScope):
        await identity.verify(session, keys, token, required_scope="repo:write")


async def test_a_token_with_the_required_scope_passes(
    session: AsyncSession, keys: KeyPair, task_id: uuid.UUID
) -> None:
    token = await identity.issue_agent_token(
        session, keys, task_id=task_id, scopes=["repo:read", "repo:write"]
    )

    claims = await identity.verify(session, keys, token, required_scope="repo:write")

    assert "repo:write" in claims.scopes


async def test_empty_scopes_are_rejected(
    session: AsyncSession, keys: KeyPair, task_id: uuid.UUID
) -> None:
    with pytest.raises(ValueError, match="empty"):
        await identity.issue_agent_token(session, keys, task_id=task_id, scopes=[])


async def test_a_non_string_scope_is_rejected(
    session: AsyncSession, keys: KeyPair, task_id: uuid.UUID
) -> None:
    with pytest.raises(ValueError, match="string"):
        await identity.issue_agent_token(session, keys, task_id=task_id, scopes=[123])  # type: ignore[list-item]


# --- TTL caps ------------------------------------------------------------------------------


async def test_a_non_positive_ttl_is_rejected(
    session: AsyncSession, keys: KeyPair, task_id: uuid.UUID
) -> None:
    with pytest.raises(ValueError, match="positive"):
        await identity.issue_agent_token(
            session, keys, task_id=task_id, scopes=["repo:read"], ttl_seconds=0
        )


async def test_ttl_above_the_agent_cap_is_rejected(
    session: AsyncSession, keys: KeyPair, task_id: uuid.UUID
) -> None:
    """Briefing: agent tokens are 15 minutes. The cap is enforced, not just defaulted."""
    with pytest.raises(ValueError, match="cap"):
        await identity.issue_agent_token(
            session, keys, task_id=task_id, scopes=["repo:read"], ttl_seconds=901
        )


# --- audit: issued/revoked rows exist, and the token itself never appears in them -----------


async def test_issuing_and_revoking_are_audited_without_ever_recording_the_token(
    session: AsyncSession, keys: KeyPair, task_id: uuid.UUID
) -> None:
    token = await identity.issue_agent_token(session, keys, task_id=task_id, scopes=["repo:read"])
    claims = await identity.verify(session, keys, token)
    await identity.revoke(session, claims.jti)

    rows = (await session.scalars(select(AuditLog).order_by(AuditLog.id))).all()
    actions = [row.action for row in rows]
    assert "token.issued" in actions
    assert "token.revoked" in actions

    result = await audit.verify(session)
    assert result.ok

    # No segment of the token, header, payload or signature, may appear in any row's details.
    header_b64, payload_b64, signature_b64 = token.split(".")
    forbidden = [token, header_b64, payload_b64, signature_b64]
    for row in rows:
        details_text = json.dumps(row.details)
        for segment in forbidden:
            assert segment not in details_text


# --- database privileges: warden_app can write issued_tokens (proves migration 0004) --------


async def test_warden_app_can_insert_and_revoke_issued_tokens(
    session: AsyncSession, keys: KeyPair, task_id: uuid.UUID
) -> None:
    """Also the proof migration 0004's comment promises: this runs under the same
    `ALTER DEFAULT PRIVILEGES` grant from migration 0003, with no extra GRANT in 0004, and
    passes. If a future migration ever needs to touch `issued_tokens`' privileges, this test
    breaking is the signal.
    """
    await session.execute(text("SET LOCAL ROLE warden_app"))

    token = await identity.issue_agent_token(session, keys, task_id=task_id, scopes=["repo:read"])
    claims = await identity.verify(session, keys, token)
    assert await identity.revoke(session, claims.jti) is True

    row = await session.get(IssuedToken, claims.jti)
    assert row is not None
    assert row.revoked_at is not None


# --- key loading ----------------------------------------------------------------------------


def test_load_keys_with_a_missing_file_mentions_make_keys(tmp_path: Path) -> None:
    settings = Settings(jwt_private_key_path=str(tmp_path / "does-not-exist.pem"))
    with pytest.raises(FileNotFoundError, match="make keys"):
        identity.load_keys(settings)
