export default {
  conversationArchives: {
    title: 'Archive Management',
    description: 'Review redacted final conversations, research trends, and Codex agent trajectories.',
    empty: 'No archive data',
    emptyConversations: 'No completed streaming conversations',
    emptyTrajectories: 'No agent trajectories',
    loadFailed: 'Failed to load archive data',
    forbidden: 'This account does not have archive management permission',
    unauthorized: 'Your session has expired. Please sign in again.',
    userNumber: 'User #{id}',
    keyNumber: 'API Key #{id}',
    tabs: {
      label: 'Archive type',
      conversations: 'Final conversations',
      trajectories: 'Agent trajectories'
    },
    filters: {
      userId: 'User ID',
      apiKeyId: 'API Key ID',
      model: 'Model',
      protocol: 'Protocol',
      allProtocols: 'All protocols',
      status: 'Terminal status',
      allStatuses: 'All statuses',
      source: 'Source',
      toolName: 'Tool name',
      startTime: 'Start time',
      endTime: 'End time'
    },
    insights: {
      title: 'Research Question Trends',
      description: 'A static snapshot is generated daily at 03:00; this page only reads aggregated results.',
      days7: 'Last 7 days',
      days30: 'Last 30 days',
      reload: 'Reload',
      processed: 'Questions analyzed',
      eligible: 'Eligible research questions',
      users: 'Users covered',
      topics: 'Trending topics',
      wordCloud: 'Keyword cloud',
      topTitle: 'Top questions',
      empty: 'The static snapshot has not been generated yet. Reload again shortly.',
      keywordHint: '{conversations} conversations · {users} users',
      conversationCount: '{count}'
    },
    columns: {
      completedAt: 'Completed',
      identity: 'User / API Key',
      model: 'Model / Protocol',
      status: 'Status',
      duration: 'Duration',
      privacyEntities: 'Redacted entities',
      source: 'Source / Turn',
      eventCounts: 'Events / Tools'
    },
    detail: {
      title: 'Final Conversation Detail',
      requestId: 'Request ID',
      endpoint: 'Endpoint',
      expiresAt: 'Expires',
      requestPayload: 'Complete redacted request (debug view)',
      userQuestions: 'User questions',
      noUserQuestions: 'No recognizable user message was found in the request',
      systemContext: 'System / developer context',
      otherMessages: 'Other input messages',
      assistantText: 'Final visible assistant response',
      toolCalls: 'Final tool calls',
      finalResponse: 'Merged final result'
    },
    trajectory: {
      detailTitle: 'Agent Trajectory Detail',
      finalAnswer: 'Final answer',
      timeline: 'Event timeline',
      eventProgress: 'Showing {shown} of {total}',
      truncated: 'Truncated',
      redactedContent: 'View redacted content',
      noEvents: 'This trajectory has no displayable events',
      loadMore: 'Load more events',
      completedAt: 'Terminal time',
      events: 'Events',
      toolCalls: 'Tool calls'
    }
  }
}
