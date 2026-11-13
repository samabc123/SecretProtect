export const navigation = [
  { path: "/", label: "安全总览", icon: "Odometer", group: "防护工作台" },
  { path: "/attacks", label: "攻击事件", icon: "Warning" },
  { path: "/decisions", label: "封禁决策", icon: "CircleCheck" },
  { path: "/engine", label: "引擎与接入", icon: "Connection" },
  { path: "/monitoring-tasks", label: "凭证检测任务", icon: "List", group: "凭证安全" },
  { path: "/findings", label: "凭证发现项", icon: "Search" },
  { path: "/admission", label: "实验室人员准入", icon: "UserFilled", admin: true, group: "实验室管理" },
  { path: "/administrators", label: "管理员", icon: "UserFilled", superadmin: true },
  { path: "/settings", label: "系统设置", icon: "Setting", group: "系统运维" },
  { path: "/security-audit", label: "防护审计与通知", icon: "Timer" },
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
