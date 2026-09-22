"""Secret Broker: hands a third-party credential to a verified agent, never to the model.

See docs/adr/ADR-006-secret-broker.md for why a proven `Claims` object is required instead of
trusting a task id, why the secret lives in the control plane's environment instead of the
database, and what this does not protect against.

Scope note (briefing §10): `identity` brokers secrets but never stores one in text in the DB.
This module does not decide *whether* a scope belongs on a token in the first place, that
happens upstream when the token is issued (`identity.issue_agent_token`, on the policy engine's
`allow` path); it only answers "does this already-proven identity, with this already-proven
scope, get this one credential".
"""

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from warden import audit
from warden.config import Settings, get_settings
from warden.identity.jwt import Claims

# The entire authorization surface for third-party credentials, in one place a reviewer can
# read in a glance rather than trace through code. Each scope names exactly one setting on
# `Settings`; adding a second GitHub-shaped secret (or a second provider) means one more row
# here, not a new code path.
_SCOPE_TO_SETTING: dict[str, str] = {
    "github:pr:open": "github_token",
    "github:repo:read": "github_token",
}

# Below this length a "secret" is either unset (`""`) or too short to be a real credential.
# redact() skips values shorter than this: replacing an empty or one-character string would
# otherwise mangle almost every string it is run on instead of hiding a real token.
_MIN_REDACTABLE_LENGTH = 8


class UnknownScope(ValueError):
    """`scope` has no secret mapped to it. A caller bug, not a security event: whoever picked
    this scope name is not asking for a credential that exists.
    """


class CredentialDenied(PermissionError):
    """The claims proved fine but do not authorize this credential: not an agent token, no
    task bound to it, or the token's own scopes do not include the one requested. Same family
    as `identity.InsufficientScope`, kept separate because this is a different resource
    (a third-party secret, not the API itself).
    """


class SecretNotConfigured(RuntimeError):
    """The scope is real and the claims are good, but the control plane has no value for the
    secret it maps to. The message names the scope, never the (nonexistent) value, so it gives
    an attacker nothing to distinguish "not configured" from "configured but wrong".
    """


class Credential:
    """A secret value whose repr/str never show it.

    Wraps `SecretStr` rather than a bare string so the masking is pydantic's, not
    hand-rolled. `.reveal()` is the only way to read the raw value, chosen so every real use
    site (the one place that must eventually call requests/httpx with this value) is
    greppable, the same reasoning as `identity.jwt`'s rule that a token itself never appears
    in a log line or an audit row.
    """

    __slots__ = ("_secret",)

    def __init__(self, value: str) -> None:
        self._secret = SecretStr(value)

    def reveal(self) -> str:
        return self._secret.get_secret_value()

    def __repr__(self) -> str:
        return "Credential(**********)"

    __str__ = __repr__


async def get_credential(
    session: AsyncSession,
    claims: Claims,
    scope: str,
    *,
    settings: Settings | None = None,
) -> Credential:
    """Hand out the secret mapped to `scope`, but only to claims that are a task-bound agent
    token and already carry that scope. `settings` is injectable the same way
    `identity.jwt.load_keys(settings=None)` is, so a test can pass a `Settings` built in memory
    instead of monkeypatching the `lru_cache`d `get_settings()` or touching a real `.env`.

    Every path, grant or refusal, appends exactly one audit row before returning or raising.
    None of them, and no exception message raised here, ever contains the secret value: the
    refusal reason is always one of a small fixed set of strings, never anything derived from
    the credential itself.

    Same short-transaction rule as `identity.jwt._issue()`: `audit.append()` holds a
    transaction-scoped advisory lock until *this session's* transaction commits, so call this
    from a short transaction and commit promptly, not from inside a long-running one.
    """
    resolved_settings = settings or get_settings()
    task_id = str(claims.task_id) if claims.task_id is not None else None

    async def deny(reason: str) -> None:
        await audit.append(
            session,
            actor_type="system",
            actor_id="broker",
            action="credential.denied",
            target_type="issued_token",
            target_id=str(claims.jti),
            details={"task_id": task_id, "jti": str(claims.jti), "scope": scope, "reason": reason},
        )

    # "Task-bound agent token", not just `typ == "agent"`: a hand-built Claims with the right
    # typ but no task_id is not something `identity.issue_agent_token` could ever produce (it
    # requires task_id), so seeing one here means whatever called this skipped verify().
    if claims.typ != "agent" or task_id is None:
        await deny("not a task-bound agent token")
        raise CredentialDenied("credential broker only serves task-bound agent claims")

    if scope not in claims.scopes:
        await deny("scope not granted to this token")
        raise CredentialDenied(f"token lacks scope {scope!r}")

    setting_name = _SCOPE_TO_SETTING.get(scope)
    if setting_name is None:
        await deny("unknown scope")
        raise UnknownScope(f"no secret is mapped to scope {scope!r}")

    value: SecretStr = getattr(resolved_settings, setting_name)
    revealed = value.get_secret_value()
    if not revealed:
        await deny("secret not configured")
        raise SecretNotConfigured(f"scope {scope!r} has no secret configured")

    await audit.append(
        session,
        actor_type="system",
        actor_id="broker",
        action="credential.granted",
        target_type="issued_token",
        target_id=str(claims.jti),
        details={"task_id": task_id, "jti": str(claims.jti), "scope": scope},
    )
    return Credential(revealed)


def _configured_secrets(settings: Settings) -> list[str]:
    """Every secret value `redact()` should scrub. One entry today; grows by one line, not a
    new mechanism, the day a second secret (e.g. a second provider's token) is configured.
    """
    return [settings.github_token.get_secret_value()]


def redact(text: str, *, settings: Settings | None = None) -> str:
    """Replace every occurrence of a configured secret value in `text` with "[redacted]".

    Plain substring replacement, not a regex built from the secret: a token can contain
    characters (`+`, `.`, `*`) that mean something else in a regex, and there is no reason to
    parse the input as anything other than literal text. This is what the week-3 checklist item
    "grep em logs e em task_events não encontra o token do GitHub" will run once the GitHub
    tools exist (wave 4 wires this into events/logging); this wave only provides and tests it.
    """
    resolved_settings = settings or get_settings()
    for value in _configured_secrets(resolved_settings):
        if len(value) >= _MIN_REDACTABLE_LENGTH:
            text = text.replace(value, "[redacted]")
    return text
