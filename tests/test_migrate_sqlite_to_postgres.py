import sqlite3

from repair_bot.scripts.migrate_sqlite_to_postgres import (
    backup_sqlite_database,
    normalize_postgres_url,
)


def test_backup_sqlite_database_uses_online_backup_api(tmp_path):
    source_path = tmp_path / "source.db"
    with sqlite3.connect(source_path) as source:
        source.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        source.execute("INSERT INTO sample(value) VALUES ('saved')")

    backup_path = backup_sqlite_database(source_path)

    assert backup_path.is_file()
    assert backup_path != source_path
    with sqlite3.connect(backup_path) as backup:
        assert backup.execute("SELECT value FROM sample").fetchone() == ("saved",)


def test_postgres_url_normalization_preserves_async_driver_urls():
    assert normalize_postgres_url("postgres://u:p@db/app") == (
        "postgresql+asyncpg://u:p@db/app"
    )
    assert normalize_postgres_url("postgresql://u:p@db/app") == (
        "postgresql+asyncpg://u:p@db/app"
    )
    assert normalize_postgres_url("postgresql+asyncpg://u:p@db/app") == (
        "postgresql+asyncpg://u:p@db/app"
    )
