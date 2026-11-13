<template>
  <div class="endpoint-builder">
    <el-alert
      title="不会自动猜测第三方接口。只有你明确配置并启用的端点才会被访问。"
      type="info"
      :closable="false"
      show-icon
      class="base-alert"
    />
    <div class="endpoint-guide">
      <div class="guide-heading">
        <el-icon><InfoFilled /></el-icon>
        <div>
          <strong>先判断是否需要新增端点</strong>
          <p>平台已有专用验证器时，优先使用专用验证器；只有没有专用验证器的类型，才在这里配置自有端点。</p>
        </div>
      </div>
      <div class="guide-grid">
        <div class="guide-step">
          <span class="guide-index">1</span>
          <div>
            <strong>GitHub 等内置平台</strong>
            <p>例如 <code>github_pat</code> 使用项目内置的 GitHub 身份端点；只需填写 GitHub Token 和授权仓库白名单。</p>
            <el-button link type="primary" @click="$emit('open-connections')">去平台授权设置</el-button>
          </div>
        </div>
        <div class="guide-step">
          <span class="guide-index">2</span>
          <div>
            <strong>自有系统或未内置平台</strong>
            <p>为发现项的 credential type 配置一个明确返回 JSON 身份结果的 HTTPS 端点。</p>
          </div>
        </div>
        <div class="guide-step">
          <span class="guide-index">3</span>
          <div>
            <strong>保存后主动验证</strong>
            <p>保存端点后，回到发现项列表点击“验证”，系统不会自动批量请求。</p>
          </div>
        </div>
      </div>
    </div>

    <div class="builder-toolbar">
      <div>
        <span class="field-caption">配置场景</span>
        <el-select v-model="preset" class="preset-select" aria-label="配置场景">
          <el-option
            v-for="item in presets"
            :key="item.value"
            :label="item.label"
            :value="item.value"
          />
        </el-select>
      </div>
      <el-button :icon="MagicStick" @click="applyPreset">填入配置模板</el-button>
    </div>

    <el-alert
      v-if="preset === 'builtin-github'"
      title="GitHub 已有平台专用验证器"
      description="如果发现项类型是 github_pat，请到“平台授权设置”配置 GitHub Token 并测试。这里的通用端点不会覆盖平台专用验证器。"
      type="success"
      :closable="false"
      show-icon
      class="preset-alert"
    />
    <el-alert
      v-else-if="preset === 'mediawiki'"
      title="MediaWiki 需要明确的身份确认契约"
      description="请填写你有权访问的 MediaWiki 或自有验证代理地址，并确认它会按下方规则返回 JSON。若你的系统需要用户名与机器人密码组合，建议先由自有验证代理统一成一个输入字段。"
      type="warning"
      :closable="false"
      show-icon
      class="preset-alert"
    />

    <div class="endpoint-workspace">
      <aside class="endpoint-list-panel">
        <div class="panel-heading">
          <div>
            <strong>已配置端点</strong>
            <span>{{ endpoints.length }} 项 · 点击切换编辑</span>
          </div>
          <el-button
            circle
            plain
            type="primary"
            :icon="Plus"
            title="新增授权端点"
            aria-label="新增授权端点"
            @click="createEndpoint"
          />
        </div>

        <button
          v-for="(item, index) in endpoints"
          :key="item.endpoint_id || index"
          type="button"
          class="endpoint-list-item"
          :class="{ active: selectedIndex === index }"
          @click="selectEndpoint(index)"
        >
          <span class="endpoint-list-title">
            <strong>{{ item.system_name || "未命名系统" }}</strong>
            <el-tag size="small" :type="item.enabled ? 'success' : 'info'" effect="plain">
              {{ item.enabled ? "已启用" : "已停用" }}
            </el-tag>
          </span>
          <span class="endpoint-list-meta">{{ item.platform || "未设置平台" }}</span>
          <small>{{ (item.supported_types || []).join("、") || "未设置凭据类型" }}</small>
        </button>

        <div v-if="!endpoints.length" class="endpoint-list-empty">
          <el-icon><Link /></el-icon>
          <strong>还没有授权端点</strong>
          <span>点击右上角加号，或先选择一个配置模板。</span>
        </div>
      </aside>

      <section v-if="selectedIndex >= 0" class="endpoint-editor-panel">
        <div class="editor-heading">
          <div>
            <span class="field-caption">正在编辑</span>
            <h3>{{ draft.system_name || "新授权端点" }}</h3>
          </div>
          <div class="editor-actions">
            <el-switch v-model="draft.enabled" active-text="启用" inactive-text="停用" />
            <el-button
              link
              type="danger"
              :icon="Delete"
              @click="removeEndpoint"
            >删除端点</el-button>
          </div>
        </div>

        <el-form label-position="top" class="endpoint-form">
          <div class="form-section">
            <div class="form-section-heading">
              <span class="section-number">A</span>
              <div>
                <strong>端点身份</strong>
                <p>用于把端点与平台、凭据类型绑定。</p>
              </div>
            </div>
            <div class="form-grid two">
              <el-form-item label="端点标识" required>
                <el-input v-model="draft.endpoint_id" placeholder="例如 mediawiki-token-check" />
                <small>只能使用字母、数字、点、下划线、冒号和短横线。</small>
              </el-form-item>
              <el-form-item label="系统名称" required>
                <el-input v-model="draft.system_name" placeholder="例如 公司 MediaWiki" />
              </el-form-item>
              <el-form-item label="平台标识" required>
                <el-input v-model="draft.platform" placeholder="例如 MediaWiki 或 internal-wiki" />
              </el-form-item>
              <el-form-item label="支持的凭据类型" required>
                <el-input
                  v-model="draft.supported_types"
                  placeholder="例如 config_assignment_secret"
                />
                <small>多个类型用英文逗号分隔；必须填写发现项中的 credential type。</small>
              </el-form-item>
            </div>
          </div>

          <div class="form-section">
            <div class="form-section-heading">
              <span class="section-number">B</span>
              <div>
                <strong>请求方式</strong>
                <p>系统只会把发现项凭据按这里的方式发送到这个明确端点。</p>
              </div>
            </div>
            <div class="form-grid three">
              <el-form-item label="端点 URL" required class="span-two">
                <el-input v-model="draft.endpoint_url" placeholder="https://wiki.example.com/w/api.php" />
                <small>只允许 HTTPS；本机调试可使用 localhost、127.0.0.1 或 ::1 的 HTTP。</small>
              </el-form-item>
              <el-form-item label="HTTP 方法">
                <el-select v-model="draft.method" class="full-width">
                  <el-option label="GET" value="GET" />
                  <el-option label="POST" value="POST" />
                </el-select>
              </el-form-item>
              <el-form-item label="授权放置位置">
                <el-select v-model="draft.auth_placement" class="full-width">
                  <el-option label="请求头 Header" value="header" />
                  <el-option label="JSON 请求体" value="json_body" />
                  <el-option label="查询参数 Query" value="query" />
                </el-select>
              </el-form-item>
              <el-form-item label="授权字段名">
                <el-input v-model="draft.auth_name" placeholder="Authorization" />
              </el-form-item>
              <el-form-item label="授权格式">
                <el-select v-model="draft.auth_scheme" class="full-width">
                  <el-option label="Bearer Token" value="bearer" />
                  <el-option label="原始值 Raw" value="raw" />
                  <el-option label="Basic（仅 Header）" value="basic" />
                </el-select>
              </el-form-item>
              <el-form-item label="超时（秒）">
                <el-input-number v-model="draft.timeout_seconds" :min="1" :max="120" controls-position="right" class="full-width" />
              </el-form-item>
              <el-form-item label="允许访问的主机" required class="span-two">
                <el-input v-model="draft.allowed_hosts" placeholder="wiki.example.com" />
                <small>填写端点 URL 的主机名；多个主机用英文逗号分隔。系统不会访问未列入白名单的主机。</small>
              </el-form-item>
            </div>
          </div>

          <div class="form-section">
            <div class="form-section-heading">
              <span class="section-number">C</span>
              <div>
                <strong>身份响应规则</strong>
                <p>HTTP 200 本身不代表凭据有效，必须同时匹配 JSON 字段。</p>
              </div>
            </div>
            <div class="condition-box success">
              <div class="condition-heading">
                <el-tag type="success" effect="plain">成功</el-tag>
                <span>满足后记录为“有效”</span>
              </div>
              <div class="form-grid three">
                <el-form-item label="HTTP 状态码">
                  <el-input v-model="draft.success_status" placeholder="200" />
                </el-form-item>
                <el-form-item label="JSON 字段路径" required class="span-two">
                  <el-input v-model="draft.success_path" placeholder="user.id 或 identity.subject" />
                </el-form-item>
                <el-form-item label="字段判断">
                  <el-select v-model="draft.success_match" class="full-width">
                    <el-option label="字段存在且非空" value="exists" />
                    <el-option label="字段等于指定值" value="equals" />
                  </el-select>
                </el-form-item>
                <el-form-item v-if="draft.success_match === 'equals'" label="指定值" class="span-two">
                  <el-input v-model="draft.success_value" placeholder="例如 active 或 true" />
                </el-form-item>
              </div>
            </div>

            <div class="condition-box failure">
              <div class="condition-heading">
                <el-tag type="danger" effect="plain">失败</el-tag>
                <span>满足后记录为“无效”</span>
              </div>
              <div class="form-grid three">
                <el-form-item label="HTTP 状态码">
                  <el-input v-model="draft.failure_status" placeholder="401, 403" />
                </el-form-item>
                <el-form-item label="可选 JSON 字段路径" class="span-two">
                  <el-input v-model="draft.failure_path" placeholder="error.code" />
                </el-form-item>
                <el-form-item v-if="draft.failure_path" label="字段判断">
                  <el-select v-model="draft.failure_match" class="full-width">
                    <el-option label="字段存在且非空" value="exists" />
                    <el-option label="字段等于指定值" value="equals" />
                  </el-select>
                </el-form-item>
                <el-form-item v-if="draft.failure_path && draft.failure_match === 'equals'" label="指定值" class="span-two">
                  <el-input v-model="draft.failure_value" placeholder="例如 invalid_token" />
                </el-form-item>
              </div>
            </div>

            <el-form-item label="身份字段路径" class="identity-fields">
              <el-input v-model="draft.identity_fields" placeholder="user.id, user.name" />
              <small>成功响应中用于证明“是谁”的字段；多个路径用英文逗号分隔。留空表示只校验成功条件。</small>
            </el-form-item>
          </div>
        </el-form>

        <div class="endpoint-preview">
          <div class="preview-heading">
            <div>
              <span class="field-caption">配置预览</span>
              <strong>{{ validationSummary }}</strong>
            </div>
            <el-tag :type="draftValid ? 'success' : 'warning'" effect="plain">
              {{ draftValid ? "字段已基本齐全" : "还需要补充" }}
            </el-tag>
          </div>
          <p v-if="draftIssues.length" class="preview-issues">{{ draftIssues.join("；") }}</p>
          <code>{{ requestSummary }}</code>
        </div>
      </section>

      <div v-else class="endpoint-editor-empty">
        <el-icon><Link /></el-icon>
        <h3>从一个端点开始</h3>
        <p>点击左侧加号创建端点，或选择配置场景后使用模板。</p>
        <el-button type="primary" :icon="Plus" @click="createEndpoint">新增授权端点</el-button>
      </div>
    </div>

    <el-collapse class="advanced-editor">
      <el-collapse-item title="高级 JSON 编辑（已有配置可直接调整）" name="authorized-endpoints-json">
        <el-input
          v-model="rawJson"
          type="textarea"
          :rows="14"
          spellcheck="false"
          placeholder="[]"
        />
        <div class="advanced-actions">
          <span class="muted">保存前会由后端再次校验字段、HTTPS、主机白名单和响应条件。</span>
          <el-button link type="primary" @click="applyRawJson">应用 JSON 到配置向导</el-button>
        </div>
      </el-collapse-item>
    </el-collapse>
  </div>
