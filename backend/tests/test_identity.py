"""Agent/user identity: issuing, verifying and revoking RS256 JWTs.

`keys` is an ephemeral RSA pair built with `cryptography` directly (see `_kid_for`, imported
from the module under test so both sides compute the same `kid`), never `identity.load_keys()`
reading a file: neither this suite nor CI needs a key on disk. `issued_tokens` has a real
foreign key to `tasks`, so an agent token needs a real task row, `task_id` builds one.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import stat
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from warden import audit, identity
from warden.config import Settings
from warden.identity import generate_keys
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


@pytest.fixture
async def clean_committed_rows(session: AsyncSession) -> AsyncIterator[None]:
    """For the few tests here that commit, because what they prove only exists across two
    real connections. Committed rows outlive the `session` fixture's rollback, and
    test_audit.py asserts on an empty log: without this the suite passes or fails depending
    on which file pytest happens to collect first. Same pattern as test_audit.py and
    test_queue.py, as superuser.
    """
    wipe = text("TRUNCATE audit_log, issued_tokens, tasks, users RESTART IDENTITY CASCADE")
    await session.execute(wipe)
    await session.commit()
    yield
    await session.execute(wipe)
    await session.commit()


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


async def test_a_revocation_from_another_session_is_seen_immediately(
    session_factory: async_sessionmaker[AsyncSession],
    keys: KeyPair,
    clean_committed_rows: None,
) -> None:
    """The entire point of a stateful, revocable `jti` (ADR-005) is that revoking it reaches
    every session checking it, not just the one that issued it. Uses `session_factory`
    directly, not the `session` fixture (which always rolls back), because the bug only shows
    up across two genuinely separate, *committed* connections: the production session factory
    runs with `expire_on_commit=False` (see warden/db.py), so a session that already has this
    row warm in its identity map keeps returning that cached copy after its own commit, unless
    verify() forces a re-read with `populate_existing=True`.

    `pinned_row` matters: SQLAlchemy's identity map holds only a *weak* reference, so without
    something else keeping the row alive it would be garbage-collected the moment verify()'s
    own local `row` variable goes out of scope, and the second verify() call below would hit
    Postgres for a genuinely fresh SELECT regardless of populate_existing, silently passing
    for the wrong reason. Pinning it is what makes this test actually exercise the cache.
    """
    async with session_factory() as session_a:
        user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="submitter")
        session_a.add(user)
        await session_a.flush()
        task = Task(user_id=user.id, spec="cross-session revoke", idempotency_key=str(uuid.uuid4()))
        session_a.add(task)
        await session_a.flush()

        token = await identity.issue_agent_token(
            session_a, keys, task_id=task.id, scopes=["repo:read"]
        )
        claims = await identity.verify(session_a, keys, token)
        pinned_row = await session_a.get(IssuedToken, claims.jti)
        assert pinned_row is not None
        await session_a.commit()

        async with session_factory() as session_b:
            assert await identity.revoke(session_b, claims.jti) is True
            await session_b.commit()

        # Sanity check on the premise above: session_a's own copy is still stale here.
        assert pinned_row.revoked_at is None

        with pytest.raises(identity.InvalidToken):
            await identity.verify(session_a, keys, token)

        await session_a.rollback()


# --- claims are sourced from the row, not the token's own payload ---------------------------


async def test_rescoping_a_resigned_token_does_not_grant_new_scopes(
    session: AsyncSession, keys: KeyPair, task_id: uuid.UUID
) -> None:
    """`sub`/`scopes` come from the `issued_tokens` row (see the `Claims` docstring), the same
    way `task_id` already did. Simulates exactly the scenario the unknown-jti check exists for:
    a leaked signing key lets an attacker take a live, known `jti` and re-sign it with scopes
    and a subject it was never issued for. verify() must answer from the row it looked up, not
    from whatever the forged payload claims.
    """
    token = await identity.issue_agent_token(session, keys, task_id=task_id, scopes=["repo:read"])
    claims = await identity.verify(session, keys, token)

    payload = jwt.decode(
        token, keys.public_key, algorithms=[ALGORITHM], audience=AUDIENCE, issuer=ISSUER
    )
    payload["scopes"] = ["repo:read", "repo:write", "admin"]
    payload["sub"] = f"agent:task:{uuid.uuid4()}"
    forged = _sign(payload, keys)

    # Refused outright, even for the scope the jti really has. A validly signed token that
    # disagrees with its own row was not minted here: quietly serving it with the original
    # permissions would turn a leaked key into a working credential instead of an incident.
    with pytest.raises(identity.InvalidToken):
        await identity.verify(session, keys, forged, required_scope="repo:read")
    with pytest.raises(identity.InvalidToken):
        await identity.verify(session, keys, forged, required_scope="admin")

    # The genuine token is unaffected by someone else's forgery of its jti.
    assert (await identity.verify(session, keys, token)).scopes == claims.scopes


@pytest.mark.parametrize(
    ("claim", "forged_value"),
    [
        # The signature check only knows the expiry the token claims for itself.
        ("exp", int((datetime.now(UTC) + timedelta(days=365)).timestamp())),
        # No column holds typ, so the row answers for it through the subject it recorded.
        ("typ", "user"),
    ],
)
async def test_a_resigned_live_jti_cannot_change_its_expiry_or_its_typ(
    session: AsyncSession, keys: KeyPair, task_id: uuid.UUID, claim: str, forged_value: object
) -> None:
    token = await identity.issue_agent_token(session, keys, task_id=task_id, scopes=["repo:read"])
    payload = jwt.decode(
        token, keys.public_key, algorithms=[ALGORITHM], audience=AUDIENCE, issuer=ISSUER
    )
    payload[claim] = forged_value

    with pytest.raises(identity.InvalidToken):
        await identity.verify(session, keys, _sign(payload, keys))


async def test_revoking_through_a_session_with_a_stale_copy_audits_the_revocation_once(
    session_factory: async_sessionmaker[AsyncSession],
    keys: KeyPair,
    clean_committed_rows: None,
) -> None:
    """The identity-map trap again, one function below where it was first fixed. Session A
    holds the row from before B revoked it; A revoking "again" used to see `revoked_at` as
    None in its cached copy, append a second `token.revoked` to a log that cannot be
    corrected, and move `revoked_at` forward. The guarded UPDATE lets the database say the
    state change already happened.
    """
    async with session_factory() as session_a:
        user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="submitter")
        session_a.add(user)
        await session_a.flush()
        task = Task(user_id=user.id, spec="double revoke", idempotency_key=str(uuid.uuid4()))
        session_a.add(task)
        await session_a.flush()
        token = await identity.issue_agent_token(
            session_a, keys, task_id=task.id, scopes=["repo:read"]
        )
        jti = (await identity.verify(session_a, keys, token)).jti
        pinned_row = await session_a.get(IssuedToken, jti)
        assert pinned_row is not None
        await session_a.commit()

        async with session_factory() as session_b:
            assert await identity.revoke(session_b, jti) is True
            await session_b.commit()
            first = await session_b.scalar(
                select(IssuedToken.revoked_at).where(IssuedToken.jti == jti)
            )

        assert pinned_row.revoked_at is None, "premise: A's copy is stale"
        assert await identity.revoke(session_a, jti) is True
        await session_a.commit()

        revocations = await session_a.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "token.revoked", AuditLog.target_id == str(jti))
        )
        assert revocations == 1
        kept = await session_a.scalar(select(IssuedToken.revoked_at).where(IssuedToken.jti == jti))
        assert kept == first


async def test_two_sessions_revoking_at_once_audit_it_once(
    session_factory: async_sessionmaker[AsyncSession],
    keys: KeyPair,
    clean_committed_rows: None,
) -> None:
    """The race the read-then-write version also lost: both sessions see NULL.

    Two revokes fired with `gather` do not prove this, they just run one after the other
    (that version of this test passed against the unfixed code). So the overlap is forced: A
    revokes and holds its transaction open, which holds the row lock, and B starts while A
    is still uncommitted. B cannot see A's change yet. Read-then-write reads NULL there,
    waits, and then revokes and audits a second time. The guarded UPDATE waits on the row
    lock, re-checks `revoked_at IS NULL` once A commits, and changes nothing.
    """
    async with session_factory() as setup:
        user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="submitter")
        setup.add(user)
        await setup.flush()
        task = Task(user_id=user.id, spec="revoke race", idempotency_key=str(uuid.uuid4()))
        setup.add(task)
        await setup.flush()
        token = await identity.issue_agent_token(setup, keys, task_id=task.id, scopes=["repo:read"])
        jti = (await identity.verify(setup, keys, token)).jti
        await setup.commit()

    async with session_factory() as session_a, session_factory() as session_b:
        assert await identity.revoke(session_a, jti) is True
        racing = asyncio.create_task(identity.revoke(session_b, jti))
        # Long enough for B to reach the lock. If it somehow had not, B would simply see A's
        # committed revocation and the test would pass for the boring reason, never fail.
        await asyncio.sleep(0.5)
        await session_a.commit()
        assert await racing is True
        await session_b.commit()

    async with session_factory() as probe:
        revocations = await probe.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "token.revoked", AuditLog.target_id == str(jti))
        )
        assert revocations == 1
        assert (await audit.verify(probe)).ok


async def test_string_exp_and_iat_are_refused_not_a_typeerror(
    session: AsyncSession, keys: KeyPair, task_id: uuid.UUID
) -> None:
    """PyJWT validates `exp`/`iat` by calling `int()` on them, so a numeric *string* survives
    `decode()` unchanged and only breaks later, in `datetime.fromtimestamp()`. A real, known
    jti is required to reach that code at all: an unknown jti is refused earlier. Before the
    fix, the TypeError escaped verify()'s documented InvalidToken/InsufficientScope contract,
    which a caller doing `except InvalidToken: return 401` would see as an unhandled 500.
    """
    token = await identity.issue_agent_token(session, keys, task_id=task_id, scopes=["repo:read"])
    payload = jwt.decode(
        token, keys.public_key, algorithms=[ALGORITHM], audience=AUDIENCE, issuer=ISSUER
    )
    payload["iat"] = str(payload["iat"])
    payload["exp"] = str(payload["exp"])
    malformed = _sign(payload, keys)

    with pytest.raises(identity.InvalidToken):
        await identity.verify(session, keys, malformed)


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


async def test_a_bare_string_scope_is_rejected(
    session: AsyncSession, keys: KeyPair, task_id: uuid.UUID
) -> None:
    """The likelier real mistake than a non-string element: a caller forgets the list
    brackets entirely. `str` satisfies `Sequence[str]`, so without an explicit check this
    would silently explode into one-character scopes instead of raising, and those characters
    would land in `issued_tokens` and the append-only audit log.
    """
    with pytest.raises(ValueError, match="single string"):
        await identity.issue_agent_token(session, keys, task_id=task_id, scopes="repo:read")


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


# --- key generation: permission and the atomic refuse-to-overwrite guarantee ----------------


def test_generate_keys_writes_atomically_with_a_restrictive_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduces the finding: without an explicit mode, the key would inherit whatever the
    process umask is (0o644 on a typical Linux deploy host, world-readable), and the previous
    exists()-then-write() was a TOCTOU race on the "refuse to overwrite" guarantee. `os.open`
    with `O_EXCL` folds the check and the create into one atomic syscall and sets the mode
    itself, independent of umask. ADR-005: file permission is the only protection this key has.
    """
    key_path = tmp_path / "nested" / "jwt-private.pem"
    monkeypatch.setattr(
        generate_keys, "get_settings", lambda: Settings(jwt_private_key_path=str(key_path))
    )

    assert generate_keys.main() == 0
    assert key_path.is_file()
    if os.name == "posix":
        # Mode bits are not meaningful on Windows (ADR-005); this is the check that matters
        # for the actual VPS deploy target.
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600

    first_contents = key_path.read_bytes()
    assert generate_keys.main() == 1
    assert key_path.read_bytes() == first_contents
