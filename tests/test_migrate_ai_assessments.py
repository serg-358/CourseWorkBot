import sqlite3

import pytest

from migrate_ai_assessments import migrate_ai_assessments


@pytest.mark.asyncio
async def test_migration_creates_assessment_table_and_backup(tmp_path) -> None:
    database_path = tmp_path / "existing.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE repair_requests (id INTEGER PRIMARY KEY)")

    backup_path = await migrate_ai_assessments(database_path)

    assert backup_path is not None
    assert backup_path.exists()
    with sqlite3.connect(database_path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert "repair_request_ai_assessments" in tables


@pytest.mark.asyncio
async def test_migration_is_idempotent(tmp_path) -> None:
    database_path = tmp_path / "existing.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE repair_requests (id INTEGER PRIMARY KEY)")

    await migrate_ai_assessments(database_path)

    assert await migrate_ai_assessments(database_path) is None
