"""identity/broker.py: who gets a third-party credential, and what never leaks about it.

`_agent_claims()` builds a `Claims` object by hand rather than going through a real
`issue_agent_token`/`verify` round trip for most cases: the broker's contract is entirely a
function of the `Claims` fields (see its docstring), so a hand-built one is enough to exercise
every branch. `test_a_real_verified_token_can_be_exchanged_for_a_credential` is the one test
that proves the two modules actually plug together end to end.
"""

import json
import uuid
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
from warden.models import AuditLog, Task, User

# Deliberately not GitHub- or OpenAI-token-shaped (no "ghp_"/"sk-" prefix): a string a secret
# scanner could flag as a real leaked credential defeats the point of a fixture.
FAKE_TOKEN = "not-a-real-secret-abcdefghijklmnop"


def _settings_with(token: str) -> Settings:
    """`Settings(github_token=...)` alone fails mypy: pydantic's generated `__init__` (no mypy
    plugin configured here) types the field as `SecretStr`, not `SecretStr | str`, even though
    the runtime validator happily coerces a plain string. One explicit wrapper, not a plugin
    just for this.
    """
    return Settings(github_token=SecretStr(token))


def _agent_claims(**overrides: object) -> Claims:
    now = datetime.now(UTC)
    fields: dict[str, object] = {
        "sub": f"agent:task:{uuid.uuid4()}",
        "jti": uuid.uuid4(),
        "typ": "agent",
        "scopes": ("github:pr:open",),
        "task_id": uuid.uuid4(),
        "iat": now,
        "exp": now + timedelta(minutes=15),
    }
    fields.update(overrides)
    return Claims(**fields)  # type: ignore[arg-type]


async def _denials(session: AsyncSession) -> list[AuditLog]:
    rows = (
        await session.scalars(
            select(AuditLog).where(AuditLog.action == "credential.denied").order_by(AuditLog.id)
        )
    ).all()
    return list(rows)


# --- granting ---------------------------------------------------------------------------


async def test_grant_with_correct_scope_returns_the_configured_secret(
    session: AsyncSession,
) -> None:
    claims = _agent_claims(scopes=("github:pr:open",))
    settings = _settings_with(FAKE_TOKEN)

    credential = await broker.get_credential(session, claims, "github:pr:open", settings=settings)

    assert credential.reveal() == FAKE_TOKEN


async def test_a_grant_appends_exactly_one_audit_row_naming_task_and_scope(
    session: AsyncSession,
) -> None:
    claims = _agent_claims(scopes=("github:repo:read",))
    settings = _settings_with(FAKE_TOKEN)

    await broker.get_credential(session, claims, "github:repo:read", settings=settings)

    rows = (
        await session.scalars(select(AuditLog).where(AuditLog.action == "credential.granted"))
    ).all()
    assert len(rows) == 1
    assert rows[0].details["task_id"] == str(claims.task_id)
    assert rows[0].details["jti"] == str(claims.jti)
    assert rows[0].details["scope"] == "github:repo:read"


