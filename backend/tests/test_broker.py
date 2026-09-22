"""identity/broker.py: who gets a third-party credential, and what never leaks about it.

Every `Claims` here comes from a real `issue_agent_token`/`issue_user_token` -> `verify()` round
trip, never built by hand: the broker re-reads the `issued_tokens` row behind the claims (a
hand-built `Claims` is exactly what it must refuse), so a test with a fake `jti` would only ever
exercise the refusal path. Forged claims are made with `dataclasses.replace` on a real one.
"""

import dataclasses
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from warden import audit, identity
from warden.config import Settings
from warden.identity import broker
from warden.identity.jwt import Claims, KeyPair, _kid_for
from warden.models import AuditLog, IssuedToken, Task, User

# Deliberately not GitHub- or OpenAI-token-shaped (no "ghp_"/"sk-" prefix): a string a secret
# scanner could flag as a real leaked credential defeats the point of a fixture.
FAKE_TOKEN = "not-a-real-secret-abcdefghijklmnop"


def _settings_with(token: str) -> Settings:
    """`Settings(github_token=...)` alone fails mypy: pydantic's generated `__init__` (no mypy
    plugin configured here) types the field as `SecretStr`, not `SecretStr | str`, even though
    the runtime validator happily coerces a plain string. One explicit wrapper, not a plugin
    just for this.

    Always pass the token explicitly, `""` included: a bare `Settings()` reads GITHUB_TOKEN
    from the developer's environment and `.env`, so the "unconfigured" tests would go red (and
    run against a real token) on exactly the machine that has one configured.
    """
    return Settings(github_token=SecretStr(token))


_CONFIGURED = _settings_with(FAKE_TOKEN)
_UNCONFIGURED = _settings_with("")


@pytest.fixture(scope="module")
def keys() -> KeyPair:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    return KeyPair(private_key=private_key, public_key=public_key, kid=_kid_for(public_key))


async def _user(session: AsyncSession) -> User:
    row = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="submitter")
    session.add(row)
    await session.flush()
    return row


async def _agent_claims(
    session: AsyncSession, keys: KeyPair, scopes: tuple[str, ...] = ("github:pr:open",)
) -> Claims:
    task = Task(
        user_id=(await _user(session)).id, spec="open a pr", idempotency_key=str(uuid.uuid4())
    )
    session.add(task)
    await session.flush()
    token = await identity.issue_agent_token(session, keys, task_id=task.id, scopes=list(scopes))
    return await identity.verify(session, keys, token)


async def _user_claims(session: AsyncSession, keys: KeyPair) -> Claims:
    token = await identity.issue_user_token(
        session, keys, await _user(session), scopes=["github:pr:open"]
    )
    return await identity.verify(session, keys, token)


async def _broker_rows(session: AsyncSession, action: str, claims: Claims) -> list[AuditLog]:
    """Scoped to this test's own `jti`: other tests commit, so the table is never assumed empty."""
    rows = await session.scalars(
        select(AuditLog)
        .where(AuditLog.action == action, AuditLog.target_id == str(claims.jti))
        .order_by(AuditLog.id)
    )
    return list(rows.all())


# --- granting ---------------------------------------------------------------------------


async def test_a_verified_agent_token_with_the_scope_gets_the_secret(
    session: AsyncSession, keys: KeyPair
) -> None:
    claims = await _agent_claims(session, keys, scopes=("github:pr:open",))

    credential = await broker.get_credential(
        session, claims, "github:pr:open", settings=_CONFIGURED
    )

    assert credential.reveal() == FAKE_TOKEN
    assert FAKE_TOKEN not in repr(credential)
    assert FAKE_TOKEN not in str(credential)


async def test_a_grant_appends_one_audit_row_naming_task_jti_and_scope_only(
    session: AsyncSession, keys: KeyPair
) -> None:
    claims = await _agent_claims(session, keys, scopes=("github:repo:read",))

    await broker.get_credential(session, claims, "github:repo:read", settings=_CONFIGURED)

    rows = await _broker_rows(session, "credential.granted", claims)
    assert len(rows) == 1
    # Exact equality, not "contains these keys": an extra field is where a secret, or a prefix
    # or hash of one, would slip in.
    assert rows[0].details == {
        "task_id": str(claims.task_id),
        "jti": str(claims.jti),
        "scope": "github:repo:read",
    }
    assert FAKE_TOKEN not in json.dumps(rows[0].details)


# --- refusals -----------------------------------------------------------------------------

Case = Callable[[AsyncSession, KeyPair], Awaitable[Claims]]


