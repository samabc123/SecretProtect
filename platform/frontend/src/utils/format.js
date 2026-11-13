export function formatTimestamp(value, fallback = "-", options = {}) {
  if (!value) return fallback;
  const normalized = String(value).includes("T")
    ? String(value)
    : String(value).replace(" ", "T");
  const hasZone = /(?:Z|[+-]\d{2}:\d{2})$/.test(normalized);
  const date = new Date(hasZone ? normalized : `${normalized}Z`);
  if (Number.isNaN(date.getTime())) return fallback;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    ...(options.milliseconds ? { fractionalSecondDigits: 3 } : {}),
    hour12: false,
  }).format(date);
}

const timeSourceLabels = {
  not_provided: "来源未提供",
  connector_not_provided: "来源未提供",
  npm_registry_version_time: "npm 版本发布时间",
  stackoverflow_creation_date: "来源页面时间",
  stackoverflow_answer_creation_date: "来源内容时间",
  rss_published: "RSS/Atom 发布时间",
  sitemap_lastmod: "Sitemap 最后修改时间（仅参考）",
  github_commit: "GitHub 匹配版本提交时间",
  gitee_commit: "Gitee 匹配版本提交时间",
  pypi_upload_time: "PyPI 分发文件上传时间（仅参考）",
  weibo_page_metadata: "微博页面元数据时间（仅参考）",
  image_config_created: "镜像配置创建时间（仅参考）",
  filesystem_mtime: "本地文件修改时间（仅参考）",
};

const timeEvidenceReasonLabels = {
  historical_first_discovery_time_unverified: "历史记录缺少首次发现过程证据",
  first_discovery_time_not_saved: "历史记录未保存系统首次发现时间",
  public_time_is_reference_only: "来源时间只能作为内容参考",
  public_content_time_unverified: "来源时间语义无法核验",
  public_time_not_provided: "来源未提供公开时间",
  connector_not_provided: "来源未提供公开时间",
  no_occurrence_evidence: "没有匹配到同一出现位置的时间证据",
  commit_metadata_not_provided: "来源未返回匹配版本的提交时间",
  public_appearance_not_proven: "仅有版本时间，无法证明凭据首次公开",
  source_revision_time_not_provided: "来源未提供内容修订时间",
  observation_time_not_provided: "历史记录未保存首次观测时间",
  acquisition_time_not_provided: "来源未提供采集完成时间",
  detection_time_not_provided: "历史记录未保存规则检测时间",
  detection_time_evidence_unverified: "检测完成时间缺少过程证据",
  timestamp_invalid_or_timezone_missing: "时间格式或时区无法核验",
  discovery_before_public_time: "首次发现早于公开时间，证据顺序异常",
  detection_before_reference_time: "检测时间早于参考时间，证据顺序异常",
};

export function displayTimeSource(value) {
  const raw = String(value ?? "").trim();
  return timeSourceLabels[raw] || raw || "来源未提供";
}

export function displayTimeEvidenceReason(value) {
  const raw = String(value ?? "").trim();
  return timeEvidenceReasonLabels[raw] || raw || "时效计算依据不足";
}

export function displayPublicEvidenceStatus(row = {}) {
  const status = String(row.public_time_status ?? "").trim();
  if (status === "known") return "有公开证据";
  if (status === "reference_only") {
    return "仅有参考时间";
  }
  return `无法确定：${displayTimeEvidenceReason(
    row.public_time_unknown_reason || "public_time_not_provided",
  )}`;
}

export function formatFirstDiscovery(row, fallback = "未记录", options = {}) {
  if (row?.first_discovery_time_status === "unknown") {
    return `未知：${displayTimeEvidenceReason(row.first_discovery_unknown_reason)}`;
  }
  return formatTimestamp(row?.first_discovered_at, fallback, options);
}

export function formatDurationSeconds(value, options = {}) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return "不可计算：时长数据缺失";
  if (seconds < 0) return "不可计算：首次发现早于公开时间";
  if (seconds < 1 && options.precision === "subsecond") {
    return `${Math.round(seconds * 1000)}毫秒`;
  }
  const roundedSeconds = Math.max(0, Math.round(seconds));
  if (roundedSeconds < 60) return `${roundedSeconds}秒`;
  const minutes = Math.floor(roundedSeconds / 60);
  const remainingSeconds = roundedSeconds % 60;
  if (minutes < 60) {
    return remainingSeconds
      ? `${minutes}分${remainingSeconds}秒`
      : `${minutes}分`;
  }
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  if (hours < 24) {
    return remainingMinutes
      ? `${hours}小时${remainingMinutes}分`
      : `${hours}小时`;
  }
  const days = Math.floor(hours / 24);
  const remainingHours = hours % 24;
  if (days < 365) {
    return remainingHours
      ? `${days}天${remainingHours}小时`
      : `${days}天`;
  }
  const years = Math.floor(days / 365);
  const remainingDays = days % 365;
  return remainingDays
    ? `${years}年${remainingDays}天`
    : `${years}年`;
}

