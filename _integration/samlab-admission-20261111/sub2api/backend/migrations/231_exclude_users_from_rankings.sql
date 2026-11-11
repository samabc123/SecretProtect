-- Keep ranking exclusions durable across application image upgrades.
CREATE TABLE IF NOT EXISTS ranking_excluded_users (
    user_id    BIGINT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO ranking_excluded_users (user_id)
VALUES (1), (6), (53)
ON CONFLICT (user_id) DO NOTHING;
