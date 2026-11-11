async function apiFetch(url, options = {}) {
  const headers = new Headers(options.headers);
  const method = (options.method || "GET").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    headers.set("X-CSRF-Token", document.querySelector('meta[name="csrf-token"]')?.content || "");
  }
  const response = await fetch(url, {...options, credentials: "same-origin", headers});
  if (response.status === 401) {
    window.location.assign("/login?next=" + encodeURIComponent(window.location.pathname + window.location.search));
    throw new Error("登录已失效，请重新登录。");
  }
  return response;
}

function activateSettingsTab(key, focus = false) {
  const tabs = [...document.querySelectorAll("[data-settings-tab]")];
  if (!tabs.some(tab => tab.dataset.settingsTab === key)) key = "account";
  tabs.forEach(tab => {
    const active = tab.dataset.settingsTab === key;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    if (active && focus) tab.focus();
  });
  document.querySelectorAll("[data-settings-panel]").forEach(panel => {
    panel.hidden = panel.dataset.settingsPanel !== key;
  });
}

async function changePassword(form) {
  const body = Object.fromEntries(new FormData(form));
  const result = form.querySelector("[data-password-result]");
  const response = await apiFetch("/api/auth/password", {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : "密码输入不符合要求。");
  document.querySelector('meta[name="csrf-token"]').content = payload.csrf_token;
  form.reset();
  document.querySelector("[data-password-warning]")?.remove();
  result.textContent = "密码已更新，其他已登录会话已失效。";
}

function showNotImplemented(action) {
  const box = document.querySelector("#action-notice");
  if (!box) return;
  box.textContent = `${action}：功能未实现。该操作尚未接入后端，不会执行真实操作。`;
  box.hidden = false;
  window.setTimeout(() => { box.hidden = true; }, 5000);
}

function showMessage(message) {
  const box = document.querySelector("#action-notice");
  if (!box) return;
  box.textContent = message;
  box.hidden = false;
}

function settingElement(key) {
  return document.querySelector(`[data-setting="${key}"]`);
}

function formatRuntimeValue(value) {
  if (typeof value === "boolean") return value ? "开启" : "关闭";
  return value ?? "-";
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[char]));
}

function formatTimestamp(value, unknown = "-") {
  if (!value) return unknown;
  const raw = String(value);
  const normalized = raw.includes("T") ? raw : raw.replace(" ", "T");
  const hasZone = /(?:Z|[+-]\d{2}:\d{2})$/.test(normalized);
  const date = new Date(hasZone ? normalized : `${normalized}Z`);
  if (Number.isNaN(date.getTime())) return unknown;
  return `${new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date)}（北京时间）`;
}

async function loadSettings() {
  const response = await apiFetch("/api/settings");
  const payload = await response.json();
  if (!response.ok || payload.error) throw new Error(payload.error || "设置读取失败");
  window.__settingsRuntime = payload.runtime;
  window.__sourceCatalog = payload.source_catalog || [];
  for (const item of payload.items) {
    const element = settingElement(item.key);
    if (!element) continue;
    if (item.sensitive) {
      element.value = "";
      const status = document.querySelector(`[data-setting-status="${item.key}"]`);
      if (status) status.textContent = item.configured ? "已配置" : "未配置";
      continue;
    }
    if (item.key === "GITHUB_REPOSITORIES" || item.key === "GITEE_REPOSITORIES" || item.key === "VERIFICATION_ALLOWED_FINGERPRINTS") {
      element.value = Array.isArray(item.value) ? item.value.join("\n") : "";
    } else if (element.type === "checkbox") {
      element.checked = Boolean(item.value);
    } else if (element.tagName === "SELECT" && item.kind === "boolean") {
      element.value = item.value ? "true" : "false";
    } else {
      element.value = item.value ?? "";
    }
  }
  document.querySelectorAll("[data-runtime]").forEach((element) => {
    const key = element.dataset.runtime;
    const value = payload.runtime[key];
    element.textContent = key.endsWith("_ready") ? (value ? "就绪" : "未就绪") : formatRuntimeValue(value);
  });
  const collector = document.querySelector("#source-collector");
  const hint = document.querySelector("[data-source-config-hint]");
  if (collector && hint) {
    const item = (payload.source_catalog || []).find((entry) => entry.key === collector.value);
    hint.textContent = item ? `最小示例：${item.config_hint}` : "请填写 JSON 对象。";
  }
}

async function loadUnifiedSources() {
  const list = document.querySelector("[data-source-list]");
  if (!list) return;
  const response = await apiFetch("/api/sources");
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "统一后端来源读取失败");
  if (!payload.length) {
    list.innerHTML = '<tr><td colspan="6" class="muted">暂无来源；请先保存一个来源配置。</td></tr>';
    return;
  }
  list.innerHTML = payload.map((source) => {
    const task = source.task || {};
    const taskStatus = task.status || "未创建";
    const enabled = source.enabled ? "启用" : "禁用";
    const config = JSON.stringify(source.config || {});
    return `<tr>
      <td><strong>${escapeHtml(source.name)}</strong><div class="muted small">#${escapeHtml(source.id)}</div></td>
      <td>${escapeHtml(source.platform)}<div class="muted small">${escapeHtml(source.collector_key)}</div></td>
      <td><code>${escapeHtml(config)}</code></td>
      <td><span class="badge badge-${source.enabled ? "low" : "medium"}">${enabled}</span></td>
      <td>${escapeHtml(taskStatus)}<div class="muted small">${escapeHtml(task.mode || "-")}</div></td>
      <td class="button-row">
        <button data-source-action="run" data-source-id="${escapeHtml(source.id)}">运行一次</button>
        <button data-source-action="toggle" data-source-id="${escapeHtml(source.id)}">${source.enabled ? "禁用" : "启用"}</button>
        <button data-source-action="delete" data-source-id="${escapeHtml(source.id)}">删除</button>
      </td>
    </tr>`;
  }).join("");
  list.querySelectorAll("[data-source-action]").forEach((button) => {
    button.addEventListener("click", () => sourceAction(button).catch((error) => showMessage(error.message)));
  });
}

