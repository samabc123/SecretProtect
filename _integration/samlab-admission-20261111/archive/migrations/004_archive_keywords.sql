BEGIN;

-- 每条归档只处理一次。即使没有可展示关键词，也写入 documents 作为处理状态，
-- 避免后台任务反复解析无效、过短或内部生成请求。
CREATE TABLE IF NOT EXISTS conversation_archive_keyword_documents (
    archive_id BIGINT PRIMARY KEY REFERENCES conversation_archives(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    completed_at TIMESTAMPTZ NOT NULL,
    eligible BOOLEAN NOT NULL DEFAULT FALSE,
    exclusion_reason VARCHAR(64) NOT NULL DEFAULT '',
    question_chars INT NOT NULL DEFAULT 0 CHECK (question_chars >= 0),
    keyword_count INT NOT NULL DEFAULT 0 CHECK (keyword_count >= 0),
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_archive_keyword_documents_completed
    ON conversation_archive_keyword_documents (completed_at DESC, archive_id DESC);
CREATE INDEX IF NOT EXISTS idx_archive_keyword_documents_user_completed
    ON conversation_archive_keyword_documents (user_id, completed_at DESC);

CREATE TABLE IF NOT EXISTS conversation_archive_keywords (
    archive_id BIGINT NOT NULL REFERENCES conversation_archive_keyword_documents(archive_id) ON DELETE CASCADE,
    keyword VARCHAR(80) NOT NULL,
    category VARCHAR(24) NOT NULL DEFAULT 'general',
    occurrences SMALLINT NOT NULL DEFAULT 1 CHECK (occurrences BETWEEN 1 AND 1000),
    PRIMARY KEY (archive_id, keyword)
);

CREATE INDEX IF NOT EXISTS idx_archive_keywords_keyword
    ON conversation_archive_keywords (keyword, archive_id);
CREATE INDEX IF NOT EXISTS idx_archive_keywords_category
    ON conversation_archive_keywords (category, archive_id);

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'conversation_archiver') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON conversation_archive_keyword_documents, conversation_archive_keywords
            TO conversation_archiver;
    END IF;
END $$;

INSERT INTO conversation_archive_plugin_migrations(name)
VALUES ('004_archive_keywords')
ON CONFLICT(name) DO NOTHING;

COMMIT;
