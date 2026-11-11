<template>
  <AppLayout>
    <PageHeader title="验证中心">
      <el-button type="primary" @click="process">处理排队任务</el-button>
      <el-button @click="load">刷新</el-button>
    </PageHeader>

    <MetricGrid :items="metrics" :show-hints="false" />

    <el-alert
      v-if="!hasEnabledEndpoint"
      title="当前没有配置明确授权验证端点。没有平台专用验证器的发现项只能完成本地证据校验，状态会保持为“未配置授权端点”。"
      type="warning"
      :closable="false"
      show-icon
      class="verification-config-alert"
    >
      <template #default>
        <el-button link type="primary" @click="goToVerificationSettings">前往配置</el-button>
      </template>
    </el-alert>

    <el-card v-else shadow="never" class="verification-endpoints-card">
      <template #header>
        <div class="verification-endpoints-heading">
          <span>明确授权验证端点</span>
          <el-button link type="primary" @click="goToVerificationSettings">管理配置</el-button>
        </div>
      </template>
      <el-table :data="endpointConfigs" size="small">
        <el-table-column prop="system_name" label="系统" />
        <el-table-column prop="platform" label="平台" />
        <el-table-column prop="endpoint_id" label="端点标识" />
        <el-table-column label="支持类型" min-width="220">
          <template #default="{ row }">{{ (row.supported_types || []).join("、") }}</template>
        </el-table-column>
        <el-table-column label="配置状态" width="130">
          <template #default="{ row }">
            <StateTag :value="row.enabled ? 'enabled' : 'disabled'" />
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card shadow="never">
      <template #header>验证队列</template>
      <el-table v-loading="loading" :data="jobPageItems">
        <el-table-column prop="id" label="任务" />
        <el-table-column prop="credential_id" label="发现项" />
        <el-table-column prop="credential_type" label="类型" />
        <el-table-column prop="verifier_id" label="验证器" />
        <el-table-column prop="requested_by" label="请求来源" />
        <el-table-column label="状态">
          <template #default="{ row }"><StateTag :value="statusLabel(row.status)" /></template>
        </el-table-column>
        <el-table-column label="创建时间">
          <template #default="{ row }">{{ formatTimestamp(row.created_at) }}</template>
        </el-table-column>
      </el-table>
      <PaginationBar
        :page="jobPagination.pagination.page"
        :page-size="jobPagination.pagination.page_size"
        :page-sizes="verificationPageSizes"
        :total="jobPagination.pagination.total_items"
        @update:page="jobPagination.setPage"
        @update:page-size="jobPagination.setPageSize"
      />
    </el-card>

    <el-card shadow="never">
      <template #header>验证历史</template>
      <el-table :data="historyPageItems">
        <el-table-column prop="credential_id" label="发现项" />
        <el-table-column prop="verifier_id" label="验证器" />
        <el-table-column prop="version" label="版本" />
        <el-table-column label="状态">
          <template #default="{ row }"><StateTag :value="statusLabel(row.status)" /></template>
        </el-table-column>
        <el-table-column label="证据摘要" min-width="260">
          <template #default="{ row }">{{ verificationReason(row) }}</template>
        </el-table-column>
        <el-table-column label="时间">
          <template #default="{ row }">{{ formatTimestamp(row.created_at) }}</template>
        </el-table-column>
      </el-table>
      <PaginationBar
        :page="historyPagination.pagination.page"
        :page-size="historyPagination.pagination.page_size"
        :page-sizes="verificationPageSizes"
        :total="historyPagination.pagination.total_items"
        @update:page="historyPagination.setPage"
        @update:page-size="historyPagination.setPageSize"
      />
    </el-card>
  </AppLayout>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import AppLayout from "../components/AppLayout.vue";
import PageHeader from "../components/PageHeader.vue";
import MetricGrid from "../components/MetricGrid.vue";
import PaginationBar from "../components/PaginationBar.vue";
import StateTag from "../components/StateTag.vue";
import * as api from "../api/findings";
import * as settingsApi from "../api/settings";
import { formatTimestamp } from "../utils/format";
import { slicePage, useUrlPagination } from "../composables/useUrlPagination";

