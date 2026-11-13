<template>
  <AppLayout>
    <PageHeader
      title="统一发现项"
      description="以凭据实体为主视图，展示统一发现、唯一指纹、复核和验证状态。"
    >
      <el-button @click="load">刷新</el-button>
      <el-button
        type="primary"
        :disabled="!selected.length"
        @click="batchConfirm"
      >批量确认{{ selected.length ? `（${selected.length}）` : "" }}</el-button>
      <el-button
        type="success"
        :disabled="!selected.length"
        @click="batchVerify"
      >批量验证{{ selected.length ? `（${selected.length}）` : "" }}</el-button>
      <el-button type="primary" @click="exportData">导出脱敏 JSON</el-button>
    </PageHeader>

    <nav class="section-nav" aria-label="发现项分区">
      <a href="#finding-summary">计数概览</a>
      <a href="#finding-filters">筛选条件</a>
      <a href="#finding-list">发现项列表</a>
    </nav>

    <section id="finding-summary" class="page-section">
      <div class="sticky-summary">
        <MetricGrid :items="summaryItems" :show-hints="false" />
      </div>
    </section>

    <section id="finding-filters" class="page-section">
      <el-card shadow="never" class="filter-card">
        <el-form
          :model="filters"
          inline
          class="filter-toolbar findings-filter-toolbar"
          @submit.prevent="applyFilters"
        >
          <el-form-item label="关键词">
            <el-input v-model="filters.q" clearable placeholder="ID、类型、厂商或脱敏候选" />
          </el-form-item>
          <el-form-item label="类型">
            <el-input v-model="filters.type" clearable placeholder="凭据类型" />
          </el-form-item>
          <el-form-item label="复核">
            <el-select v-model="filters.review" clearable placeholder="全部复核状态">
              <el-option label="待复核" value="pending" />
              <el-option label="已确认" value="confirmed" />
              <el-option label="误报" value="false_positive" />
              <el-option label="已忽略" value="ignored" />
            </el-select>
          </el-form-item>
          <el-form-item label="验证">
            <el-select v-model="filters.verify" clearable placeholder="全部验证状态">
              <el-option label="未请求" value="not_requested" />
              <el-option label="未配置授权端点" value="not_configured" />
              <el-option label="排队中" value="queued" />
              <el-option label="无法判断" value="inconclusive" />
              <el-option label="有效" value="valid" />
              <el-option label="无效" value="invalid" />
            </el-select>
          </el-form-item>
          <el-form-item label="来源">
            <el-select v-model="filters.source_kind" placeholder="真实检测结果">
              <el-option label="真实检测结果" value="真实检测结果" />
              <el-option label="全部来源" value="全部来源" />
              <el-option label="受控投放检出" value="受控投放检出" />
              <el-option label="离线授权导入" value="离线授权导入" />
              <el-option label="合成样例" value="合成样例" />
              <el-option label="桩数据" value="桩数据" />
            </el-select>
          </el-form-item>
          <el-form-item class="filter-actions">
            <el-button type="primary" native-type="submit">应用筛选</el-button>
            <el-button @click="resetFilters">重置</el-button>
          </el-form-item>
        </el-form>
      </el-card>
    </section>

    <section id="finding-list" class="page-section">
      <el-card shadow="never">
        <template #header>
          <div class="card-heading">
            <span>凭据实体</span>
          </div>
        </template>
        <el-table
          v-loading="loading"
          :data="items"
          class="findings-table"
          row-key="id"
          empty-text="当前筛选条件下没有发现项"
          @selection-change="selected = $event"
        >
          <el-table-column type="selection" width="48" />
          <el-table-column prop="id" label="ID" width="70" />
          <el-table-column prop="type" label="类型" min-width="170" />
          <el-table-column prop="preview" label="脱敏候选" min-width="170" />
          <el-table-column label="HMAC 指纹" min-width="220">
            <template #default="{ row }">
              <div class="fingerprint-cell">
                <span class="fingerprint-value" :title="row.fingerprint">{{ row.fingerprint }}</span>
                <el-button
                  link
                  :icon="CopyDocument"
                  title="复制 HMAC 指纹"
                  aria-label="复制 HMAC 指纹"
                  @click.stop="copyFingerprint(row.fingerprint)"
                />
              </div>
            </template>
          </el-table-column>
          <el-table-column prop="confidence" label="置信度" width="90" />
          <el-table-column prop="occurrence_count" label="出现次数" width="95" />
          <el-table-column label="公开时间" min-width="175">
            <template #default="{ row }">{{ formatTimestamp(row.published_at, "未知") }}</template>
          </el-table-column>
          <el-table-column label="复核" width="105">
            <template #default="{ row }"><StateTag :value="row.review_status" kind="review" /></template>
          </el-table-column>
          <el-table-column label="验证" width="160">
            <template #default="{ row }"><StateTag :value="row.verification_status" kind="verification" /></template>
          </el-table-column>
          <el-table-column label="操作" width="160" fixed="right" align="right">
            <template #default="{ row }">
              <div class="finding-actions">
                <el-button link @click="open(row)">详情</el-button>
                <el-button
                  link
                  type="primary"
                  title="确认该发现项为真实公开暴露；受控或离线来源会记录为受控环境确认"
                  @click="review(row, 'confirm')"
                >确认</el-button>
                <el-button link type="success" @click="verify(row)">验证</el-button>
              </div>
            </template>
          </el-table-column>
        </el-table>
        <PaginationBar
          :page="pagination.pagination.page"
          :page-size="pagination.pagination.page_size"
          :total="pagination.pagination.total_items"
          @update:page="onPage"
          @update:page-size="onPageSize"
        />
      </el-card>
    </section>

    <el-drawer v-model="drawer" title="发现项详情" size="560px">
      <el-skeleton v-if="detailLoading" :rows="8" animated />
      <template v-else>
        <el-descriptions :column="1" border>
          <el-descriptions-item
            v-for="(value, key) in detailFields"
            :key="key"
            :label="key"
          >
            {{ value }}
          </el-descriptions-item>
        </el-descriptions>
        <div v-if="detail.fingerprint" class="fingerprint-copy-row">
          <span class="muted">HMAC 指纹</span>
          <el-button
            type="primary"
            plain
            :icon="CopyDocument"
            @click="copyFingerprint(detail.fingerprint)"
          >复制指纹</el-button>
        </div>
        <el-collapse class="raw-json-collapse">
          <el-collapse-item title="查看原始响应结构（脱敏）" name="finding-json">
            <pre class="json-block">{{ JSON.stringify(detailForDisplay, null, 2) }}</pre>
          </el-collapse-item>
        </el-collapse>
      </template>
    </el-drawer>
  </AppLayout>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from "vue";
