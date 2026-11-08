-- Additive SQLite migration; take a backup before applying.
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS gift_certificates (
	id INTEGER NOT NULL, 
	owner_id INTEGER NOT NULL, 
	source_request_id INTEGER, 
	source_number INTEGER NOT NULL, 
	qualifying_total_kopecks INTEGER NOT NULL, 
	code VARCHAR(32) NOT NULL, 
	amount_kopecks INTEGER NOT NULL, 
	target_request_id INTEGER, 
	created_at DATETIME NOT NULL, 
	used_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(owner_id) REFERENCES users (id) ON DELETE CASCADE, 
	UNIQUE (source_request_id), 
	FOREIGN KEY(source_request_id) REFERENCES repair_requests (id) ON DELETE SET NULL, 
	UNIQUE (code), 
	UNIQUE (target_request_id), 
	FOREIGN KEY(target_request_id) REFERENCES repair_requests (id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS ix_gift_certificates_owner_id ON gift_certificates (owner_id);
COMMIT;
