export const navigation = [
  { path: "/", label: "总览", icon: "Odometer", group: "安全监测" },
  { path: "/monitoring-tasks", label: "监控任务", icon: "List" },
  { path: "/events", label: "事件列表", icon: "Warning" },
  { path: "/findings", label: "统一发现项", icon: "Search" },
  { path: "/verification", label: "验证中心", icon: "CircleCheck" },
  { path: "/channels", label: "渠道与规则", icon: "Connection" },
  { path: "/reports", label: "指标报告", icon: "DataAnalysis" },
  { path: "/admission", label: "实验室人员准入", icon: "UserFilled", admin: true, group: "实验室管理" },
  { path: "/administrators", label: "管理员", icon: "UserFilled", superadmin: true },
  { path: "/settings", label: "系统设置", icon: "Setting", group: "系统运维" },
  { path: "/connector-runs", label: "运行审计", icon: "Timer" },
];

export const settingsTabs = [
  ["account", "个人账号", "账号与会话"],
  ["general", "基础设置", "数据源与运行状态"],
  ["connections", "平台授权", "代码托管与采集连接"],
  ["sources", "来源任务", "创建与运行来源"],
  ["budget", "采集预算", "请求与文件上限"],
  ["verification", "凭据验证", "授权验证边界"],
  ["audit", "安全审计", "操作与决策记录"],
];

export const sourceHints = {
  local_dir: '{"path":"C:/authorized/project-copy"}',
  controlled_fixtures: '{"path":"fixtures/controlled"}',
  authorized_jsonl: '{"path":"fixtures/authorized"}',
  github: '{"repositories":["owner/repository"],"branch":"main"}',
  gitee: '{"repositories":["owner/repository"]}',
  mediawiki: '{"api_url":"https://wiki.example.org/w/api.php","max_items":20}',
  generic_web: '{"url":"https://example.org/page","max_items":1}',
  stackoverflow: '{"queries":["AWS_ACCESS_KEY_ID","OPENAI_API_KEY","DATABASE_URL"],"site":"stackoverflow","max_items":10,"include_answers":true}',
  weibo_authorized_api: '{"uid":"7845697819","post_id":"5355671562486378","include_long_text":true,"max_items":1}',
  npm_registry: '{"package":"package-name","version":"1.0.0"}',
  pypi_registry: '{"package":"package-name","version":"1.0.0"}',
  huggingface_repo: '{"repository":"owner/model-or-dataset","revision":"main"}',
  crates_registry: '{"package":"package-name","version":"1.0.0"}',
  nuget_registry: '{"package":"package-name","version":"1.0.0"}',
  rubygems_registry: '{"package":"package-name","version":"1.0.0"}',
};
