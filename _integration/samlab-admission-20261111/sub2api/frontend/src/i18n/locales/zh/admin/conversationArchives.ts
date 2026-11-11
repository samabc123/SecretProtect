export default {
  conversationArchives: {
    title: '归档管理',
    description: '查看脱敏后的最终对话、科研热点与 Codex Agent 逻辑轨迹。',
    empty: '暂无归档数据',
    emptyConversations: '暂无已完成的流式对话',
    emptyTrajectories: '暂无 Agent 轨迹',
    loadFailed: '加载归档数据失败',
    forbidden: '当前账号没有归档管理权限',
    unauthorized: '登录状态已失效，请重新登录',
    userNumber: '用户 #{id}',
    keyNumber: 'API Key #{id}',
    tabs: {
      label: '归档类型',
      conversations: '最终对话',
      trajectories: 'Agent 轨迹'
    },
    filters: {
      userId: '用户编号',
      apiKeyId: 'API Key 编号',
      model: '模型',
      protocol: '协议',
      allProtocols: '全部协议',
      status: '结束状态',
      allStatuses: '全部状态',
      source: '来源',
      toolName: '工具名称',
      startTime: '开始时间',
      endTime: '结束时间'
    },
    insights: {
      title: '科研问题热点',
      description: '每天凌晨 03:00 生成静态快照；页面只读取聚合结果',
      days7: '近 7 天',
      days30: '近 30 天',
      reload: '重新读取',
      processed: '已分析问题',
      eligible: '有效科研问题',
      users: '覆盖用户',
      topics: '热点主题',
      wordCloud: '关键词云',
      topTitle: '热门问题',
      empty: '静态快照尚未生成，请稍后重新读取',
      keywordHint: '{conversations} 次对话 · {users} 名用户',
      conversationCount: '{count} 次'
    },
    columns: {
      completedAt: '完成时间',
      identity: '用户 / API Key',
      model: '模型 / 协议',
      status: '状态',
      duration: '耗时',
      privacyEntities: '脱敏实体',
      source: '来源 / Turn',
      eventCounts: '事件 / 工具'
    },
    detail: {
      title: '最终对话详情',
      requestId: '请求 ID',
      endpoint: '接口',
      expiresAt: '过期时间',
      requestPayload: '脱敏后的完整请求（调试原文）',
      userQuestions: '用户问题',
      noUserQuestions: '请求中没有可识别的用户消息',
      systemContext: '系统 / 开发者上下文',
      otherMessages: '其他输入消息',
      assistantText: '最终可见助手回复',
      toolCalls: '最终工具调用',
      finalResponse: '归并后的最终结果'
    },
    trajectory: {
      detailTitle: 'Agent 轨迹详情',
      finalAnswer: '最终回答',
      timeline: '事件时间线',
      eventProgress: '已显示 {shown} / {total} 条',
      truncated: '已截断',
      redactedContent: '查看脱敏内容',
      noEvents: '该轨迹没有可显示的事件',
      loadMore: '加载更多事件',
      completedAt: '结束时间',
      events: '事件数量',
      toolCalls: '工具调用'
    }
  }
}
