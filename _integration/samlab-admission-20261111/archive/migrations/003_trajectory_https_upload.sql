BEGIN;

CREATE TABLE IF NOT EXISTS trajectory_upload_device_codes (
    id UUID PRIMARY KEY,
    device_code_hash CHAR(64) NOT NULL UNIQUE,
    user_code_hash CHAR(64) NOT NULL UNIQUE,
    status VARCHAR(16) NOT NULL CHECK (status IN ('pending', 'approved', 'consumed', 'expired')),
    user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
    poll_interval_seconds INT NOT NULL DEFAULT 3 CHECK (poll_interval_seconds BETWEEN 1 AND 30),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    approved_at TIMESTAMPTZ,
    consumed_at TIMESTAMPTZ,
    last_poll_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS trajectory_upload_sessions (
    id UUID PRIMARY KEY,
    device_code_id UUID REFERENCES trajectory_upload_device_codes(id) ON DELETE SET NULL,
    import_batch_id UUID NOT NULL UNIQUE REFERENCES agent_trajectory_import_batches(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash CHAR(64) NOT NULL UNIQUE,
    status VARCHAR(16) NOT NULL CHECK (status IN ('active', 'completed', 'expired', 'revoked')),
    max_bytes BIGINT NOT NULL CHECK (max_bytes > 0),
    received_bytes BIGINT NOT NULL DEFAULT 0 CHECK (received_bytes >= 0),
    total_records INT NOT NULL DEFAULT 0 CHECK (total_records >= 0),
    inserted_records INT NOT NULL DEFAULT 0 CHECK (inserted_records >= 0),
    updated_records INT NOT NULL DEFAULT 0 CHECK (updated_records >= 0),
    skipped_records INT NOT NULL DEFAULT 0 CHECK (skipped_records >= 0),
    failed_records INT NOT NULL DEFAULT 0 CHECK (failed_records >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS trajectory_upload_items (
    session_id UUID NOT NULL REFERENCES trajectory_upload_sessions(id) ON DELETE CASCADE,
    trajectory_id VARCHAR(384) NOT NULL,
    request_sha256 CHAR(64) NOT NULL,
    request_bytes BIGINT NOT NULL CHECK (request_bytes > 0),
    status VARCHAR(16) NOT NULL CHECK (status IN ('processing', 'completed', 'failed')),
    action VARCHAR(16),
    error_code VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (session_id, trajectory_id)
);

CREATE INDEX IF NOT EXISTS idx_trajectory_upload_device_expiry
    ON trajectory_upload_device_codes (expires_at);
CREATE INDEX IF NOT EXISTS idx_trajectory_upload_sessions_user_created
    ON trajectory_upload_sessions (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trajectory_upload_sessions_expiry
    ON trajectory_upload_sessions (status, expires_at);
CREATE INDEX IF NOT EXISTS idx_trajectory_upload_items_status
    ON trajectory_upload_items (session_id, status, updated_at);

-- Pre-release builds briefly duplicated account PII in these short-lived tables.
-- The HTTPS upload protocol only needs the authoritative user_id binding.
ALTER TABLE trajectory_upload_device_codes DROP COLUMN IF EXISTS email_snapshot;
ALTER TABLE trajectory_upload_device_codes DROP COLUMN IF EXISTS role_snapshot;
ALTER TABLE trajectory_upload_sessions DROP COLUMN IF EXISTS email_snapshot;
ALTER TABLE trajectory_upload_sessions DROP COLUMN IF EXISTS role_snapshot;

INSERT INTO conversation_archive_plugin_migrations (name)
VALUES ('003_trajectory_https_upload')
ON CONFLICT (name) DO NOTHING;

COMMIT;