const jobs = ref([]);
const history = ref([]);
const loading = ref(false);
const endpointConfigs = ref([]);
const router = useRouter();
const hasEnabledEndpoint = computed(() => endpointConfigs.value.some((item) => item.enabled));
const verificationPageSizes = [5, 10, 20, 50];
const jobPagination = useUrlPagination({ prefix: "verificationJobs", defaultPageSize: 5, pageSizes: verificationPageSizes });
const historyPagination = useUrlPagination({ prefix: "verificationHistory", defaultPageSize: 5, pageSizes: verificationPageSizes });
const jobPageItems = computed(() => slicePage(jobs.value, jobPagination.pagination).items);
const historyPageItems = computed(() => slicePage(history.value, historyPagination.pagination).items);
const latestHistoryByCredential = computed(() => {
  const latest = new Map();
  history.value.forEach((item) => {
    const credentialId = String(item.credential_id || "");
    if (credentialId && !latest.has(credentialId)) latest.set(credentialId, item);
  });
  return [...latest.values()];
});
const statusLabels = {
  queued: "排队中",
  running: "运行中",
  done: "已完成",
  finished: "已完成",
  valid: "有效",
  controlled_valid: "受控样例成功",
  invalid: "无效",
  inconclusive: "无法判断",
  not_configured: "未配置授权端点",
  not_requested: "未执行身份验证",
  unsupported: "不支持该类型",
  error: "失败",
  network_error: "网络错误",
  missing_context: "缺少配对凭据",
  rate_limited: "服务端限流",
};
const providerLabels = {
  GitHub: "GitHub",
  Gitee: "Gitee",
  GitLab: "GitLab",
  "Docker Hub": "Docker Hub",
  npm: "npm",
  "Hugging Face": "Hugging Face",
};
const providerReasonLabels = {
  finding_credential_differs_from_configured_token: {
    GitHub: "GitHub 返回 401：当前验证 Token 与这条发现项不是同一条凭据；单独的身份测试通过不代表该发现项通过",
    Gitee: "Gitee 返回 401：当前验证 Token 与这条发现项不是同一条凭据；单独的身份测试通过不代表该发现项通过",
  },
  provider_rejected_credentials: {
    GitHub: "GitHub 返回 401：凭据未被接受，可能是 Token 错误、过期、撤销或认证头不符合要求",
    Gitee: "Gitee 返回 401：访问令牌未被接受，可能是 Token 错误、过期、撤销或认证参数不符合要求",
    GitLab: "GitLab 返回 401：访问令牌未被接受，可能是 Token 错误、过期或撤销",
    "Docker Hub": "Docker Hub 返回 401：PAT 未被接受，可能是 Token 错误、过期、撤销或用户名不匹配",
  },
  provider_forbidden: {
    GitHub: "GitHub 返回 403：请求被拒绝，可能是权限不足或安全策略限制",
    Gitee: "Gitee 返回 403：请求被拒绝，可能是权限不足或访问策略限制",
    GitLab: "GitLab 返回 403：请求被拒绝，可能是权限不足或访问策略限制",
  },
  provider_rate_limited: {
    GitHub: "GitHub 返回限流：请稍后重试",
    Gitee: "Gitee 返回限流：请稍后重试",
    GitLab: "GitLab 返回限流：请稍后重试",
  },
  provider_unavailable: {},
  provider_rejected_request: {},
  npm_whoami_endpoint_not_found: {
    npm: "npm 身份确认端点返回 404：请确认使用的是 npmjs.org 的 Token；当前系统只验证公共 npm 注册表，不支持把其他私有注册表 Token 当作 npmjs Token 验证",
  },
};
function providerName(row) {
  if (row.evidence?.provider) return providerLabels[row.evidence.provider] || row.evidence.provider;
  const endpoint = row.evidence?.endpoint || "";
  return Object.keys(providerLabels).find((name) => endpoint.includes(name.toLowerCase().replace(" ", ""))) || "服务端";
}
function statusLabel(value) {
  return statusLabels[value] || value || "未请求";
}
function verificationReason(row) {
  if (row.evidence?.configured_token === true && row.status === "valid") {
    return `${providerName(row)} 身份确认通过：服务端接受当前验证 Token（HTTP ${row.evidence.http_status || 200}）；这是即时测试，不会写入发现项历史`;
  }
  if (row.status === "controlled_valid") {
    return "受控样例验证成功：HMAC 指纹与 TestDemo 受控清单匹配，未向第三方平台发送请求；不代表真实平台凭据有效";
  }
  if (row.evidence?.configured_token === true && row.status === "invalid") {
    return `${providerName(row)} 身份确认失败：服务端拒绝当前验证 Token${row.evidence?.http_status ? `（HTTP ${row.evidence.http_status}）` : ""}`;
  }
  const reason = row.evidence?.reason || row.reason || "";
  const provider = providerName(row);
  if (
    reason === "provider_rejected_credentials"
    && row.evidence?.configured_token_match === true
  ) {
    return `${provider} 返回 401：当前验证 Token 与发现项凭据一致，但服务端仍拒绝认证，需检查 Token 状态或认证参数`;
  }
  const label = providerReasonLabels[reason]?.[provider]
    || ({
      provider_unavailable: `${provider} 服务暂时不可用：请稍后重试`,
      provider_rejected_request: `${provider} 拒绝了这次验证请求`,
    }[reason])
    || ({
      network_error: "验证接口网络错误",
      transport_error: `${provider} 请求未收到有效响应：可能是网络、DNS、代理、防火墙或超时问题，不代表 Token 无效`,
      transport_timeout: `${provider} 验证请求超时：无法判断 Token 状态`,
      verification_network_disabled: "验证联网未开启",
      verification_fingerprint_not_authorized: "指纹未加入授权白名单",
      no_minimal_verifier: "该凭据类型没有配置最小身份验证器",
      authorized_endpoint_not_configured: `已完成本地证据校验，但未配置授权验证端点（类型：${row.evidence?.credential_type || "未提供"}；平台：${row.evidence?.platform || "未提供"}）。请到“系统设置 → 凭据验证”配置自有系统或平台的明确授权验证端点`,
      requires_authorized_endpoint: `已完成本地证据校验，但未配置授权验证端点（类型：${row.evidence?.credential_type || "未提供"}；平台：${row.evidence?.platform || "未提供"}）。请到“系统设置 → 凭据验证”配置自有系统或平台的明确授权验证端点`,
      authorized_endpoint_success_condition_matched: "授权端点返回了满足配置条件的身份响应",
      authorized_endpoint_failure_condition_matched: "授权端点返回了满足配置条件的失败身份响应",
      authorized_endpoint_timeout: "授权验证端点请求超时，无法判断凭据状态",
      authorized_endpoint_unavailable: "授权验证端点暂不可用，无法判断凭据状态",
      authorized_endpoint_response_unexpected: "授权验证端点返回了不符合 JSON 契约的响应，无法判断凭据状态",
      authorized_endpoint_conditions_not_matched: "授权端点响应未满足已配置的成功或失败条件，无法判断凭据状态",
      authorized_endpoint_redirect_not_allowed: "授权验证端点发生跳转，出于安全限制未跟随，无法判断凭据状态",
      authorized_endpoint_identity_fields_missing: "授权端点缺少配置要求的身份字段，无法判断凭据状态",
      provider_response_missing_identity: "平台返回成功状态码但未提供配置要求的身份字段，无法判断凭据状态",
      missing_dockerhub_username: "Docker Hub 验证需要配置与 PAT 对应的用户名",
      missing_twilio_key_pair: "Twilio 需要同时提供 Account SID 和 Auth Token",
      missing_aws_key_pair: "AWS 需要同时提供 Access Key ID 和 Secret Access Key",
    }[reason])
    || reason;
  if (label) {
    return row.evidence?.http_status ? `${label}（HTTP ${row.evidence.http_status}）` : label;
  }
  return row.evidence?.http_status
    ? `服务端返回 HTTP ${row.evidence.http_status}，未提供额外说明`
    : "未提供额外说明";
}
const metrics = computed(() => {
  const statuses = jobs.value.reduce((out, item) => {
    out[item.status] = (out[item.status] || 0) + 1;
    return out;
  }, {});
  const latest = latestHistoryByCredential.value;
  const valid = latest.filter(
    (item) => item.status === "valid" && item.source_kind === "真实检测结果",
  ).length;
  const controlledValid = latest.filter(
    (item) => (
      ["valid", "controlled_valid"].includes(item.status)
      && item.source_kind !== "真实检测结果"
    ),
  ).length;
  const invalid = latest.filter((item) => item.status === "invalid").length;
  const inconclusive = latest.filter((item) => item.status === "inconclusive").length;
  return [
    { label: "排队", value: statuses.queued || 0, hint: "待处理任务" },
    { label: "无法判断", value: inconclusive, hint: "按每个凭据的最新结果统计，不等于失效" },
    { label: "验证通过", value: valid + controlledValid, hint: `真实有效 ${valid} · 受控来源成功 ${controlledValid}` },
    { label: "无效", value: invalid, hint: "按每个凭据的最新结果统计，仅在有依据时记录" },
  ];
});