async function createUnifiedSource(form) {
  let config;
  try {
    config = JSON.parse(form.elements.config.value);
  } catch {
    throw new Error("连接器配置必须是合法 JSON 对象；未写入。");
  }
  if (!config || Array.isArray(config) || typeof config !== "object") {
    throw new Error("连接器配置必须是 JSON 对象；未写入。");
  }
  const payload = {
    name: form.elements.name.value.trim(),
    collector_key: form.elements.collector_key.value,
    config,
    mode: form.elements.mode.value,
    interval_sec: Number(form.elements.interval_sec.value),
    enabled: form.elements.enabled.checked,
  };
  const response = await apiFetch("/api/sources", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || "统一后端来源保存失败");
  form.reset();
  form.elements.interval_sec.value = "300";
  form.elements.enabled.checked = true;
  showMessage("统一后端来源已写入 SQLite；尚未自动运行。");
  await loadUnifiedSources();
}

async function sourceAction(button) {
  const id = encodeURIComponent(button.dataset.sourceId);
  const action = button.dataset.sourceAction;
  let response;
  if (action === "run") {
    response = await apiFetch(`/api/sources/${id}/run`, {method: "POST"});
  } else if (action === "delete") {
    if (!window.confirm("确认删除该来源及其任务配置？已有发现记录不会自动删除。")) return;
    response = await apiFetch(`/api/sources/${id}`, {method: "DELETE"});
  } else {
    const sourcesResponse = await apiFetch("/api/sources");
    const sources = await sourcesResponse.json();
    const source = sources.find((item) => String(item.id) === String(button.dataset.sourceId));
    if (!source) throw new Error("来源不存在；未执行操作。");
    response = await apiFetch(`/api/sources/${id}`, {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({enabled: !source.enabled}),
    });
  }
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || result.error || "来源操作失败");
  showMessage(action === "run" ? "来源运行已完成；请查看任务运行记录和事件列表。" : "来源配置已更新。");
  await loadUnifiedSources();
}

function collectSettings(scope = document) {
  const values = {};
  scope.querySelectorAll("[data-setting]").forEach((element) => {
    const key = element.dataset.setting;
    if (!key) return;
    if (element.tagName === "BUTTON") return;
    if (key === "GITHUB_REPOSITORIES" || key === "GITEE_REPOSITORIES" || key === "VERIFICATION_ALLOWED_FINGERPRINTS") {
      values[key] = element.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
    } else if (element.type === "checkbox") {
      values[key] = element.checked;
    } else if (element.tagName === "SELECT" && (element.value === "true" || element.value === "false")) {
      values[key] = element.value === "true";
    } else if (element.type === "number") {
      values[key] = Number(element.value);
    } else if (element.type !== "password" || element.value) {
      values[key] = element.value;
    }
  });
  scope.querySelectorAll("[data-clear-setting]").forEach((button) => {
    if (button.dataset.clearSetting === "true") values[button.dataset.setting] = "";
  });
  return values;
}

async function saveSettings(button) {
  const scope = button?.dataset.settingsSave === "group" ? button.closest("[data-settings-group]") : document;
  const response = await apiFetch("/api/settings", {
    method: "PUT",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({values: collectSettings(scope)})
  });
  const payload = await response.json();
  if (!response.ok || payload.error) throw new Error(payload.error || "设置保存失败");
  showMessage(`设置已保存：${payload.changed.join("、")}。敏感项仍不会回显。`);
  scope.querySelectorAll("[data-clear-setting]").forEach(element => { element.dataset.clearSetting = "false"; });
  const unsaved = new Map([...document.querySelectorAll("input[data-setting], textarea[data-setting], select[data-setting]")]
    .filter(element => !scope.contains(element))
    .map(element => [element, {value: element.value, checked: element.checked}]));
  await loadSettings();
  unsaved.forEach((state, element) => { element.value = state.value; if (element.type === "checkbox") element.checked = state.checked; });
}

async function testGithubConnection() {
  const repositories = (settingElement("GITHUB_REPOSITORIES")?.value || "")
    .split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
  const result = document.querySelector("[data-github-test-result]");
  if (!result) return;
  if (repositories.length !== 1) {
    result.textContent = "请在白名单中只保留一个仓库后测试。";
    return;
  }
  result.textContent = "测试中，未扫描仓库内容……";
  const response = await apiFetch("/api/settings/test-github", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({repository: repositories[0]})
  });
  const payload = await response.json();
  result.textContent = payload.ok ? payload.message : payload.error;
}

async function testGiteeConnection() {
  const repositories = (settingElement("GITEE_REPOSITORIES")?.value || "")
    .split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
  const result = document.querySelector("[data-gitee-test-result]");
  if (!result) return;
  if (giteeSettingsPending()) {
    result.textContent = "有尚未保存的 Token 或联网设置，请先保存对应配置。";
    return;
  }
  if (repositories.length !== 1) {
    result.textContent = "请在 Gitee 白名单中只保留一个仓库后测试。";
    return;
  }
  result.textContent = "测试中，未扫描仓库内容……";
  const response = await apiFetch("/api/settings/test-gitee", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({repository: repositories[0]})
  });
  const payload = await response.json();
  result.textContent = payload.ok
    ? `${payload.message} 默认分支：${payload.default_branch || "未知"}`
    : payload.error || payload.detail || "连接测试失败，请稍后重试。";
}

