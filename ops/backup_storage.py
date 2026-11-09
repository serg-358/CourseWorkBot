#!/usr/bin/env python3
"""Согласованный бэкап SQLite и загруженных фото; три архива на VPS."""

import re
import sqlite3
import tarfile
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def backup_storage(database: Path, backup_dir: Path, keep: int = 3) -> Path:
    """Снимок базы согласован; фотографии неизменяемы и сохраняются вместе с ним."""
    database = database.resolve(strict=True)
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S%f")
    target = backup_dir / f"storage-{stamp}.tar.gz"
    with tempfile.TemporaryDirectory(prefix=".storage-", dir=backup_dir) as temp:
        snapshot = Path(temp) / "bot_database.db"
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source, closing(sqlite3.connect(snapshot)) as copy:
            snapshot.chmod(0o600)
            source.backup(copy)
            if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Резервная копия не прошла проверку целостности.")
        archive = Path(temp) / "backup.tar.gz"
        archive.touch(mode=0o600)
        with tarfile.open(archive, "w:gz") as output:
            output.add(snapshot, arcname="bot_database.db")
            photos = database.parent / "request_photos"
            if photos.is_dir():
                for photo in sorted(photos.iterdir()):
                    if photo.is_file() and not photo.is_symlink() and re.fullmatch(r"[a-f0-9]{32}\.jpg", photo.name):
                        output.add(photo, arcname="request_photos/" + photo.name, recursive=False)
        archive.replace(target)
    # Очищаем только созданные этим скриптом архивы, старые бэкапы SQLite сохраняем.
    archives = sorted(backup_dir.glob("storage-*.tar.gz"), reverse=True)
    for old in archives[keep:]:
        old.unlink()
    return target


if __name__ == "__main__":
    result = backup_storage(Path("/var/lib/docker/volumes/courseworkbot_bot_data/_data/bot_database.db"), Path("/var/backups/courseworkbot"))
    print("Storage backup OK:", result.name)
