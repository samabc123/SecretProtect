package repository

import (
	"context"
	"database/sql"
	"fmt"

	"github.com/Wei-Shaw/sub2api/internal/service"
)

type rankingExclusionRepository struct {
	db sqlExecutor
}

// NewRankingExclusionRepository creates the read-only repository for the
// durable ranking exclusion policy.
func NewRankingExclusionRepository(db *sql.DB) service.RankingExclusionRepository {
	return &rankingExclusionRepository{db: db}
}

func (r *rankingExclusionRepository) ListExcludedUserIDs(ctx context.Context) (ids []int64, err error) {
	if r == nil || r.db == nil {
		return nil, fmt.Errorf("ranking exclusion repository is not configured")
	}
	return listRankingExcludedUserIDs(ctx, r.db)
}

func listRankingExcludedUserIDs(ctx context.Context, db sqlExecutor) (ids []int64, err error) {
	if db == nil {
		return nil, fmt.Errorf("ranking exclusion repository is not configured")
	}

	rows, err := db.QueryContext(ctx, `
		SELECT user_id
		FROM ranking_excluded_users
		ORDER BY user_id ASC
	`)
	if err != nil {
		return nil, fmt.Errorf("list excluded ranking users: %w", err)
	}
	defer func() {
		if closeErr := rows.Close(); closeErr != nil && err == nil {
			err = closeErr
			ids = nil
		}
	}()

	ids = make([]int64, 0)
	for rows.Next() {
		var userID int64
		if err = rows.Scan(&userID); err != nil {
			return nil, err
		}
		ids = append(ids, userID)
	}
	if err = rows.Err(); err != nil {
		return nil, err
	}
	return ids, nil
}