async function load() {
  loading.value = true;
  try {
    const [jobsResponse, historyResponse, settingsResponse] = await Promise.all([
      api.getJobs(),
      api.getHistory(),
      settingsApi.getSettings(),
    ]);
    jobs.value = jobsResponse.items || [];
    history.value = historyResponse.items || [];
    endpointConfigs.value = settingsResponse.runtime?.authorized_verification_endpoints || [];
    jobPagination.pagination.total_items = jobs.value.length;
    jobPagination.pagination.total_pages = jobs.value.length
      ? Math.ceil(jobs.value.length / jobPagination.pagination.page_size)
      : 0;
    historyPagination.pagination.total_items = history.value.length;
    historyPagination.pagination.total_pages = history.value.length
      ? Math.ceil(history.value.length / historyPagination.pagination.page_size)
      : 0;
  } finally {
    loading.value = false;
  }
}

function goToVerificationSettings() {
  router.push("/settings#verification");
}

async function process() {
  const result = await api.processJobs();
  const processed = Number(result?.processed || 0);
  if (!processed) {
    ElMessage.info("当前没有待处理的验证任务");
  } else {
    const valid = Number(result?.valid || 0);
    const controlledValid = Number(result?.controlled_valid || 0);
    const invalid = Number(result?.invalid || 0);
    const inconclusive = Number(result?.inconclusive || 0);
    const notConfigured = Number(result?.not_configured || 0);
    ElMessage.success(`验证完成：${processed} 条，真实有效 ${valid}，受控成功 ${controlledValid}，无效 ${invalid}，无法判断 ${inconclusive}，未配置端点 ${notConfigured}`);
  }
  await load();
}

onMounted(load);
</script>