import { CopyDocument } from "@element-plus/icons-vue";
import { ElMessage } from "element-plus";
import { useRoute, useRouter } from "vue-router";
import AppLayout from "../components/AppLayout.vue";
import PageHeader from "../components/PageHeader.vue";
import MetricGrid from "../components/MetricGrid.vue";
import PaginationBar from "../components/PaginationBar.vue";
import StateTag from "../components/StateTag.vue";
import * as api from "../api/findings";
import { displayState, downloadJson, formatTimestamp } from "../utils/format";
import { useUrlPagination } from "../composables/useUrlPagination";

const items = ref([]);
const loading = ref(false);
const drawer = ref(false);
const detail = ref({});
const detailLoading = ref(false);
const selected = ref([]);
const route = useRoute();
const router = useRouter();
const filters = reactive({
  q: String(route.query.q || ""),
  type: String(route.query.type || ""),
  review: String(route.query.review || ""),
  verify: String(route.query.verify || ""),
  source_kind: String(route.query.source_kind || "真实检测结果"),
});
const pagination = useUrlPagination();
const summary = ref({});

const summaryItems = computed(() => [
  {
    label: "统一发现项",
    value: summary.value.unified_finding_count ?? 0,
    hint: "occurrences 出现位置总数",
  },
  {
    label: "唯一发现项",
    value: summary.value.unique_finding_count ?? 0,
    hint: "credentials 指纹去重",
  },
  {
    label: "待复核",
    value: summary.value.pending_review_count ?? 0,
    hint: "review_status=pending",
  },
  {
    label: "待验证",
    value: summary.value.pending_verification_count ?? 0,
    hint: "未请求、排队中或运行中",
  },
]);

