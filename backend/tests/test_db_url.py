from warden.db import with_database


def test_with_database_preserves_password() -> None:
    """`str(URL)` masks the password as "***". If anyone swaps render_as_string back
    for str, this breaks here instead of surfacing as an auth failure in CI."""
    url = with_database("postgresql+asyncpg://warden:s3cr3t@localhost:5434/warden", "postgres")
    assert url == "postgresql+asyncpg://warden:s3cr3t@localhost:5434/postgres"
