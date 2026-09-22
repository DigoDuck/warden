from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration read from the environment. No secret lives in a tracked file."""

    # Commands run from backend/, but the .env file sits at the repository root.
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 5434 is the default port docker-compose publishes (WARDEN_DB_PORT). See the
    # comment there for why it is not 5432.
    database_url: str = "postgresql+asyncpg://warden:warden@localhost:5434/warden"

    # Read from the .env file rather than the process environment so that a run cannot
    # pick up a key by accident. Empty means the demo refuses to call the real API.
    anthropic_api_key: str = ""

    # The private key never lives in the repository (see .gitignore's `.keys/` and `*.pem`).
    # Relative paths resolve against the repository root, not the process's cwd, in
    # warden/identity/jwt.py: `make keys` and the app itself run from different working
    # directories, and a cwd-relative default would silently point at two different files.
    jwt_private_key_path: str = ".keys/jwt-private.pem"

    # Read by identity/broker.py. SecretStr rather than str: pydantic's repr/str for this type
    # is always "**********", so a stray `print(settings)`, log line or traceback cannot show
    # the value by accident, the same guarantee Credential (broker.py) gives at the call site.
    # Empty means the broker refuses every GitHub scope (see broker.SecretNotConfigured).
    github_token: SecretStr = SecretStr("")


@lru_cache
def get_settings() -> Settings:
    return Settings()