function giteeSettingsPending() {
  return Boolean(settingElement("GITEE_TOKEN")?.value)
    || document.querySelector('[data-clear-setting="true"][data-setting="GITEE_TOKEN"]') !== null
    || settingElement("CONNECTOR_NETWORK_ENABLED")?.checked !== window.__settingsRuntime?.network_enabled;
}

async function testGiteeToken() {
  const result = document.querySelector("[data-gitee-token-test-result]");
  if (!result) return;
  if (giteeSettingsPending()) {
    result.textContent = "有尚未保存的 Token 或联网设置，请先保存对应配置。";
    return;
  }
  result.textContent = "验证中，只访问 Gitee 身份端点……";
  const response = await apiFetch("/api/settings/test-gitee-token", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({})
  });
  const payload = await response.json();
  result.textContent = payload.ok
    ? `${payload.message}${payload.login ? ` 当前账号：${payload.login}` : ""}`
    : payload.error || payload.detail || "Token 身份验证失败，请稍后重试。";
}

async function submitFeedback(button) {
  const response = await apiFetch(`/api/events/${encodeURIComponent(button.dataset.eventId)}/feedback`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({action: button.dataset.feedback, note: ""})
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "反馈提交失败");
  showMessage(`反馈已记录：${payload.feedback.disposition}`);
  window.setTimeout(() => window.location.reload(), 500);
}

async function deliverAlert(button) {
  const response = await apiFetch(`/api/alerts/${encodeURIComponent(button.dataset.alertId)}/deliver`, {method: "POST"});
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "告警记录失败");
  showMessage("离线通知桩已记录，未发送外部通知");
  window.setTimeout(() => window.location.reload(), 500);
}

async function createAlert(button) {
  const response = await apiFetch(`/api/events/${encodeURIComponent(button.dataset.eventId)}/alert`, {method: "POST"});
  const payload = await response.json();
  if (!response.ok || payload.error) throw new Error(payload.error || "告警创建失败");
  showMessage("告警已写入 SQLite 离线队列");
  window.setTimeout(() => window.location.reload(), 500);
}

async function exportEvidence(button) {
  const response = await apiFetch(`/api/events/${encodeURIComponent(button.dataset.eventId)}/evidence-export`);
  if (!response.ok) {
    const payload = await response.json();
    throw new Error(payload.error || "证据导出失败");
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `event-${button.dataset.eventId}-evidence.json`;
  link.click();
  URL.revokeObjectURL(url);
  showMessage("脱敏证据包已生成；未包含完整秘密");
}

async function batchFeedback(button) {
  const eventIds = [...document.querySelectorAll("[data-event-select]:checked")]
    .map((item) => item.value);
  if (!eventIds.length) {
    showMessage("请先选择至少一个事件。");
    return;
  }
  const response = await apiFetch("/api/events/batch-feedback", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({event_ids: eventIds, action: button.dataset.batchFeedback, note: ""})
  });
  const payload = await response.json();
  if (!response.ok && !payload.updated?.length) throw new Error(payload.error || "批量反馈失败");
  showMessage(payload.error || `已记录 ${payload.updated.length} 个事件的反馈`);
  window.setTimeout(() => window.location.reload(), 500);
}

async function exportEvents() {
  const eventIds = [...document.querySelectorAll("[data-event-select]:checked")]
    .map((item) => item.value);
  const suffix = eventIds.length ? `?event_ids=${encodeURIComponent(eventIds.join(","))}` : "";
  const response = await apiFetch(`/api/events/export${suffix}`);
  const payload = await response.json();
  if (!response.ok || payload.error) throw new Error(payload.error || "事件导出失败");
  const blob = new Blob([JSON.stringify(payload, null, 2)], {type: "application/json"});
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "events-masked.json";
  link.click();
  URL.revokeObjectURL(url);
  showMessage("脱敏事件导出已生成；未包含完整秘密");
}

async function runDueTasks() {
  const response = await apiFetch("/api/backend/run-due", {method: "POST"});
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || payload.error || "采集调度失败");
  const result = payload.result || {};
  showMessage(`已检查到期任务：尝试 ${result.attempted ?? 0} 个，完成 ${result.completed ?? 0} 个，失败 ${result.failed ?? 0} 个`);
  window.setTimeout(() => window.location.reload(), 700);
}

async function exportReport() {
  const response = await apiFetch("/api/reports/export");
  const payload = await response.json();
  if (!response.ok || payload.error) throw new Error(payload.error || "报告导出失败");
  const blob = new Blob([JSON.stringify(payload, null, 2)], {type: "application/json"});
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "evaluation-report.json";
  link.click();
  URL.revokeObjectURL(url);
  showMessage("指标报告已导出；统计保留当前来源和不确定性");
}

async function mutateTask(button) {
  const action = button.dataset.taskAction;
  const taskId = encodeURIComponent(button.dataset.taskId);
  const method = action === "delete" ? "DELETE" : "POST";
  const response = await apiFetch(`/api/monitoring-tasks/${taskId}/${action}`, {method});
  const payload = await response.json();
  if (!response.ok || payload.error) throw new Error(payload.error || "任务操作失败");
  showMessage(action === "delete" ? "监控任务已删除" : "监控任务状态已更新");
  window.setTimeout(() => window.location.reload(), 400);
}

async function createTask(form) {
  const payload = Object.fromEntries(new FormData(form).entries());
  const response = await apiFetch("/api/monitoring-tasks", {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)
  });
  const result = await response.json();
  if (!response.ok || result.error) throw new Error(result.error || "任务保存失败");
  showMessage("监控任务配置已写入 SQLite，尚未启动采集");
  window.setTimeout(() => window.location.reload(), 400);
}

