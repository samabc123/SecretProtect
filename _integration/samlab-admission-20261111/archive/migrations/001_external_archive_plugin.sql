BEGIN;

CREATE TABLE IF NOT EXISTS conversation_archive_plugin_migrations (
    name TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS conversation_archives (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    api_key_id BIGINT REFERENCES api_keys(id) ON DELETE SET NULL,
    client_request_id UUID,
    request_id VARCHAR(128) NOT NULL,
    protocol VARCHAR(64) NOT NULL,
    endpoint VARCHAR(512) NOT NULL,
    model VARCHAR(255) NOT NULL DEFAULT '',
    request_payload JSONB NOT NULL,
    assistant_text TEXT NOT NULL DEFAULT '',
    tool_calls JSONB NOT NULL DEFAULT '[]'::jsonb,
    final_response JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL,
    duration_ms BIGINT NOT NULL CHECK (duration_ms >= 0),
    response_status INT NOT NULL,
    privacy_entities INT NOT NULL DEFAULT 0 CHECK (privacy_entities >= 0),
    expires_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT conversation_archives_request_id_unique UNIQUE (request_id)
);

ALTER TABLE conversation_archives
    ADD COLUMN IF NOT EXISTS client_request_id UUID;

CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_archives_client_request_id
    ON conversation_archives (client_request_id)
    WHERE client_request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_conversation_archives_user_completed
    ON conversation_archives (user_id, completed_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_archives_expires_at
    ON conversation_archives (expires_at);
CREATE INDEX IF NOT EXISTS idx_conversation_archives_protocol_completed
    ON conversation_archives (protocol, completed_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_archives_model_completed
    ON conversation_archives (model, completed_at DESC);

CREATE TABLE IF NOT EXISTS conversation_archive_pending (
    id BIGSERIAL PRIMARY KEY,
    client_request_id UUID NOT NULL,
    request_id VARCHAR(128) NOT NULL,
    protocol VARCHAR(64) NOT NULL,
    endpoint VARCHAR(512) NOT NULL,
    model VARCHAR(255) NOT NULL DEFAULT '',
    request_payload JSONB NOT NULL,
    assistant_text TEXT NOT NULL DEFAULT '',
    tool_calls JSONB NOT NULL DEFAULT '[]'::jsonb,
    final_response JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL,
    duration_ms BIGINT NOT NULL CHECK (duration_ms >= 0),
    response_status INT NOT NULL,
    privacy_entities INT NOT NULL DEFAULT 0 CHECK (privacy_entities >= 0),
    expires_at TIMESTAMPTZ NOT NULL,
    attempt_count INT NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deadline_at TIMESTAMPTZ NOT NULL,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT conversation_archive_pending_client_request_id_unique UNIQUE (client_request_id),
    CONSTRAINT conversation_archive_pending_request_id_unique UNIQUE (request_id)
);

CREATE INDEX IF NOT EXISTS idx_conversation_archive_pending_due
    ON conversation_archive_pending (next_attempt_at, id);

CREATE INDEX IF NOT EXISTS idx_usage_logs_archive_identity
    ON usage_logs (request_id, created_at)
    WHERE request_id IS NOT NULL AND stream = TRUE;

CREATE OR REPLACE FUNCTION resolve_archive_identity(
    p_client_request_id UUID,
    p_started_at TIMESTAMPTZ,
    p_completed_at TIMESTAMPTZ
)
RETURNS TABLE(user_id BIGINT, api_key_id BIGINT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    identity_count INT;
BEGIN
    SELECT COUNT(*)
    INTO identity_count
    FROM (
        SELECT DISTINCT ul.user_id, ul.api_key_id
        FROM public.usage_logs AS ul
        WHERE ul.request_id = 'client:' || p_client_request_id::TEXT
          AND ul.stream = TRUE
          AND ul.created_at >= p_started_at - INTERVAL '5 minutes'
          AND ul.created_at <= p_completed_at + INTERVAL '5 minutes'
    ) AS identities;

    IF identity_count = 0 THEN
        RETURN;
    END IF;
    IF identity_count > 1 THEN
        RAISE EXCEPTION 'ambiguous archive identity for client request %', p_client_request_id
            USING ERRCODE = 'P0001', DETAIL = 'archive_identity_ambiguous';
    END IF;

    RETURN QUERY
    SELECT DISTINCT ul.user_id, ul.api_key_id
    FROM public.usage_logs AS ul
    WHERE ul.request_id = 'client:' || p_client_request_id::TEXT
      AND ul.stream = TRUE
      AND ul.created_at >= p_started_at - INTERVAL '5 minutes'
      AND ul.created_at <= p_completed_at + INTERVAL '5 minutes';
END;
$$;

REVOKE ALL ON FUNCTION resolve_archive_identity(UUID, TIMESTAMPTZ, TIMESTAMPTZ)
    FROM PUBLIC;

INSERT INTO conversation_archive_plugin_migrations (name)
VALUES ('001_external_archive_plugin')
ON CONFLICT (name) DO NOTHING;

COMMIT;
