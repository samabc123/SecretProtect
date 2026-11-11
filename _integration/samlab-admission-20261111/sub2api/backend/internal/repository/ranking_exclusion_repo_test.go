//go:build unit

package repository

import (
	"context"
	"regexp"
	"testing"

	"github.com/DATA-DOG/go-sqlmock"
	"github.com/stretchr/testify/require"
)

func TestRankingExclusionRepositoryListsDurableUserIDs(t *testing.T) {
	db, mock := newSQLMock(t)
	repo := NewRankingExclusionRepository(db)

	mock.ExpectQuery(regexp.QuoteMeta("SELECT user_id FROM ranking_excluded_users ORDER BY user_id ASC")).
		WillReturnRows(sqlmock.NewRows([]string{"user_id"}).AddRow(1).AddRow(6).AddRow(53))

	ids, err := repo.ListExcludedUserIDs(context.Background())
	require.NoError(t, err)
	require.Equal(t, []int64{1, 6, 53}, ids)
	require.NoError(t, mock.ExpectationsWereMet())
}

func TestRankingExclusionRepositoryFailsClosedWhenTableCannotBeRead(t *testing.T) {
	db, mock := newSQLMock(t)
	repo := NewRankingExclusionRepository(db)

	mock.ExpectQuery("SELECT user_id FROM ranking_excluded_users").WillReturnError(context.DeadlineExceeded)

	ids, err := repo.ListExcludedUserIDs(context.Background())
	require.Error(t, err)
	require.Nil(t, ids)
	require.NoError(t, mock.ExpectationsWereMet())
}