function updateUnifiedBadges(payload) {
  document.querySelectorAll("[data-unified-source]").forEach((item) => {
    item.textContent = payload.source || "真实数据：规范化后端 SQLite";
  });
  document.querySelectorAll("[data-unified-status]").forEach((item) => {
    item.textContent = payload.implementation_status || "部分实现";
  });
}

function badgeForStatus(status) {
  const safe = String(status || "unknown").toLowerCase().replace(/[^a-z0-9_-]/g, "-");
  return `<span class="badge badge-${safe}">${escapeHtml(status || "未知")}</span>`;
}

async function loadFindings() {
  const rows = document.querySelector("[data-findings-rows]");
  if (!rows) return;
  const params = new URLSearchParams();
  document.querySelectorAll("[data-finding-filter]").forEach((input) => {
    if (input.value.trim()) params.set(input.dataset.findingFilter, input.value.trim());
  });
  params.set("page_size", "50");
  const response = await apiFetch(`/api/findings?${params.toString()}`);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "统一发现项读取失败");
  updateUnifiedBadges(payload);
  const items = payload.items || [];
  const pending = items.filter((item) => item.review_status === "pending").length;
  const waiting = items.filter((item) => ["not_requested", "queued"].includes(item.verification_status)).length;
  const summary = document.querySelector("[data-findings-summary]");
  if (summary) {
    const values = [payload.total ?? 0, items.length, pending, waiting];
    summary.querySelectorAll(".value").forEach((item, index) => { item.textContent = values[index]; });
  }
  rows.innerHTML = items.length ? items.map((item) => `<tr>
    <td>#${escapeHtml(item.id)}</td>
    <td>${escapeHtml(item.type)}<div class="muted small">${escapeHtml(item.vendor || "未分类厂商")}</div></td>
    <td><code>${escapeHtml(item.preview)}</code></td>
    <td><code>${escapeHtml(item.fingerprint)}</code></td>
    <td>${escapeHtml(item.confidence)}</td>
    <td>${escapeHtml(item.occurrence_count)}</td>
    <td>${badgeForStatus(item.review_status)}</td>
    <td>${badgeForStatus(item.verification_status)}</td>
    <td>${escapeHtml(item.first_seen_at || "未知")}</td>
    <td class="button-row">
      <button data-finding-review="${item.id}" data-review-action="confirm">确认</button>
      <button data-finding-review="${item.id}" data-review-action="false_positive">误报</button>
      <button data-finding-verify="${item.id}">排队验证</button>
    </td>
  </tr>`).join("") : '<tr><td colspan="10" class="muted">统一后端当前没有发现项；不会自动用桩数据补齐。</td></tr>';
  const pagination = document.querySelector("[data-findings-pagination]");
  if (pagination) pagination.textContent = `共 ${payload.total ?? 0} 项；当前页 ${items.length} 项`;
  rows.querySelectorAll("[data-finding-review]").forEach((button) => {
    button.addEventListener("click", () => reviewFinding(button).catch((error) => showMessage(error.message)));
  });
  rows.querySelectorAll("[data-finding-verify]").forEach((button) => {
    button.addEventListener("click", () => verifyFinding(button).catch((error) => showMessage(error.message)));
  });
}

async function reviewFinding(button) {
  const response = await apiFetch(`/api/findings/${encodeURIComponent(button.dataset.findingReview)}/review`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({action: button.dataset.reviewAction, note: ""}),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "发现项复核失败");
  showMessage(`复核状态已更新为：${payload.review_status}`);
  await loadFindings();
}

async function verifyFinding(button) {
  const response = await apiFetch(`/api/findings/${encodeURIComponent(button.dataset.findingVerify)}/verify`, {method: "POST"});
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "验证任务创建失败");
  showMessage(`验证任务已排队：${payload.job_id ?? "-"}`);
  await loadFindings();
}

async function loadVerification() {
  const jobs = document.querySelector("[data-verification-jobs]");
  if (!jobs) return;
  const [jobsResponse, historyResponse] = await Promise.all([
    apiFetch("/api/verification/jobs"),
    apiFetch("/api/verification/history"),
  ]);
  const jobEnvelope = await jobsResponse.json();
  const historyEnvelope = await historyResponse.json();
  if (!jobsResponse.ok || !historyResponse.ok) throw new Error("验证中心读取失败");
  updateUnifiedBadges(jobEnvelope);
  const jobPayload = Array.isArray(jobEnvelope) ? jobEnvelope : (jobEnvelope.items || []);
  const historyPayload = Array.isArray(historyEnvelope) ? historyEnvelope : (historyEnvelope.items || []);
  jobs.innerHTML = jobPayload.length ? jobPayload.map((item) => `<tr>
    <td>#${escapeHtml(item.id)}</td><td>#${escapeHtml(item.credential_id)}</td>
    <td>${escapeHtml(item.type)}</td><td>${escapeHtml(item.verifier_id)}</td>
    <td>${escapeHtml(item.requested_by)}</td><td>${badgeForStatus(item.status)}</td>
    <td>${escapeHtml(formatTimestamp(item.created_at))}</td>
  </tr>`).join("") : '<tr><td colspan="7" class="muted">暂无验证任务。</td></tr>';
  const history = document.querySelector("[data-verification-history]");
  if (history) history.innerHTML = historyPayload.length ? historyPayload.map((item) => `<tr>
    <td>#${escapeHtml(item.credential_id)}</td><td>${escapeHtml(item.verifier_id)}</td>
    <td>${escapeHtml(item.verifier_version)}</td><td>${badgeForStatus(item.status)}</td>
    <td>${escapeHtml(item.latency_ms ?? "-")} ms</td><td>${escapeHtml(item.evidence_json || item.evidence?.reason || "-")}</td>
    <td>${escapeHtml(formatTimestamp(item.created_at))}</td>
  </tr>`).join("") : '<tr><td colspan="7" class="muted">暂无验证历史。</td></tr>';
  const counts = {
    queued: jobPayload.filter((item) => item.status === "queued").length,
    inconclusive: historyPayload.filter((item) => item.status === "inconclusive").length,
    valid: historyPayload.filter((item) => item.status === "valid").length,
    invalid: historyPayload.filter((item) => item.status === "invalid").length,
  };
  document.querySelectorAll("[data-verification-summary] .value").forEach((item, index) => {
    item.textContent = [counts.queued, counts.inconclusive, counts.valid, counts.invalid][index];
  });
}