</template>

<script setup>
import { computed, reactive, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { Delete, InfoFilled, Link, MagicStick, Plus } from "@element-plus/icons-vue";

const props = defineProps({
  modelValue: {
    type: String,
    default: "[]",
  },
});

const emit = defineEmits(["update:modelValue", "open-connections"]);
const endpoints = ref([]);
const selectedIndex = ref(-1);
const preset = ref("custom");
const rawJson = ref("[]");
const draft = reactive(emptyDraft());
let hydrating = false;

const presets = [
  {
    value: "custom",
    label: "自有系统 / 自定义平台",
  },
  {
    value: "mediawiki",
    label: "MediaWiki 自有端点模板",
  },
  {
    value: "builtin-github",
    label: "GitHub（已有内置验证器）",
  },
];

function emptyDraft() {
  return {
    endpoint_id: "",
    system_name: "",
    platform: "",
    supported_types: "",
    endpoint_url: "",
    method: "GET",
    auth_placement: "header",
    auth_name: "Authorization",
    auth_scheme: "bearer",
    success_status: "200",
    success_path: "",
    success_match: "exists",
    success_value: "",
    failure_status: "401,403",
    failure_path: "",
    failure_match: "exists",
    failure_value: "",
    identity_fields: "",
    timeout_seconds: 8,
    allowed_hosts: "",
    enabled: true,
  };
}

function splitValues(value) {
  return String(value || "")
    .split(/[\n,，]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function parseStatuses(value) {
  return splitValues(value)
    .map((item) => Number(item))
    .filter((item) => Number.isInteger(item) && item >= 100 && item <= 599);
}

function parseValue(value) {
  const text = String(value ?? "").trim();
  if (text === "true") return true;
  if (text === "false") return false;
  if (text === "null") return null;
  if (/^-?\d+(?:\.\d+)?$/.test(text)) return Number(text);
  return text;
}

function conditionToDraft(condition, fallbackStatus, fallbackMatch) {
  const source = condition || {};
  const statuses = source.http_status || source.status_codes || [];
  return {
    status: statuses.join(","),
    path: source.json_path || "",
    match: Object.prototype.hasOwnProperty.call(source, "equals")
      ? "equals"
      : fallbackMatch,
    value: Object.prototype.hasOwnProperty.call(source, "equals")
      ? String(source.equals)
      : "",
    fallbackStatus,
  };
}

function endpointToDraft(endpoint) {
  const success = conditionToDraft(endpoint.success_condition, "200", "exists");
  const failure = conditionToDraft(endpoint.failure_condition, "401,403", "exists");
  Object.assign(draft, {
    endpoint_id: endpoint.endpoint_id || "",
    system_name: endpoint.system_name || "",
    platform: endpoint.platform || "",
    supported_types: (endpoint.supported_types || []).join(", "),
    endpoint_url: endpoint.endpoint_url || "",
    method: endpoint.method || "GET",
    auth_placement: endpoint.auth_placement || "header",
    auth_name: endpoint.auth_name || "Authorization",
    auth_scheme: endpoint.auth_scheme || "bearer",
    success_status: success.status || success.fallbackStatus,
    success_path: success.path,
    success_match: success.match,
    success_value: success.value,
    failure_status: failure.status || failure.fallbackStatus,
    failure_path: failure.path,
    failure_match: failure.match,
    failure_value: failure.value,
    identity_fields: (endpoint.identity_fields || []).join(", "),
    timeout_seconds: endpoint.timeout_seconds ?? 8,
    allowed_hosts: (endpoint.allowed_hosts || []).join(", "),
    enabled: endpoint.enabled !== false,
  });
}

function buildCondition(status, path, match, value, required) {
  const condition = {};
  const statuses = parseStatuses(status);
  if (statuses.length) condition.http_status = statuses;
  const jsonPath = String(path || "").trim();
  if (jsonPath) {
    condition.json_path = jsonPath;
    if (match === "equals") condition.equals = parseValue(value);
    else condition.exists = true;
  }
  if (required && !condition.json_path) return null;
  return condition;
}

function draftToEndpoint() {
  return {
    endpoint_id: String(draft.endpoint_id || "").trim(),
    system_name: String(draft.system_name || "").trim(),
    platform: String(draft.platform || "").trim(),
    supported_types: splitValues(draft.supported_types),
    endpoint_url: String(draft.endpoint_url || "").trim(),
    method: draft.method,
    auth_placement: draft.auth_placement,
    auth_name: String(draft.auth_name || "").trim(),
    auth_scheme: draft.auth_scheme,
    success_condition: buildCondition(
      draft.success_status,
      draft.success_path,
      draft.success_match,
      draft.success_value,
      true,
    ) || {},
    failure_condition: buildCondition(
      draft.failure_status,
      draft.failure_path,
      draft.failure_match,
      draft.failure_value,
      false,
    ) || {},
    identity_fields: splitValues(draft.identity_fields),
    timeout_seconds: Number(draft.timeout_seconds) || 8,
    allowed_hosts: splitValues(draft.allowed_hosts),
    enabled: Boolean(draft.enabled),
  };
}

function syncRawAndParent() {
  rawJson.value = JSON.stringify(endpoints.value, null, 2);
  emit("update:modelValue", rawJson.value);
}

function syncDraft() {
  if (hydrating || selectedIndex.value < 0) return;
  endpoints.value[selectedIndex.value] = draftToEndpoint();
  syncRawAndParent();
}

function loadExternal(value) {
  let parsed = [];
  try {
    const candidate = JSON.parse(value || "[]");
    if (Array.isArray(candidate)) parsed = candidate;
  } catch {
    rawJson.value = value || "[]";
    return;
  }
  hydrating = true;
  endpoints.value = parsed;
  rawJson.value = JSON.stringify(parsed, null, 2);
  if (selectedIndex.value >= parsed.length) selectedIndex.value = parsed.length ? 0 : -1;
  if (selectedIndex.value >= 0) endpointToDraft(parsed[selectedIndex.value]);
  hydrating = false;
}

function selectEndpoint(index) {
  selectedIndex.value = index;
  hydrating = true;
  endpointToDraft(endpoints.value[index]);
  hydrating = false;
}

function createEndpoint() {
  const item = draftToEndpoint();
  const next = {
    ...item,
    endpoint_id: "",
    system_name: "",
    platform: "",
    supported_types: [],
    endpoint_url: "",
    success_condition: { http_status: [200] },
    failure_condition: { http_status: [401, 403] },
    identity_fields: [],
    allowed_hosts: [],
  };
  endpoints.value.push(next);
  selectedIndex.value = endpoints.value.length - 1;
  hydrating = true;
  endpointToDraft(next);
  hydrating = false;
  syncRawAndParent();
}

function removeEndpoint() {
  if (selectedIndex.value < 0) return;
  endpoints.value.splice(selectedIndex.value, 1);
  selectedIndex.value = endpoints.value.length ? Math.min(selectedIndex.value, endpoints.value.length - 1) : -1;
  if (selectedIndex.value >= 0) {
    hydrating = true;
    endpointToDraft(endpoints.value[selectedIndex.value]);
    hydrating = false;
  }
  syncRawAndParent();
}

function applyPreset() {
  if (preset.value === "builtin-github") {
    emit("open-connections");
    return;
  }
  const template = preset.value === "mediawiki"
    ? {
        endpoint_id: "mediawiki-token-check",
        system_name: "自有 MediaWiki 验证端点",
        platform: "MediaWiki",
        supported_types: "config_assignment_secret",
        auth_placement: "header",
        auth_name: "Authorization",
        auth_scheme: "bearer",
        success_path: "user.id",
        identity_fields: "user.id, user.name",
        failure_status: "401,403",
      }
    : emptyDraft();
  if (selectedIndex.value < 0) createEndpoint();
  hydrating = true;
  Object.assign(draft, emptyDraft(), template);
  hydrating = false;
  syncDraft();
}

function applyRawJson() {
  try {
    const parsed = JSON.parse(rawJson.value || "[]");
    if (!Array.isArray(parsed)) throw new Error("not-array");
    hydrating = true;
    endpoints.value = parsed;
    selectedIndex.value = parsed.length ? 0 : -1;
    if (selectedIndex.value >= 0) endpointToDraft(parsed[0]);
    hydrating = false;
    rawJson.value = JSON.stringify(parsed, null, 2);
    emit("update:modelValue", rawJson.value);
  } catch {
      ElMessage.error("JSON 格式不正确，无法应用到配置向导");
  }
}

const draftIssues = computed(() => {
  const issues = [];
  if (!draft.endpoint_id) issues.push("缺少端点标识");
  if (!draft.system_name || !draft.platform) issues.push("缺少系统或平台标识");
  if (!splitValues(draft.supported_types).length) issues.push("至少填写一个凭据类型");
  if (!/^https:\/\//i.test(String(draft.endpoint_url || ""))
    && !/^http:\/\/(localhost|127\.0\.0\.1|\[::1\])(?::\d+)?(?:\/|$)/i.test(String(draft.endpoint_url || ""))) {
    issues.push("端点 URL 需要使用 HTTPS 或本机 HTTP");
  }
  if (!String(draft.success_path || "").trim()) issues.push("成功条件必须填写 JSON 字段路径");
  if (!splitValues(draft.allowed_hosts).length) issues.push("需要填写主机白名单");
  return issues;
});

const draftValid = computed(() => draftIssues.value.length === 0);
const validationSummary = computed(() => draftValid.value
  ? "保存后可按当前契约执行身份验证"
  : "补齐必填字段后再保存");
const requestSummary = computed(() => {
  const endpoint = draftToEndpoint();
  return JSON.stringify({
    method: endpoint.method,
    endpoint_url: endpoint.endpoint_url || "待填写",
    auth: `${endpoint.auth_placement}:${endpoint.auth_name} (${endpoint.auth_scheme})`,
    success_condition: endpoint.success_condition,
    failure_condition: endpoint.failure_condition,
    identity_fields: endpoint.identity_fields,
  }, null, 2);
});

watch(() => props.modelValue, (value) => {
  const normalized = String(value || "[]");
  if (normalized !== rawJson.value) loadExternal(normalized);
}, { immediate: true });

watch(draft, syncDraft, { deep: true });
</script>

<style scoped>
.endpoint-builder { display: grid; gap: 16px; }
.endpoint-guide,
.endpoint-list-panel,
.endpoint-editor-panel,
.endpoint-editor-empty {
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-sm);
  background: #fbfdff;
}
.endpoint-guide { padding: 16px; }
.guide-heading { display: flex; gap: 10px; align-items: flex-start; color: #294a6b; }
.guide-heading .el-icon { margin-top: 1px; color: #3d80b4; font-size: 18px; }
.guide-heading strong { display: block; font-size: 14px; }
.guide-heading p { margin: 5px 0 0; color: #718097; font-size: 12px; line-height: 1.55; }
.guide-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-top: 15px; }
.guide-step { display: grid; grid-template-columns: 26px minmax(0, 1fr); gap: 9px; padding: 12px; border: 1px solid #e2ebf3; border-radius: var(--radius-sm); background: #fff; }
.guide-index { display: grid; place-items: center; width: 24px; height: 24px; border-radius: 50%; background: #e8f2fb; color: #2b6c9f; font-size: 12px; font-weight: 760; }
.guide-step strong { color: #334961; font-size: 12px; }
.guide-step p { margin: 5px 0 2px; color: #7a8899; font-size: 11px; line-height: 1.5; }
.guide-step code { color: #35688f; font-family: var(--el-font-family); font-size: 11px; }
.guide-step :deep(.el-button) { height: auto; padding: 0; font-size: 11px; }
.builder-toolbar { display: flex; align-items: flex-end; justify-content: space-between; gap: 14px; padding: 13px 0 0; }
.builder-toolbar > div { display: grid; gap: 6px; }
.field-caption { color: #7b8a9e; font-size: 11px; font-weight: 720; letter-spacing: .04em; text-transform: uppercase; }
.preset-select { width: 300px; }
.preset-alert { margin-top: 2px; }
.endpoint-workspace { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.endpoint-editor-panel { order: 1; }
.endpoint-editor-empty { order: 1; }
.endpoint-list-panel { order: 2; padding: 12px; }
.panel-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 2px 2px 10px; border-bottom: 1px solid var(--line-soft); }
.panel-heading > div { display: grid; gap: 3px; }
.panel-heading strong { color: #344a63; font-size: 13px; }
.panel-heading span { color: #8491a2; font-size: 11px; }
.endpoint-list-item { display: grid; width: 100%; gap: 5px; margin-top: 8px; padding: 10px; border: 1px solid transparent; border-radius: var(--radius-sm); background: transparent; color: inherit; text-align: left; cursor: pointer; }
.endpoint-list-item:hover { border-color: #cbddea; background: #f5faff; }
.endpoint-list-item.active { border-color: #8eb9d8; background: #edf6fc; box-shadow: inset 3px 0 #347cad; }
.endpoint-list-title { display: flex; align-items: center; justify-content: space-between; gap: 6px; min-width: 0; }
.endpoint-list-title strong { min-width: 0; overflow: hidden; color: #3a4f67; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.endpoint-list-meta { color: #66809b; font-size: 11px; }
.endpoint-list-item small { overflow-wrap: anywhere; color: #8794a5; font-size: 11px; line-height: 1.4; }
.endpoint-list-empty { display: grid; place-items: center; gap: 8px; min-height: 180px; padding: 20px 10px; color: #8492a4; text-align: center; }
.endpoint-list-empty .el-icon { color: #91b1ca; font-size: 24px; }
.endpoint-list-empty strong { color: #5f7186; font-size: 12px; }
.endpoint-list-empty span { font-size: 11px; line-height: 1.5; }
.endpoint-editor-panel { min-width: 0; padding: 16px; background: #fff; }
.editor-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding-bottom: 14px; border-bottom: 1px solid var(--line-soft); }
.editor-heading h3 { margin: 5px 0 0; color: #2e435d; font-size: 17px; font-weight: 760; }
.editor-actions { display: flex; align-items: center; gap: 14px; }
.endpoint-form { padding-top: 15px; }
.form-section + .form-section { margin-top: 18px; padding-top: 18px; border-top: 1px solid var(--line-soft); }
.form-section-heading { display: flex; gap: 9px; align-items: flex-start; margin-bottom: 13px; }
.section-number { display: grid; place-items: center; width: 22px; height: 22px; border-radius: 5px; background: #eaf3fa; color: #3776a1; font-size: 11px; font-weight: 780; }
.form-section-heading strong { color: #3c526c; font-size: 13px; }
.form-section-heading p { margin: 4px 0 0; color: #8491a2; font-size: 11px; line-height: 1.45; }
.form-grid { display: grid; gap: 12px; }
.form-grid.two { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.form-grid.three { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.form-grid :deep(.el-form-item) { min-width: 0; margin-bottom: 0; }
.form-grid :deep(.el-form-item__label), .identity-fields :deep(.el-form-item__label) { height: auto; margin-bottom: 5px; padding: 0; color: #596a7f; font-size: 12px; font-weight: 680; line-height: 1.4; }
.form-grid small, .identity-fields small { display: block; margin-top: 5px; color: #8895a5; font-size: 11px; line-height: 1.45; }
.span-two { grid-column: span 2; }
.full-width { width: 100%; }
.condition-box { margin-top: 10px; padding: 13px; border: 1px solid var(--line-soft); border-radius: var(--radius-sm); }
.condition-box.success { background: #f7fcf9; border-color: #d9eee1; }
.condition-box.failure { background: #fffafa; border-color: #f0dde1; }
.condition-heading { display: flex; align-items: center; gap: 8px; margin-bottom: 11px; color: #66758a; font-size: 11px; }
.identity-fields { margin: 13px 0 0; }
.endpoint-preview { margin-top: 16px; padding: 13px; border: 1px solid #dbe7f0; border-radius: var(--radius-sm); background: #f7fbfe; }
.preview-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.preview-heading > div { display: grid; gap: 4px; }
.preview-heading strong { color: #3e576f; font-size: 12px; }
.preview-issues { margin: 8px 0; color: #9b6b26; font-size: 11px; line-height: 1.5; }
.endpoint-preview code { display: block; margin-top: 9px; padding: 10px; overflow: auto; border: 1px solid #e2ebf2; border-radius: 4px; background: #fff; color: #4f6378; font-family: var(--el-font-family-monospace, monospace); font-size: 11px; line-height: 1.55; white-space: pre-wrap; overflow-wrap: anywhere; }
.endpoint-editor-empty { display: grid; place-items: center; min-height: 420px; padding: 30px; background: #fbfdff; text-align: center; }
.endpoint-editor-empty .el-icon { color: #91b1ca; font-size: 32px; }
.endpoint-editor-empty h3 { margin: 10px 0 3px; color: #50657c; font-size: 15px; }
.endpoint-editor-empty p { margin: 0 0 15px; color: #8491a2; font-size: 12px; }
.advanced-editor { border-top: 1px solid var(--line-soft); }
.advanced-actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 8px; }
.advanced-actions .muted { font-size: 11px; }
@media (max-width: 1050px) {
  .guide-grid { grid-template-columns: 1fr; }
  .form-grid.three { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 760px) {
  .builder-toolbar { align-items: stretch; flex-direction: column; }
  .preset-select { width: 100%; }
  .endpoint-list-panel { min-height: 0; }
  .endpoint-list-empty { min-height: 120px; }
  .editor-heading { align-items: flex-start; flex-direction: column; }
  .form-grid.two, .form-grid.three { grid-template-columns: 1fr; }
  .span-two { grid-column: auto; }
  .advanced-actions { align-items: flex-start; flex-direction: column; }
}
</style>