async def _lacks_scope(session: AsyncSession, keys: KeyPair) -> Claims:
    return await _agent_claims(session, keys, scopes=("github:repo:read",))


async def _no_task_bound(session: AsyncSession, keys: KeyPair) -> Claims:
    return dataclasses.replace(await _agent_claims(session, keys), task_id=None)


async def _revoked_after_verify(session: AsyncSession, keys: KeyPair) -> Claims:
    """Incident containment (`revoke_all_for_task`) must stop grants at once, even for a
    `Claims` that `verify()` produced before the revocation and is still held in memory.
    """
    claims = await _agent_claims(session, keys)
    assert claims.task_id is not None
    await identity.revoke_all_for_task(session, claims.task_id)
    return claims


async def _expired_after_verify(session: AsyncSession, keys: KeyPair) -> Claims:
    claims = await _agent_claims(session, keys)
    row = await session.get(IssuedToken, claims.jti)
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.flush()
    return claims


async def _jti_never_issued(session: AsyncSession, keys: KeyPair) -> Claims:
    return dataclasses.replace(await _agent_claims(session, keys), jti=uuid.uuid4())


async def _scopes_widened_by_hand(session: AsyncSession, keys: KeyPair) -> Claims:
    """A real, live jti whose token only carries `github:repo:read`, with the scope the caller
    wants pasted onto the dataclass. The `Claims` alone would pass the scope check.
    """
    claims = await _agent_claims(session, keys, scopes=("github:repo:read",))
    return dataclasses.replace(claims, scopes=("github:repo:read", "github:pr:open"))


async def _unknown_scope(session: AsyncSession, keys: KeyPair) -> Claims:
    return await _agent_claims(session, keys, scopes=("totally:unknown",))


_REFUSALS: list[tuple[Case, str, Settings, type[Exception]]] = [
    (_lacks_scope, "github:pr:open", _CONFIGURED, broker.CredentialDenied),
    (_user_claims, "github:pr:open", _CONFIGURED, broker.CredentialDenied),
    (_no_task_bound, "github:pr:open", _CONFIGURED, broker.CredentialDenied),
    (_revoked_after_verify, "github:pr:open", _CONFIGURED, broker.CredentialDenied),
    (_expired_after_verify, "github:pr:open", _CONFIGURED, broker.CredentialDenied),
    (_jti_never_issued, "github:pr:open", _CONFIGURED, broker.CredentialDenied),
    (_scopes_widened_by_hand, "github:pr:open", _CONFIGURED, broker.CredentialDenied),
    (_unknown_scope, "totally:unknown", _CONFIGURED, broker.UnknownScope),
    (_lacks_scope, "github:repo:read", _UNCONFIGURED, broker.SecretNotConfigured),
]


@pytest.mark.parametrize(
    ("make_claims", "scope", "settings", "expected"),
    _REFUSALS,
    ids=[f"{case.__name__.strip('_')}-{exc.__name__}" for case, _, _, exc in _REFUSALS],
)
async def test_every_refusal_raises_audits_once_and_never_leaks_the_secret(
    session: AsyncSession,
    keys: KeyPair,
    make_claims: Case,
    scope: str,
    settings: Settings,
    expected: type[Exception],
) -> None:
    claims = await make_claims(session, keys)

    with pytest.raises(expected) as excinfo:
        await broker.get_credential(session, claims, scope, settings=settings)

    assert FAKE_TOKEN not in str(excinfo.value)
    assert await _broker_rows(session, "credential.granted", claims) == []
    denials = await _broker_rows(session, "credential.denied", claims)
    assert len(denials) == 1
    assert set(denials[0].details) == {"task_id", "jti", "scope", "reason"}
    assert FAKE_TOKEN not in json.dumps(denials[0].details)
    assert (await audit.verify(session)).ok


# --- redact() -----------------------------------------------------------------------------


def test_redact_replaces_every_occurrence_of_the_configured_secret() -> None:
    text = f"clone failed: remote rejected {FAKE_TOKEN} for user bot, retry with {FAKE_TOKEN}"

    result = broker.redact(text, settings=_CONFIGURED)

    assert FAKE_TOKEN not in result
    assert result.count("[redacted]") == 2


def test_redact_leaves_text_alone_when_no_secret_is_configured() -> None:
    text = "nothing sensitive here"

    assert broker.redact(text, settings=_UNCONFIGURED) == text


def test_redact_does_not_touch_short_values() -> None:
    """A short token would otherwise get replaced at nearly every position in the string,
    turning an unconfigured or trivially short "secret" into a corrupted log line.
    """
    text = "the word short1 appears once"

    assert broker.redact(text, settings=_settings_with("short1")) == text
