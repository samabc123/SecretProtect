BEGIN;

-- Static snapshots replaced the per-conversation keyword index. Keep this
-- migration after 004 so replaying the complete migration set cannot recreate
-- the retired derived tables.
DROP TABLE IF EXISTS conversation_archive_keywords;
DROP TABLE IF EXISTS conversation_archive_keyword_documents;

INSERT INTO conversation_archive_plugin_migrations(name)
VALUES ('006_drop_legacy_keyword_indexes')
ON CONFLICT(name) DO NOTHING;

COMMIT;