export function formatDiscoveryLatency(row = {}, options = {}) {
  const concise = Boolean(options.concise);
  if (row.discovery_latency_status !== "calculated") {
    if (concise) return "不可计算";
    return `不可计算：${displayTimeEvidenceReason(
      row.discovery_latency_reason
        || row.public_time_unknown_reason
        || "public_time_not_provided",
    )}`;
  }
  const duration = formatDurationSeconds(row.discovery_latency_seconds, {
    precision: row.time_provenance?.detected_at?.precision
      || row.time_provenance?.first_discovered_at?.precision,
  });
  if (concise) return duration;
  if (row.discovery_latency_calculation_type === "reference_to_detection") {
    const basis = row.discovery_latency_basis
      || `按${displayTimeSource(row.published_source)}计算`;
    return `内容时间至检出间隔：${duration}（${basis}）`;
  }
  if (row.discovery_latency_calculation_type === "public_to_first_discovery") {
    return `公开至首次发现：${duration}`;
  }
  return duration;
}

function normalizeState(value) {
  return String(value ?? "").trim().toLowerCase().replace(/[_-]+/g, " ");
}

const stateLabels = {
  critical: "严重",
  high: "高风险",
  medium: "中风险",
  low: "低风险",
  info: "信息",
  unknown: "未知",
  pending: "待处理",
  queued: "排队中",
  running: "运行中",
  done: "已完成",
  executed: "已执行",
  completed: "已完成",
  succeeded: "成功",
  success: "成功",
  passed: "已通过",
  failed: "失败",
  error: "错误",
  valid: "有效",
  "controlled valid": "受控样例成功",
  invalid: "无效",
  inconclusive: "无法判断",
  "not configured": "未配置授权端点",
  "missing context": "缺少配对凭据",
  unsupported: "不支持该类型",
  confirmed: "已确认",
  "false positive": "误报",
  ignored: "已忽略",
  "not requested": "未请求",
  "not verified": "未验证",
  "not tested": "未实测",
  enabled: "已启用",
  disabled: "已禁用",
  paused: "已暂停",
  active: "运行中",
  rejected: "已拒绝",
  "offline recorded": "已记录",
  "not created": "未创建",
  "not implemented": "未实现",
  "implemented and runnable": "已实现可运行",
  "not implemented or tested": "未实现未实测",
  "controlled environment confirmation": "受控环境确认",
};

export function displayState(value, kind = "") {
  const raw = String(value ?? "").trim();
  if (!raw) return "-";
  const normalized = normalizeState(raw);
  if (kind === "risk") {
    return {
      critical: "严重",
      high: "高风险",
      medium: "中风险",
      low: "低风险",
    }[normalized] || stateLabels[normalized] || raw;
  }
  if (kind === "review" && normalized === "pending") return "待复核";
  if (kind === "verification" && normalized === "not requested") return "未执行身份验证";
  return stateLabels[normalized] || raw;
}

export function stateType(value, kind = "") {
  const normalized = normalizeState(value);
  if (
    kind === "risk"
    || ["critical", "high", "medium", "low", "严重", "高风险", "中风险", "低风险"].includes(normalized)
  ) {
    return ["critical", "严重"].includes(normalized) ? "state-risk-severe"
      : ["high", "高风险"].includes(normalized) ? "state-risk-high"
        : ["low", "低风险"].includes(normalized) ? "state-risk-low"
          : "state-risk-medium";
  }
  if ([
    "done", "executed", "completed", "succeeded", "valid", "confirmed", "enabled", "passed",
    "success", "已实现", "已实测可运行", "已验证", "已确认", "有效", "启用", "已启用",
    "成功", "controlled valid", "已确认公开泄露", "受控环境确认", "受控样例成功", "active",
  ].includes(normalized) || ["已完成", "成功", "有效", "已确认", "已启用", "已验证", "已记录", "已实现可运行"].includes(normalized)) return "success";
  if ([
    "failed", "invalid", "error", "false", "danger", "误报", "拒绝", "rejected",
  ].some((item) => normalized.includes(item)) || ["失败", "无效", "错误", "拒绝"].includes(normalized)) return "danger";
  if (["pending", "queued", "running", "not requested", "not configured", "missing context", "unsupported", "inconclusive", "unknown"].some((item) => normalized.includes(item))
    || ["待处理", "待复核", "排队中", "运行中", "未请求", "未执行身份验证", "未验证", "未配置授权端点", "未实测", "无法判断", "未知", "不支持该类型"].includes(normalized)) {
    return "info";
  }
  return "info";
}

export function statusType(value) {
  const type = stateType(value);
  return type.startsWith("state-risk-") ? "info" : type;
}

export function stateClass(value, kind = "") {
  const type = stateType(value, kind);
  return type.startsWith("state-risk-") ? type : "";
}

export function downloadJson(filename, payload) {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}
