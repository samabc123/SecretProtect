package migrations

import (
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestRankingExclusionMigrationIsIdempotentAndSeedsStableIDs(t *testing.T) {
	content, err := FS.ReadFile("231_exclude_users_from_rankings.sql")
	require.NoError(t, err)

	sql := strings.Join(strings.Fields(string(content)), " ")
	require.Contains(t, sql, "CREATE TABLE IF NOT EXISTS ranking_excluded_users")
	require.Contains(t, sql, "PRIMARY KEY")
	require.Contains(t, sql, "VALUES (1), (6), (53)")
	require.Contains(t, sql, "ON CONFLICT (user_id) DO NOTHING")
}
