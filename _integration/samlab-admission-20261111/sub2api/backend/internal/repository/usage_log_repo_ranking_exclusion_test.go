//go:build unit

package repository

import (
	"context"
	"testing"
	"time"

	"github.com/DATA-DOG/go-sqlmock"
	"github.com/stretchr/testify/require"
)

func TestUsageLogRepositoryGetUserUsageTrendFiltersBeforeTopN(t *testing.T) {
	db, mock := newSQLMock(t)
	repo := &usageLogRepository{sql: db}
	start := time.Date(2025, 1, 1, 0, 0, 0, 0, time.UTC)
	end := start.Add(24 * time.Hour)

	mock.ExpectQuery("(?s)WITH top_users AS \\(.+ranking_excluded_users").
		WithArgs(start, end, 1, start, end).
		WillReturnRows(sqlmock.NewRows([]string{"date", "user_id", "email", "username", "requests", "tokens", "cost", "actual_cost"}).
			AddRow("2025-01-01", int64(7), "user@example.com", "user", int64(2), int64(20), 1.5, 1.25))

	trend, err := repo.GetUserUsageTrend(context.Background(), start, end, "day", 1, "tokens")
	require.NoError(t, err)
	require.Len(t, trend, 1)
	require.Equal(t, int64(7), trend[0].UserID)
	require.NoError(t, mock.ExpectationsWereMet())
}
