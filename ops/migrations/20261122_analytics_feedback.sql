-- Additive migration; apply with ops/apply_additive_migration.py after a backup.
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS request_assignments (
    request_id INTEGER NOT NULL PRIMARY KEY,
    master_user_id INTEGER NOT NULL,
    master_name VARCHAR(200) NOT NULL,
    assigned_at DATETIME NOT NULL,
    FOREIGN KEY(request_id) REFERENCES repair_requests (id) ON DELETE CASCADE,
    FOREIGN KEY(master_user_id) REFERENCES users (id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS ix_request_assignments_master_user_id ON request_assignments (master_user_id);
CREATE TABLE IF NOT EXISTS request_completions (
    request_id INTEGER NOT NULL PRIMARY KEY,
    completed_at DATETIME NOT NULL,
    FOREIGN KEY(request_id) REFERENCES repair_requests (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_request_completions_completed_at ON request_completions (completed_at);
CREATE TABLE IF NOT EXISTS repair_ratings (
    request_id INTEGER NOT NULL PRIMARY KEY,
    score INTEGER NOT NULL,
    comment TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    FOREIGN KEY(request_id) REFERENCES repair_requests (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_repair_ratings_created_at ON repair_ratings (created_at);
CREATE TABLE IF NOT EXISTS repair_complaints (
    request_id INTEGER NOT NULL PRIMARY KEY,
    category VARCHAR(32) NOT NULL,
    message TEXT NOT NULL,
    master_user_id INTEGER,
    master_name VARCHAR(200) NOT NULL,
    created_at DATETIME NOT NULL,
    FOREIGN KEY(request_id) REFERENCES repair_requests (id) ON DELETE CASCADE,
    FOREIGN KEY(master_user_id) REFERENCES users (id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS ix_repair_complaints_master_user_id ON repair_complaints (master_user_id);
CREATE INDEX IF NOT EXISTS ix_repair_complaints_created_at ON repair_complaints (created_at);
COMMIT;