async function processVerification() {
  const response = await apiFetch("/api/verification/process", {method: "POST"});
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "验证队列处理失败");
  showMessage(`验证队列已处理 ${payload.processed ?? 0} 项；联网未授权项会保持无法判断。`);
  await loadVerification();
}

async function loadChannels() {
  const collectors = document.querySelector("[data-collector-rows]");
  if (!collectors) return;
  const [channelResponse, ruleResponse] = await Promise.all([apiFetch("/api/channels"), apiFetch("/api/rules")]);
  const channelPayload = await channelResponse.json();
  const rules = await ruleResponse.json();
  if (!channelResponse.ok || !ruleResponse.ok) throw new Error("渠道与规则读取失败");
  updateUnifiedBadges(channelPayload);
  collectors.innerHTML = (channelPayload.collectors || []).map((item) => `<tr>
    <td>${escapeHtml(item.name)}</td><td>${escapeHtml(item.channel)}</td><td>${escapeHtml(item.platform)}</td>
    <td>${escapeHtml(item.mode)}</td><td>${escapeHtml(item.implementation_status || "未知")}</td>
    <td>${escapeHtml(item.offline_test_status || "未验证")}</td>
    <td>${escapeHtml(item.real_platform_test_status || "未实测")}</td>
    <td>${badgeForStatus(item.status)}</td>
    <td><span class="muted small">${escapeHtml(item.evidence_ref || item.documentation || "-")}</span></td>
  </tr>`).join("") || '<tr><td colspan="9" class="muted">暂无连接器能力。</td></tr>';
  const ruleRows = document.querySelector("[data-rule-rows]");
  if (ruleRows) ruleRows.innerHTML = (rules || []).map((item) => `<tr>
    <td><code>${escapeHtml(item.id)}</code></td><td>${escapeHtml(item.title)}</td>
    <td>${escapeHtml(item.type)}</td><td>${escapeHtml(item.vendor || "-")}</td>
    <td>${escapeHtml(item.version)}</td><td>${escapeHtml(item.implementation_status || "已实现")}</td>
    <td>${escapeHtml(item.offline_test_status || "未验证")}</td>
    <td>${escapeHtml(item.real_platform_test_status || "不适用")}</td>
    <td>${item.enabled ? "开启" : "关闭"}</td>
    <td><span class="muted small">${escapeHtml(item.evidence_ref || "-")}</span></td>
    <td><button data-rule-id="${escapeHtml(item.id)}" data-rule-enabled="${item.enabled}">${item.enabled ? "关闭" : "开启"}</button></td>
  </tr>`).join("") || '<tr><td colspan="11" class="muted">暂无规则。</td></tr>';
  ruleRows?.querySelectorAll("[data-rule-id]").forEach((button) => {
    button.addEventListener("click", () => toggleRule(button).catch((error) => showMessage(error.message)));
  });
  const cards = document.querySelector("[data-verifier-cards]");
  if (cards) cards.innerHTML = (channelPayload.verifiers || []).map((item) => `<div class="panel">
    <strong>${escapeHtml(item.title || item.id)}</strong><p class="muted small">版本：${escapeHtml(item.version || "-")}</p>
    <p class="muted small">支持类型：${escapeHtml((item.supported_types || []).join("、") || "未声明")}</p>
    <p class="muted small">${escapeHtml(item.endpoint || "仅离线记录")}</p>
  </div>`).join("") || '<div class="panel muted">暂无验证器。</div>';
  const matrixRows = document.querySelector("[data-support-matrix-rows]");
  if (matrixRows) {
    const rows = channelPayload.support_matrix || [];
    matrixRows.innerHTML = rows.length ? rows.map((item) => `<tr>
      <td>${escapeHtml(item.item_kind === "channel" ? "渠道" : "凭据")}</td>
      <td>${escapeHtml(item.channel_type || item.credential_type || "-")}</td>
      <td>${escapeHtml(item.platform || item.vendor || "-")}</td>
      <td>${escapeHtml(item.public_carrier || item.format_variant || "-")}</td>
      <td>${escapeHtml(item.permission_condition || "-")}</td>
      <td>${escapeHtml(item.collection_method || "-")} / ${escapeHtml(item.parse_method || "-")}</td>
      <td>${escapeHtml(item.polling_method || "-")}</td>
      <td>${badgeForStatus(item.status)}</td>
      <td><span class="muted small">${escapeHtml(item.evidence_ref || "-")}</span></td>
    </tr>`).join("") : '<tr><td colspan="9" class="muted">暂无支持矩阵记录。</td></tr>';
  }
  const experimentRows = document.querySelector("[data-experiment-rows]");
  if (experimentRows) {
    const rows = channelPayload.extensibility_experiments || [];
    experimentRows.innerHTML = rows.length ? rows.map((item) => `<tr>
      <td>${escapeHtml(item.connector_key)}</td><td>${badgeForStatus(item.execution_status)}</td>
      <td>${escapeHtml(item.code_lines ?? "未知")}</td><td>${escapeHtml(item.config_fields ?? "未知")}</td>
      <td>${escapeHtml(item.development_hours ?? "未知")} h</td><td>${escapeHtml(item.test_duration_ms ?? "未知")} ms</td>
      <td>${item.checkpoint_verified ? "通过" : "未记录"}</td>
      <td>${item.failure_recovery_verified ? "通过" : "未记录"}</td>
      <td><span class="muted small">${escapeHtml(item.evidence_ref || item.notes || "-")}</span></td>
    </tr>`).join("") : '<tr><td colspan="9" class="muted">暂无扩展性实验记录。</td></tr>';
  }
}

