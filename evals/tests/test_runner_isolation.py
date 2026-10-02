"""The eval run must never be able to reach the real GitHub, whatever the shell exports.

Case 3 registers github.open_pr and relies on the approval gate to stop it. If that gate
ever regresses (the exact thing the case exists to catch), the call goes through the real
gateway, so the credentials and the API host it would use have to be inert here too.
"""

import pytest
from warden.config import get_settings

from evals import runner


def test_isolate_github_overrides_credentials_already_in_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_a_real_looking_token")
    monkeypatch.setenv("GITHUB_REPO", "someone/real-repo")
    monkeypatch.setenv("GITHUB_API_URL", "https://api.github.com")
    try:
        runner.isolate_github()
        settings = get_settings()
        assert settings.github_token.get_secret_value() != "ghp_a_real_looking_token"
        assert settings.github_repo != "someone/real-repo"
        assert settings.github_api_url.startswith("http://127.0.0.1:")
    finally:
        get_settings.cache_clear()
