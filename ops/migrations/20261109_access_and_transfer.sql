-- Additive migration; back up SQLite before application.
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS ai_credit_grants (
	id INTEGER NOT NULL,
	telegram_id VARCHAR NOT NULL,
	actor_name VARCHAR(200) NOT NULL,
	credits INTEGER NOT NULL,
	amount_kopecks INTEGER NOT NULL,
	reason VARCHAR(32) NOT NULL,
	reference VARCHAR(64),
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (reference)
);

CREATE INDEX IF NOT EXISTS ix_ai_credit_grants_telegram_id ON ai_credit_grants (telegram_id);

CREATE TABLE IF NOT EXISTS ai_wallets (
	telegram_id VARCHAR NOT NULL,
	free_used BOOLEAN NOT NULL,
	credits INTEGER NOT NULL,
	PRIMARY KEY (telegram_id)
);

CREATE TABLE IF NOT EXISTS link_code_rate_limits (
	"key" VARCHAR(100) NOT NULL,
	window_start DATETIME NOT NULL,
	attempts INTEGER NOT NULL,
	PRIMARY KEY ("key")
);

CREATE TABLE IF NOT EXISTS request_import_batches (
	id VARCHAR(64) NOT NULL,
	actor_id INTEGER NOT NULL,
	payload TEXT NOT NULL,
	created_at DATETIME NOT NULL,
	expires_at DATETIME NOT NULL,
	used_at DATETIME,
	PRIMARY KEY (id),
	FOREIGN KEY(actor_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS telegram_access_codes (
	id INTEGER NOT NULL,
	user_id INTEGER NOT NULL,
	code_hash VARCHAR(64) NOT NULL,
	created_at DATETIME NOT NULL,
	expires_at DATETIME NOT NULL,
	used_at DATETIME,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	UNIQUE (code_hash)
);

CREATE INDEX IF NOT EXISTS ix_telegram_access_codes_user_id ON telegram_access_codes (user_id);

CREATE TABLE IF NOT EXISTS ai_usages (
	request_id INTEGER NOT NULL,
	actor_telegram_id VARCHAR NOT NULL,
	kind VARCHAR(32) NOT NULL,
	state VARCHAR(16) NOT NULL,
	attempts INTEGER NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (request_id),
	FOREIGN KEY(request_id) REFERENCES repair_requests (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS request_lifecycle (
	request_id INTEGER NOT NULL,
	deleted_at DATETIME,
	deleted_by_name VARCHAR(200) NOT NULL,
	imported BOOLEAN NOT NULL,
	external_key VARCHAR(64),
	provenance TEXT,
	photo_file_id VARCHAR,
	PRIMARY KEY (request_id),
	FOREIGN KEY(request_id) REFERENCES repair_requests (id) ON DELETE CASCADE,
	UNIQUE (external_key)
);

CREATE TABLE IF NOT EXISTS staff_edit_details (
	edit_id INTEGER NOT NULL,
	parts TEXT NOT NULL,
	tools TEXT NOT NULL,
	PRIMARY KEY (edit_id),
	FOREIGN KEY(edit_id) REFERENCES repair_request_staff_edits (id) ON DELETE CASCADE
);
COMMIT;