async function toggleRule(button) {
  const response = await apiFetch(`/api/rules/${encodeURIComponent(button.dataset.ruleId)}`, {
    method: "PATCH",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({enabled: button.dataset.ruleEnabled !== "true"}),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "规则状态更新失败");
  showMessage(`规则已${payload.enabled ? "开启" : "关闭"}；后续检测将使用新规则快照。`);
  await loadChannels();
}

async function runExtensibilityExperiments() {
  const response = await apiFetch("/api/extensibility-experiments/run", {method: "POST"});
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || payload.error || "扩展性实验运行失败");
  const passed = (payload.items || []).filter((item) => item.execution_status === "passed").length;
  showMessage(`离线扩展性实验完成：${passed}/${(payload.items || []).length} 项通过；未进行真实平台测试。`);
  await loadChannels();
}

async function loadConnectorRuns() {
  const rows = document.querySelector("[data-runs-rows]");
  if (!rows) return;
  const response = await apiFetch("/api/backend-runs");
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "运行记录读取失败");
  updateUnifiedBadges(payload);
  rows.innerHTML = payload.items.length ? payload.items.map((item) => `<tr>
    <td><code>${escapeHtml(item.run_id)}</code></td><td>${escapeHtml(item.connector_name)}</td>
    <td>${escapeHtml(item.target)}</td><td>${escapeHtml(item.task_id || "-")}</td>
    <td>${escapeHtml(formatTimestamp(item.started_at))}</td><td>${escapeHtml(formatTimestamp(item.finished_at, "进行中"))}</td>
    <td>${escapeHtml(item.duration_seconds == null ? "-" : `${item.duration_seconds}s`)}</td>
    <td>${badgeForStatus(item.status)}</td><td>${escapeHtml(item.candidates)}</td>
    <td>${escapeHtml(item.inserted)}</td><td>${escapeHtml(item.error_code || "-")}</td>
  </tr>`).join("") : '<tr><td colspan="11" class="muted">暂无统一连接器运行记录。</td></tr>';
}

async function loadUnifiedOverview() {
  const block = document.querySelector("[data-overview-unified]");
  if (!block) return;
  const response = await apiFetch("/api/overview/stats");
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "统一后端总览读取失败");
  updateUnifiedBadges(payload);
  const latency = payload.detection_latency?.published_to_detected || {};
  const p95 = latency.p95_seconds == null ? "未知" : `${latency.p95_seconds}s`;
  const values = [
    payload.credentials_total ?? 0,
    payload.occurrences_total ?? 0,
    payload.notification_queue ?? 0,
    p95,
  ];
  block.querySelectorAll(".value").forEach((item, index) => { item.textContent = values[index]; });
}

async function loadNotifications() {
  const rows = document.querySelector("[data-notification-rows]");
  if (!rows) return;
  const response = await apiFetch("/api/notifications");
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "通知队列读取失败");
  document.querySelectorAll("[data-notification-source]").forEach((item) => {
    item.textContent = payload.source || "真实数据：规范化后端 SQLite 离线通知队列";
  });
  document.querySelectorAll("[data-notification-status]").forEach((item) => {
    item.textContent = payload.implementation_status || "部分实现";
  });
  const items = payload.items || [];
  rows.innerHTML = items.length ? items.map((item) => `<tr>
    <td><code>${escapeHtml(item.notification_id)}</code></td>
    <td><a href="/events/${encodeURIComponent(item.event_id)}">${escapeHtml(item.event_id)}</a></td>
    <td>${badgeForStatus(item.severity)}</td>
    <td>${escapeHtml(item.payload?.source_kind || "-")}</td>
    <td>${badgeForStatus(item.status)}</td>
    <td>${escapeHtml(item.attempt_count ?? 0)}</td>
    <td>${escapeHtml(formatTimestamp(item.created_at))}</td>
    <td class="button-row">
      <button data-notification-record="${escapeHtml(item.notification_id)}">记录离线通知</button>
      <button data-notification-retry="${escapeHtml(item.notification_id)}">重新排队</button>
    </td>
  </tr>`).join("") : '<tr><td colspan="8" class="muted">当前没有通知记录。</td></tr>';
  rows.querySelectorAll("[data-notification-record]").forEach((button) => {
    button.addEventListener("click", () => recordNotification(button).catch((error) => showMessage(error.message)));
  });
  rows.querySelectorAll("[data-notification-retry]").forEach((button) => {
    button.addEventListener("click", () => retryNotification(button).catch((error) => showMessage(error.message)));
  });
}

async function recordNotification(button) {
  const response = await apiFetch(`/api/notifications/${encodeURIComponent(button.dataset.notificationRecord)}/record`, {method: "POST"});
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || payload.error || "通知记录失败");
  showMessage("离线通知已记录；未发送外部消息。");
  await loadNotifications();
}

