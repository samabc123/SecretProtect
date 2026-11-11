BEGIN;

CREATE TABLE IF NOT EXISTS agent_trajectory_import_batches (
    id UUID PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    email_snapshot VARCHAR(255) NOT NULL,
    source VARCHAR(64) NOT NULL,
    schema_version VARCHAR(64) NOT NULL,
    manifest_sha256 CHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL CHECK (status IN ('importing', 'completed', 'partial', 'failed')),
    total_records INT NOT NULL DEFAULT 0 CHECK (total_records >= 0),
    inserted_records INT NOT NULL DEFAULT 0 CHECK (inserted_records >= 0),
    updated_records INT NOT NULL DEFAULT 0 CHECK (updated_records >= 0),
    skipped_records INT NOT NULL DEFAULT 0 CHECK (skipped_records >= 0),
    failed_records INT NOT NULL DEFAULT 0 CHECK (failed_records >= 0),
    error_summary TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT agent_trajectory_batches_manifest_unique UNIQUE (user_id, manifest_sha256)
);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'agent_trajectory_import_batches'
          AND column_name = 'username_snapshot'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'agent_trajectory_import_batches'
          AND column_name = 'email_snapshot'
    ) THEN
        ALTER TABLE agent_trajectory_import_batches
            RENAME COLUMN username_snapshot TO email_snapshot;
    END IF;
END;
$$;

CREATE TABLE IF NOT EXISTS agent_trajectories (
    id BIGSERIAL PRIMARY KEY,
    import_batch_id UUID REFERENCES agent_trajectory_import_batches(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source VARCHAR(64) NOT NULL,
    source_thread_id VARCHAR(128) NOT NULL,
    source_turn_id VARCHAR(128) NOT NULL,
    trajectory_id VARCHAR(384) NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    model VARCHAR(255) NOT NULL DEFAULT '',
    model_provider VARCHAR(128) NOT NULL DEFAULT '',
    status VARCHAR(32) NOT NULL CHECK (status IN ('completed', 'failed', 'aborted', 'incomplete')),
    final_text TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL,
    duration_ms BIGINT NOT NULL DEFAULT 0 CHECK (duration_ms >= 0),
    event_count INT NOT NULL DEFAULT 0 CHECK (event_count >= 0),
    tool_call_count INT NOT NULL DEFAULT 0 CHECK (tool_call_count >= 0),
    privacy_entities INT NOT NULL DEFAULT 0 CHECK (privacy_entities >= 0),
    content_sha256 CHAR(64) NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT agent_trajectories_source_unique UNIQUE (user_id, source, source_thread_id, source_turn_id)
);

CREATE TABLE IF NOT EXISTS agent_trajectory_events (
    id BIGSERIAL PRIMARY KEY,
    trajectory_id BIGINT NOT NULL REFERENCES agent_trajectories(id) ON DELETE CASCADE,
    sequence_no INT NOT NULL CHECK (sequence_no > 0),
    source_item_id VARCHAR(255),
    event_type VARCHAR(64) NOT NULL,
    role VARCHAR(32),
    phase VARCHAR(32),
    tool_call_id VARCHAR(255),
    tool_name VARCHAR(255),
    content JSONB NOT NULL DEFAULT 'null'::jsonb,
    status VARCHAR(64),
    event_at TIMESTAMPTZ NOT NULL,
    privacy_entities INT NOT NULL DEFAULT 0 CHECK (privacy_entities >= 0),
    original_bytes BIGINT NOT NULL DEFAULT 0 CHECK (original_bytes >= 0),
    stored_bytes BIGINT NOT NULL DEFAULT 0 CHECK (stored_bytes >= 0),
    is_truncated BOOLEAN NOT NULL DEFAULT FALSE,
    content_sha256 CHAR(64) NOT NULL,
    extra JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT agent_trajectory_events_sequence_unique UNIQUE (trajectory_id, sequence_no)
);

CREATE TABLE IF NOT EXISTS agent_trajectory_labels (
    id BIGSERIAL PRIMARY KEY,
    trajectory_id BIGINT NOT NULL REFERENCES agent_trajectories(id) ON DELETE CASCADE,
    label_source VARCHAR(64) NOT NULL,
    label_type VARCHAR(64) NOT NULL,
    score DOUBLE PRECISION,
    verdict VARCHAR(64),
    reward_components JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE agent_trajectories
    DROP CONSTRAINT IF EXISTS agent_trajectories_import_batch_id_fkey;
ALTER TABLE agent_trajectories
    ADD CONSTRAINT agent_trajectories_import_batch_id_fkey
    FOREIGN KEY (import_batch_id)
    REFERENCES agent_trajectory_import_batches(id)
    ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_agent_trajectories_user_completed
    ON agent_trajectories (user_id, completed_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_agent_trajectories_expires_at
    ON agent_trajectories (expires_at);
CREATE INDEX IF NOT EXISTS idx_agent_trajectories_status_completed
    ON agent_trajectories (status, completed_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_trajectories_model_completed
    ON agent_trajectories (model, completed_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_trajectory_events_timeline
    ON agent_trajectory_events (trajectory_id, sequence_no);
CREATE INDEX IF NOT EXISTS idx_agent_trajectory_events_tool
    ON agent_trajectory_events (tool_name, event_at DESC)
    WHERE tool_name IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_trajectory_labels_trajectory
    ON agent_trajectory_labels (trajectory_id, created_at DESC);

CREATE OR REPLACE FUNCTION resolve_archive_user_by_email(p_email TEXT)
RETURNS TABLE(user_id BIGINT, email VARCHAR(255), role VARCHAR(20))
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    normalized TEXT := LOWER(BTRIM(p_email));
    matches INT;
BEGIN
    IF normalized = '' THEN
        RAISE EXCEPTION 'archive import email is empty'
            USING ERRCODE = 'P0001', DETAIL = 'archive_email_invalid';
    END IF;

    SELECT COUNT(*) INTO matches
    FROM public.users AS u
    WHERE LOWER(BTRIM(u.email)) = normalized
      AND u.status = 'active'
      AND u.deleted_at IS NULL;

    IF matches = 0 THEN
        RETURN;
    END IF;
    IF matches > 1 THEN
        RAISE EXCEPTION 'archive import email is ambiguous: %', normalized
            USING ERRCODE = 'P0001', DETAIL = 'archive_email_ambiguous';
    END IF;

    RETURN QUERY
    SELECT u.id, u.email, u.role
    FROM public.users AS u
    WHERE LOWER(BTRIM(u.email)) = normalized
      AND u.status = 'active'
      AND u.deleted_at IS NULL;
END;
$$;

REVOKE ALL ON FUNCTION resolve_archive_user_by_email(TEXT) FROM PUBLIC;
DROP FUNCTION IF EXISTS resolve_archive_user_by_username(TEXT);

INSERT INTO conversation_archive_plugin_migrations (name)
VALUES ('002_agent_trajectories')
ON CONFLICT (name) DO NOTHING;

COMMIT;
