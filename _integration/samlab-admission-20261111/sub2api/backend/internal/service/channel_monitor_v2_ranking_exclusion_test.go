package service

import (
	"context"
	"testing"

	"github.com/stretchr/testify/require"
)

type rankingExclusionRepoStub struct {
	ids []int64
	err error
}

func (s *rankingExclusionRepoStub) ListExcludedUserIDs(context.Context) ([]int64, error) {
	return s.ids, s.err
}

func TestChannelMonitorV2UsersExcludesRankingUsersAndDoesNotAppendExcludedViewer(t *testing.T) {
	excludedID, eligibleID := int64(53), int64(7)
	repo := &channelMonitorV2RepoStub{
		config: ChannelMonitorV2Config{Enabled: true},
		users: &ChannelMonitorV2List[ChannelMonitorV2UserRow]{Items: []ChannelMonitorV2UserRow{
			{UserID: &excludedID, DisplayLabel: "excluded", Metrics: ChannelMonitorV2Metric{RequestCount: 20}},
			{UserID: &eligibleID, DisplayLabel: "eligible", Metrics: ChannelMonitorV2Metric{RequestCount: 10}},
		}},
	}
	svc := NewChannelMonitorV2Service(repo)
	svc.SetRankingExclusionRepository(&rankingExclusionRepoStub{ids: []int64{1, 6, 53}})

	result, err := svc.Users(context.Background(), ChannelMonitorV2Filter{}, excludedID, true)

	require.NoError(t, err)
	require.Len(t, result.Items, 1)
	require.Equal(t, eligibleID, *result.Items[0].UserID)
	require.Equal(t, 1, result.Items[0].Rank)
}

func TestChannelMonitorV2UsersPropagatesRankingExclusionReadFailure(t *testing.T) {
	repo := &channelMonitorV2RepoStub{
		config: ChannelMonitorV2Config{Enabled: true},
		users:  &ChannelMonitorV2List[ChannelMonitorV2UserRow]{},
	}
	svc := NewChannelMonitorV2Service(repo)
	svc.SetRankingExclusionRepository(&rankingExclusionRepoStub{err: context.DeadlineExceeded})

	result, err := svc.Users(context.Background(), ChannelMonitorV2Filter{}, 7, true)

	require.Error(t, err)
	require.Nil(t, result)
}
