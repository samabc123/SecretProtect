package service

import "context"

// RankingExclusionRepository provides the durable user IDs that must not take
// part in user-oriented rankings. It is intentionally read-only here; the
// initial policy is maintained by a database migration.
type RankingExclusionRepository interface {
	ListExcludedUserIDs(ctx context.Context) ([]int64, error)
}
