from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuracao vinda do ambiente. Segredo nenhum mora em arquivo versionado."""

    # Os comandos rodam de backend/, mas o .env fica na raiz do repo.
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 5434: porta padrao publicada pelo docker-compose (WARDEN_DB_PORT). Ver o
    # comentario la sobre por que nao e 5432.
    database_url: str = "postgresql+asyncpg://warden:warden@localhost:5434/warden"


@lru_cache
def get_settings() -> Settings:
    return Settings()