async def test_a_real_verified_token_can_be_exchanged_for_a_credential(
    session: AsyncSession,
) -> None:
    """The integration point: a token minted by `issue_agent_token` and checked by `verify()`,
    the exact object the rest of the system will hand to this broker, is accepted.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    keys = KeyPair(private_key=private_key, public_key=public_key, kid=_kid_for(public_key))

    user = User(email=f"{uuid.uuid4()}@warden.test", password_hash="x", role="submitter")
    session.add(user)
    await session.flush()
    task = Task(user_id=user.id, spec="open a pr", idempotency_key=str(uuid.uuid4()))
    session.add(task)
    await session.flush()

    token = await identity.issue_agent_token(
        session, keys, task_id=task.id, scopes=["github:pr:open"]
    )
    claims = await identity.verify(session, keys, token)
    settings = _settings_with(FAKE_TOKEN)

    credential = await broker.get_credential(session, claims, "github:pr:open", settings=settings)

    assert credential.reveal() == FAKE_TOKEN


# --- refusals -----------------------------------------------------------------------------


async def test_refuse_without_the_scope(session: AsyncSession) -> None:
    claims = _agent_claims(scopes=("github:repo:read",))
    settings = _settings_with(FAKE_TOKEN)

    with pytest.raises(broker.CredentialDenied):
        await broker.get_credential(session, claims, "github:pr:open", settings=settings)


async def test_refuse_user_typ_claims(session: AsyncSession) -> None:
    claims = _agent_claims(
        typ="user", sub=f"user:{uuid.uuid4()}", task_id=None, scopes=("github:pr:open",)
    )
    settings = _settings_with(FAKE_TOKEN)

    with pytest.raises(broker.CredentialDenied):
        await broker.get_credential(session, claims, "github:pr:open", settings=settings)


async def test_refuse_an_agent_typ_claims_with_no_task_bound(session: AsyncSession) -> None:
    """Not something `issue_agent_token` can produce (it requires `task_id`), but a defensive
    check for the same "task-bound agent token" contract the ADR describes.
    """
    claims = _agent_claims(task_id=None)
    settings = _settings_with(FAKE_TOKEN)

    with pytest.raises(broker.CredentialDenied):
        await broker.get_credential(session, claims, "github:pr:open", settings=settings)


async def test_refuse_unknown_scope(session: AsyncSession) -> None:
    claims = _agent_claims(scopes=("totally:unknown",))
    settings = _settings_with(FAKE_TOKEN)

    with pytest.raises(broker.UnknownScope):
        await broker.get_credential(session, claims, "totally:unknown", settings=settings)


async def test_refuse_when_unconfigured(session: AsyncSession) -> None:
    claims = _agent_claims(scopes=("github:pr:open",))
    settings = Settings()  # github_token defaults to empty

    with pytest.raises(broker.SecretNotConfigured):
        await broker.get_credential(session, claims, "github:pr:open", settings=settings)


# --- every refusal path audits, and none of it leaks the secret ----------------------------


_CONFIGURED = _settings_with(FAKE_TOKEN)
_UNCONFIGURED = Settings()  # github_token defaults to empty

_REFUSAL_CASES = [
    # (claims overrides, requested scope, settings, expected exception)
    ({"scopes": ("github:repo:read",)}, "github:pr:open", _CONFIGURED, broker.CredentialDenied),
    (
        {"typ": "user", "sub": f"user:{uuid.uuid4()}", "task_id": None},
        "github:pr:open",
        _CONFIGURED,
        broker.CredentialDenied,
    ),
    ({"scopes": ("totally:unknown",)}, "totally:unknown", _CONFIGURED, broker.UnknownScope),
    ({"scopes": ("github:pr:open",)}, "github:pr:open", _UNCONFIGURED, broker.SecretNotConfigured),
]


@pytest.mark.parametrize(
    ("claims_overrides", "requested_scope", "settings", "expected_exception"), _REFUSAL_CASES
)
async def test_every_refusal_audits_without_a_trace_of_the_secret(
    session: AsyncSession,
    claims_overrides: dict[str, object],
    requested_scope: str,
    settings: Settings,
    expected_exception: type[Exception],
) -> None:
    claims = _agent_claims(**claims_overrides)

    with pytest.raises(expected_exception) as excinfo:
        await broker.get_credential(session, claims, requested_scope, settings=settings)

    assert FAKE_TOKEN not in str(excinfo.value)

    rows = await _denials(session)
    assert len(rows) == 1
    details_text = json.dumps(rows[-1].details)
    assert FAKE_TOKEN not in details_text

    result = await audit.verify(session)
    assert result.ok


async def test_credential_repr_and_str_never_reveal_the_value(session: AsyncSession) -> None:
    claims = _agent_claims(scopes=("github:pr:open",))
    settings = _settings_with(FAKE_TOKEN)

    credential = await broker.get_credential(session, claims, "github:pr:open", settings=settings)

    assert FAKE_TOKEN not in repr(credential)
    assert FAKE_TOKEN not in str(credential)


# --- redact() -----------------------------------------------------------------------------


def test_redact_replaces_the_configured_secret() -> None:
    settings = _settings_with(FAKE_TOKEN)
    text = f"clone failed: remote rejected {FAKE_TOKEN} for user bot"

    result = broker.redact(text, settings=settings)

    assert FAKE_TOKEN not in result
    assert "[redacted]" in result


def test_redact_leaves_text_alone_when_no_secret_is_configured() -> None:
    settings = Settings()  # empty github_token
    text = "nothing sensitive here"

    assert broker.redact(text, settings=settings) == text


def test_redact_does_not_touch_short_values() -> None:
    """A short token would otherwise get replaced at nearly every position in the string,
    turning an unconfigured or trivially short "secret" into a corrupted log line.
    """
    settings = _settings_with("short1")
    text = "the word short1 appears once"

    assert broker.redact(text, settings=settings) == text
