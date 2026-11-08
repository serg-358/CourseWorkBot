#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/opt/courseworkbot"
BACKUP_DIR="/var/backups/courseworkbot"
STAMP="$(date -u +%Y%m%d_%H%M%S)"
POSTGRES_FILE="$BACKUP_DIR/postgres-$STAMP.sql.gz"
FSM_FILE="$BACKUP_DIR/fsm-$STAMP.db"
install -d -m 700 "$BACKUP_DIR"
POSTGRES_TMP="$(mktemp "$BACKUP_DIR/.postgres-$STAMP.XXXXXX")"
FSM_TMP="$(mktemp "$BACKUP_DIR/.fsm-$STAMP.XXXXXX")"

cleanup() {
    rm -f -- "$POSTGRES_TMP" "$FSM_TMP"
}
trap cleanup EXIT

cd "$PROJECT_DIR"

docker compose -f compose.yaml -f compose.web.yaml --profile web exec -T postgres \
    sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' \
    | gzip -9 > "$POSTGRES_TMP"

docker compose -f compose.yaml -f compose.web.yaml --profile web exec -T bot \
    python -c "import os,sqlite3,sys,tempfile; src=sqlite3.connect('/data/bot_database.db'); f=tempfile.NamedTemporaryFile(delete=False); f.close(); dst=sqlite3.connect(f.name); src.backup(dst); dst.close(); sys.stdout.buffer.write(open(f.name,'rb').read()); os.unlink(f.name)" \
    > "$FSM_TMP"

test -s "$POSTGRES_TMP"
test -s "$FSM_TMP"
chmod 600 "$POSTGRES_TMP" "$FSM_TMP"
mv -- "$POSTGRES_TMP" "$POSTGRES_FILE"
mv -- "$FSM_TMP" "$FSM_FILE"
trap - EXIT

mapfile -t old_postgres_backups < <(
    ls -1t "$BACKUP_DIR"/postgres-*.sql.gz 2>/dev/null || true
)
for old_backup in "${old_postgres_backups[@]:3}"; do
    stamp="$(basename "$old_backup" .sql.gz | sed 's/^postgres-//')"
    rm -f -- "$old_backup" "$BACKUP_DIR/fsm-$stamp.db"
done
