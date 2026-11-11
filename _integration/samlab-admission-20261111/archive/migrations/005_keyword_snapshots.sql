BEGIN;

-- 词云只读取定期生成的聚合快照，不再为每条对话写关键词派生记录。
CREATE TABLE IF NOT EXISTS conversation_archive_keyword_snapshots (
    days INT PRIMARY KEY CHECK (days IN (7, 30)),
    generated_at TIMESTAMPTZ NOT NULL,
    processed_documents BIGINT NOT NULL DEFAULT 0 CHECK (processed_documents >= 0),
    eligible_documents BIGINT NOT NULL DEFAULT 0 CHECK (eligible_documents >= 0),
    covered_users BIGINT NOT NULL DEFAULT 0 CHECK (covered_users >= 0),
    items JSONB NOT NULL DEFAULT '[]'::jsonb
);

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'conversation_archiver') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON conversation_archive_keyword_snapshots
            TO conversation_archiver;
    END IF;
END $$;

INSERT INTO conversation_archive_plugin_migrations(name)
VALUES ('005_keyword_snapshots')
ON CONFLICT(name) DO NOTHING;

COMMIT;
