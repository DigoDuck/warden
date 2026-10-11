"""identity/broker.py::redact finds credentials by shape, not only by configured value.

Until now `redact` replaced the one configured GitHub token and nothing else, so a token the
agent read out of a file, or one a tool echoed back, went into events, `args_safe`, evidence
and now span attributes untouched. These pin the shapes that matter for the secrets this
project handles or that an agent is likely to run into, and the text that must survive.
"""

import pytest

from tests import fake_secrets
from warden.config import Settings
from warden.identity import broker

# No configured secret: every case below must be caught by shape alone.
_NOTHING_CONFIGURED = Settings(github_token="")  # type: ignore[arg-type]

_SHAPES = {
    "github classic token": fake_secrets.github_pat(),
    "github fine-grained token": fake_secrets.github_fine_grained(),
    "anthropic key": fake_secrets.anthropic_key(),
    "aws access key id": fake_secrets.aws_access_key(),
    "jwt": fake_secrets.jwt(),
    "pem private key": fake_secrets.pem_private_key(),
}


@pytest.mark.parametrize("secret", _SHAPES.values(), ids=_SHAPES.keys())
def test_redact_replaces_a_credential_by_its_shape(secret: str) -> None:
    text = f"before {secret} after"

    result = broker.redact(text, settings=_NOTHING_CONFIGURED)

    assert result == "before [redacted] after"


def test_redact_replaces_a_pem_block_that_was_cut_short() -> None:
    """Output is truncated before it is stored, so the END line is often missing. Leaving the
    body because the footer is gone would defeat the detector on exactly the long outputs."""
    cut = fake_secrets.pem_private_key().split("-----END")[0] + "MIIEvQIBADAN"

    result = broker.redact(f"cat key.pem:\n{cut}", settings=_NOTHING_CONFIGURED)

    assert "MIIEvQIBADAN" not in result
    assert result.startswith("cat key.pem:\n")


@pytest.mark.parametrize(
    "text",
    [
        "pytest -k test_login src/app.py",
        "disk-usage-statistics-for-the-whole-workspace is a path-like word",
        "task-5f0e9c1a-77aa-4c7e-9a0e-3d2f4b1c9e55",
        "AKIA is a prefix, AKIAshort is not a key id",
        "ghp_short",
        "eyJ is how base64 JSON starts",
        "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----",
    ],
)
def test_redact_leaves_ordinary_text_alone(text: str) -> None:
    assert broker.redact(text, settings=_NOTHING_CONFIGURED) == text