const hiddenDetailFields = new Set([
  "first_discovered_at",
  "first_discovery_time_status",
  "first_discovery_unknown_reason",
  "discovery_latency",
  "discovery_latency_seconds",
  "discovery_latency_status",
  "discovery_latency_reason",
]);

function fieldsForDisplay(source) {
  return Object.fromEntries(
    Object.entries(source)
      .filter(([key]) => (
        !["locations", "review_logs", "verification_history"].includes(key)
        && !hiddenDetailFields.has(key)
      ))
      .map(([key, value]) => [key, formatValue(value, key)]),
  );
}

const detailFields = computed(() => fieldsForDisplay(detail.value || {}));
const detailForDisplay = computed(() => fieldsForDisplay(detail.value || {}));

function formatValue(value, key = "") {
  if (value === null || value === undefined || value === "") return "未提供";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  if (["review_status", "review"].includes(key)) return displayState(value, "review");
  if (["verification_status", "verify"].includes(key)) return displayState(value, "verification");
  if (["risk_level", "severity"].includes(key)) return displayState(value, "risk");
  return String(value);
}

async function load() {
  loading.value = true;
  try {
    const data = await api.getFindings({
      ...filters,
      page: pagination.pagination.page,
      page_size: pagination.pagination.page_size,
    });
    items.value = data.items || [];
    pagination.applyResponse(data.pagination || {
      page: data.page || 1,
      page_size: data.page_size || 20,
      total_items: data.total || 0,
      total_pages: data.total ? Math.ceil(data.total / (data.page_size || 20)) : 0,
    });
    summary.value = data.summary || {};
  } finally {
    loading.value = false;
  }
}

async function applyFilters() {
  const nextQuery = { ...route.query };
  delete nextQuery.platform;
  await router.replace({
    query: {
      ...nextQuery,
      q: filters.q || undefined,
      type: filters.type || undefined,
      review: filters.review || undefined,
      verify: filters.verify || undefined,
      source_kind: filters.source_kind && filters.source_kind !== "真实检测结果"
        ? filters.source_kind
        : undefined,
    },
  });
  await pagination.setPage(1);
  await load();
}

function resetFilters() {
  Object.assign(filters, {
    q: "",
    type: "",
    review: "",
    verify: "",
    source_kind: "真实检测结果",
  });
  applyFilters();
}

async function open(row) {
  drawer.value = true;
  detailLoading.value = true;
  try {
    detail.value = await api.getFinding(row.id);
  } finally {
    detailLoading.value = false;
  }
}

async function review(row, action) {
  try {
    await api.reviewFinding(row.id, { action, note: "" });
    ElMessage.success(action === "confirm" ? "已确认，状态已更新" : "复核状态已更新");
    await load();
  } catch {
    // The request interceptor already shows the server-side reason.
  }
}

async function batchConfirm() {
  if (!selected.value.length) {
    ElMessage.info("请先选择凭据实体");
    return;
  }
  try {
    const result = await api.batchReview({
      ids: selected.value.map((item) => item.id),
      action: "confirm",
      note: "",
    });
    const successCount = Array.isArray(result?.ok) ? result.ok.length : 0;
    const failedCount = Array.isArray(result?.failed) ? result.failed.length : 0;
    ElMessage.success(
      failedCount
        ? `批量确认完成：成功 ${successCount} 条，失败 ${failedCount} 条`
        : `批量确认完成：${successCount} 条`,
    );
    selected.value = [];
    await load();
  } catch {
    // The request interceptor shows the server-side reason.
  }
}

