from functools import lru_cache

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