async function retryNotification(button) {
  const response = await apiFetch(`/api/notifications/${encodeURIComponent(button.dataset.notificationRetry)}/retry`, {method: "POST"});
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || payload.error || "通知重新排队失败");
  showMessage("通知已重新排队；仍未发送外部消息。");
  await loadNotifications();
}

async function loadSecurityAudit() {
  const rows = document.querySelector("[data-audit-rows]");
  if (!rows) return;
  const response = await apiFetch("/api/security/audit?limit=100");
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "安全审计读取失败");
  document.querySelectorAll("[data-audit-source]").forEach((item) => {
    item.textContent = payload.source || "真实数据：规范化后端 SQLite security_audit";
  });
  document.querySelectorAll("[data-audit-status]").forEach((item) => {
    item.textContent = payload.implementation_status || "部分实现";
  });
  const items = payload.items || [];
  rows.innerHTML = items.length ? items.map((item) => `<tr>
    <td>${escapeHtml(formatTimestamp(item.created_at))}</td>
    <td>${escapeHtml(item.role || "-")}</td>
    <td><code>${escapeHtml(item.action || "-")}</code></td>
    <td>${escapeHtml(item.resource_type || "-")} ${escapeHtml(item.resource_id || "")}</td>
    <td>${badgeForStatus(item.decision)}</td>
    <td>${escapeHtml(item.reason || "-")}</td>
    <td>${escapeHtml(item.source_ip || "-")}</td>
    <td><code>${escapeHtml(JSON.stringify(item.metadata || {}))}</code></td>
  </tr>`).join("") : '<tr><td colspan="8" class="muted">当前没有安全审计记录。</td></tr>';
}