async function verify(row) {
  try {
    const result = await api.verifyFinding(row.id);
    if (result?.deduplicated) {
      ElMessage.info(`该发现项已有验证任务（任务 #${result.job_id}）`);
    } else {
      ElMessage.success(`验证任务 #${result?.job_id || "-"} 已加入队列`);
    }
    await load();
  } catch {
    // The request interceptor already shows the server-side reason.
  }
}

async function batchVerify() {
  if (!selected.value.length) {
    ElMessage.info("请先选择凭据实体");
    return;
  }
  try {
    const result = await api.batchVerify({
      ids: selected.value.map((item) => item.id),
    });
    const successCount = Array.isArray(result?.queued) ? result.queued.length : 0;
    const failedCount = Array.isArray(result?.failed) ? result.failed.length : 0;
    ElMessage.success(
      failedCount
        ? `批量验证已入队：成功 ${successCount} 条，失败 ${failedCount} 条`
        : `批量验证已入队：${successCount} 条`,
    );
    selected.value = [];
    await load();
  } catch {
    // The request interceptor shows the server-side reason.
  }
}

async function copyFingerprint(value) {
  const fingerprint = String(value || "").trim();
  if (!fingerprint) {
    ElMessage.info("当前发现项没有 HMAC 指纹");
    return;
  }
  try {
    await navigator.clipboard.writeText(fingerprint);
  } catch {
    const textarea = document.createElement("textarea");
    textarea.value = fingerprint;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand("copy");
    textarea.remove();
  }
  ElMessage.success("HMAC 指纹已复制");
}

function onPage(page) { pagination.setPage(page).then(load); }
function onPageSize(pageSize) { pagination.setPageSize(pageSize).then(load); }

async function exportData() {
  downloadJson("findings-masked.json", await api.exportFindings());
}

onMounted(load);
</script>

<style scoped>
.fingerprint-cell {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.finding-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 4px;
  min-width: max-content;
  white-space: nowrap;
}

.findings-filter-toolbar {
  display: grid;
  grid-template-columns: minmax(0, 1.35fr) repeat(4, minmax(0, 1fr)) minmax(118px, auto);
  align-items: center;
  column-gap: 12px;
  row-gap: 12px;
  padding: 2px 0;
}

.findings-filter-toolbar .el-form-item {
  display: flex;
  align-items: center;
  min-width: 0;
  margin-right: 0;
}

.findings-filter-toolbar :deep(.el-form-item__label) {
  flex: 0 0 auto;
  padding-right: 10px;
  color: #526178;
  font-size: 13px;
  font-weight: 650;
}

.findings-filter-toolbar .el-input,
.findings-filter-toolbar .el-select {
  flex: 1 1 auto;
  width: auto;
  min-width: 0;
}

.findings-filter-toolbar .filter-actions {
  min-width: 118px;
  justify-content: flex-end;
  gap: 8px;
  white-space: nowrap;
}

.findings-filter-toolbar .filter-actions :deep(.el-form-item__content) {
  display: flex;
  gap: 8px;
}

@media (max-width: 760px) {
  .findings-filter-toolbar {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .findings-filter-toolbar .filter-actions {
    justify-content: flex-start;
  }
}

@media (max-width: 680px) {
  .findings-filter-toolbar {
    grid-template-columns: 1fr;
    row-gap: 10px;
  }

  .findings-filter-toolbar .el-form-item {
    width: 100%;
  }

  .findings-filter-toolbar .filter-actions {
    justify-content: flex-start;
  }
}

.fingerprint-value {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--el-font-family-monospace, monospace);
}

.fingerprint-copy-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 16px;
}
</style>
