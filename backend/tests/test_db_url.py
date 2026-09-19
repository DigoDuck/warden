from warden.db import with_database


def test_with_database_preserves_password() -> None:
    """`str(URL)` mascara a senha como "***". Se alguem trocar render_as_string por str,
    este teste quebra aqui em vez de virar um "password authentication failed" no CI."""
    url = with_database("postgresql+asyncpg://warden:s3cr3t@localhost:5434/warden", "postgres")
    assert url == "postgresql+asyncpg://warden:s3cr3t@localhost:5434/postgres"