async function loadUnifiedEvaluation() {
  const block = document.querySelector("[data-evaluation-unified]");
  if (!block) return;
  const response = await apiFetch("/api/evaluation-report");
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "统一后端报告读取失败");
  updateUnifiedBadges(payload);
  const counts = payload.counts || {};
  const p95 = payload.timeliness?.published_to_detected?.p95_seconds;
  const values = [
    counts.candidate_count ?? 0,
    counts.channel_type_count ?? 0,
    counts.credential_type_count ?? 0,
    p95 == null ? "未知" : `${p95}s`,
  ];
  block.querySelectorAll(".value").forEach((item, index) => { item.textContent = values[index]; });
  const breakdown = document.querySelector("[data-evaluation-breakdown]");
  if (breakdown) {
    const entries = Object.entries(counts.source_kind_breakdown || {}).map(
      ([key, value]) => `<span class="badge badge-neutral">${escapeHtml(key)}：${escapeHtml(value)}</span>`
    );
    breakdown.innerHTML = `<strong>来源明细：</strong> ${entries.join(" ") || "当前没有统一后端发现记录。"}<p class="muted small">${escapeHtml(payload.timeliness?.note || "")}</p>`;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-unimplemented]").forEach((button) => {
    button.addEventListener("click", () => showNotImplemented(button.dataset.unimplemented));
  });
  document.querySelectorAll("[data-feedback]").forEach((button) => {
    button.addEventListener("click", () => submitFeedback(button).catch((error) => showMessage(error.message)));
  });
  document.querySelectorAll("[data-deliver-alert]").forEach((button) => {
    button.addEventListener("click", () => deliverAlert(button).catch((error) => showMessage(error.message)));
  });
  document.querySelectorAll("[data-create-alert]").forEach((button) => {
    button.addEventListener("click", () => createAlert(button).catch((error) => showMessage(error.message)));
  });
  document.querySelectorAll("[data-export-evidence]").forEach((button) => {
    button.addEventListener("click", () => exportEvidence(button).catch((error) => showMessage(error.message)));
  });
  document.querySelectorAll("[data-batch-feedback]").forEach((button) => {
    button.addEventListener("click", () => batchFeedback(button).catch((error) => showMessage(error.message)));
  });
  document.querySelectorAll("[data-export-events]").forEach((button) => {
    button.addEventListener("click", () => exportEvents().catch((error) => showMessage(error.message)));
  });
  document.querySelectorAll("[data-run-due]").forEach((button) => {
    button.addEventListener("click", () => runDueTasks().catch((error) => showMessage(error.message)));
  });
  document.querySelectorAll("[data-export-report]").forEach((button) => {
    button.addEventListener("click", () => exportReport().catch((error) => showMessage(error.message)));
  });
  document.querySelectorAll("[data-refresh-page]").forEach((button) => {
    button.addEventListener("click", () => window.location.reload());
  });
  document.querySelectorAll("[data-open-credential-distribution]").forEach((button) => {
    button.addEventListener("click", () => {
      const dialog = document.querySelector("[data-credential-dialog]");
      if (dialog?.showModal) dialog.showModal();
    });
  });
  document.querySelectorAll("[data-close-credential-distribution]").forEach((button) => {
    button.addEventListener("click", () => {
      const dialog = document.querySelector("[data-credential-dialog]");
      dialog?.close();
    });
  });
  document.querySelectorAll("[data-select-all]").forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      document.querySelectorAll("[data-event-select]").forEach((item) => {
        item.checked = checkbox.checked;
      });
    });
  });
  document.querySelectorAll("[data-toggle-task-form]").forEach((button) => {
    button.addEventListener("click", () => {
      const form = document.querySelector("[data-task-form]");
      form.hidden = !form.hidden;
    });
  });
  document.querySelectorAll("[data-task-action]").forEach((button) => {
    button.addEventListener("click", () => mutateTask(button).catch((error) => showMessage(error.message)));
  });
  document.querySelectorAll("[data-task-form]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      createTask(form).catch((error) => showMessage(error.message));
    });
  });
  document.querySelectorAll("[data-settings-save]").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try { await saveSettings(button); } catch (error) { showMessage(error.message); }
      finally { button.disabled = false; }
    });
  });
  document.querySelectorAll("[data-settings-tab]").forEach((tab, index, tabs) => {
    tab.addEventListener("click", () => { window.location.hash = tab.dataset.settingsTab; activateSettingsTab(tab.dataset.settingsTab); });
    tab.addEventListener("keydown", (event) => {
      const direction = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
      if (!direction && !["Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const target = event.key === "Home" ? tabs[0] : event.key === "End" ? tabs[tabs.length - 1] : tabs[(index + direction + tabs.length) % tabs.length];
      window.location.hash = target.dataset.settingsTab;
      activateSettingsTab(target.dataset.settingsTab, true);
    });
  });
  activateSettingsTab(window.location.hash.slice(1));
  window.addEventListener("hashchange", () => activateSettingsTab(window.location.hash.slice(1)));
  document.querySelectorAll("[data-logout]").forEach(button => {
    button.addEventListener("click", async () => {
      try {
        const response = await apiFetch("/api/auth/logout", {method: "POST"});
        if (!response.ok) throw new Error("退出失败，请刷新后重试。");
        window.location.assign("/login");
      } catch (error) { showMessage(error.message); }
    });
  });
  document.querySelector("[data-password-form]")?.addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    try { await changePassword(form); }
    catch (error) { form.querySelector("[data-password-result]").textContent = error.message; }
    finally { button.disabled = false; }
  });
  document.querySelectorAll("[data-settings-refresh]").forEach((button) => {
    button.addEventListener("click", () => Promise.all([loadSettings(), loadUnifiedSources()]).catch((error) => showMessage(error.message)));
  });
  document.querySelectorAll("[data-source-form]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      createUnifiedSource(form).catch((error) => {
        const result = document.querySelector("[data-source-form-result]");
        if (result) result.textContent = error.message;
        showMessage(error.message);
      });
    });
  });
  const sourceCollector = document.querySelector("#source-collector");
  if (sourceCollector) {
    sourceCollector.addEventListener("change", () => {
      const item = window.__sourceCatalog?.find((entry) => entry.key === sourceCollector.value);
      const hint = document.querySelector("[data-source-config-hint]");
      if (hint) hint.textContent = item ? `最小示例：${item.config_hint}` : "请填写 JSON 对象。";
    });
  }
  document.querySelectorAll("[data-clear-setting]").forEach((button) => {
    button.addEventListener("click", () => {
      button.dataset.clearSetting = "true";
      const input = settingElement(button.dataset.setting);
      if (input) input.value = "";
      showMessage(`已标记清除${button.dataset.setting}，点击保存后生效。`);
    });
  });
  document.querySelectorAll("[data-github-test]").forEach((button) => {
    button.addEventListener("click", () => testGithubConnection().catch((error) => showMessage(error.message)));
  });
  document.querySelectorAll("[data-gitee-test]").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try { await testGiteeConnection(); }
      catch (error) { document.querySelector("[data-gitee-test-result]").textContent = error.message; }
      finally { button.disabled = false; }
    });
  });
  document.querySelectorAll("[data-gitee-token-test]").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try { await testGiteeToken(); }
      catch (error) { document.querySelector("[data-gitee-token-test-result]").textContent = error.message; }
      finally { button.disabled = false; }
    });
  });
  if (document.querySelector("[data-setting]")) {
    loadSettings().then(loadUnifiedSources).catch((error) => showMessage(error.message));
  }
  document.querySelectorAll("[data-findings-refresh], [data-findings-apply]").forEach((button) => {
    button.addEventListener("click", () => loadFindings().catch((error) => showMessage(error.message)));
  });
  document.querySelectorAll("[data-unified-export]").forEach((button) => {
    button.addEventListener("click", () => { window.location.href = "/api/export/findings.json"; });
  });
  document.querySelectorAll("[data-verification-refresh]").forEach((button) => {
    button.addEventListener("click", () => loadVerification().catch((error) => showMessage(error.message)));
  });
  document.querySelectorAll("[data-verification-process]").forEach((button) => {
    button.addEventListener("click", () => processVerification().catch((error) => showMessage(error.message)));
  });
  document.querySelectorAll("[data-channels-refresh]").forEach((button) => {
    button.addEventListener("click", () => loadChannels().catch((error) => showMessage(error.message)));
  });
  document.querySelectorAll("[data-experiments-run]").forEach((button) => {
    button.addEventListener("click", () => runExtensibilityExperiments().catch((error) => showMessage(error.message)));
  });
  document.querySelectorAll("[data-runs-refresh]").forEach((button) => {
    button.addEventListener("click", () => loadConnectorRuns().catch((error) => showMessage(error.message)));
  });
  if (document.querySelector("[data-findings-rows]")) loadFindings().catch((error) => showMessage(error.message));
  if (document.querySelector("[data-verification-jobs]")) loadVerification().catch((error) => showMessage(error.message));
  if (document.querySelector("[data-collector-rows]")) loadChannels().catch((error) => showMessage(error.message));
  if (document.querySelector("[data-runs-rows]")) loadConnectorRuns().catch((error) => showMessage(error.message));
  if (document.querySelector("[data-overview-unified]")) loadUnifiedOverview().catch((error) => showMessage(error.message));
  if (document.querySelector("[data-notification-rows]")) loadNotifications().catch((error) => showMessage(error.message));
  if (document.querySelector("[data-audit-rows]")) loadSecurityAudit().catch((error) => showMessage(error.message));
  if (document.querySelector("[data-evaluation-unified]")) loadUnifiedEvaluation().catch((error) => showMessage(error.message));
});
