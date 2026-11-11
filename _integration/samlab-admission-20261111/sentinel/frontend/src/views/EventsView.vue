<template>
  <AppLayout>
    <PageHeader title="事件列表" description="按渠道、凭据类型、风险和处置状态筛选事件。">
      <el-button @click="batchConfirm">批量确认</el-button>
      <el-button @click="exportSelected">导出事件</el-button>
    </PageHeader>
    <el-card shadow="never" class="filter-card">
      <el-form :model="filters" inline class="filter-toolbar" @submit.prevent="applyFilters">
        <el-form-item label="渠道"><el-select v-model="filters.channel" clearable placeholder="全部渠道"><el-option v-for="item in filterOptions.channel" :key="item" :label="item" :value="item" /></el-select></el-form-item>
        <el-form-item label="凭据类型"><el-select v-model="filters.credential_type" clearable placeholder="全部类型"><el-option v-for="item in filterOptions.credential_type" :key="item" :label="item" :value="item" /></el-select></el-form-item>
        <el-form-item label="风险"><el-select v-model="filters.risk_level" clearable placeholder="全部风险"><el-option v-for="item in filterOptions.risk_level" :key="item" :label="displayState(item, 'risk')" :value="item" /></el-select></el-form-item>
        <el-form-item label="处置"><el-select v-model="filters.disposition" clearable placeholder="全部处置"><el-option v-for="item in filterOptions.disposition" :key="item" :label="displayState(item)" :value="item" /></el-select></el-form-item>
        <el-form-item><el-button type="primary" native-type="submit">应用筛选</el-button><el-button @click="resetFilters">重置</el-button></el-form-item>
      </el-form>
    </el-card>
    <MetricGrid :items="summaryItems" />
    <el-card shadow="never">
      <el-table v-loading="loading" :data="items" class="event-table" @selection-change="selected = $event">
        <el-table-column type="selection" width="48" />
        <el-table-column prop="event_id" label="事件" min-width="180"><template #default="{ row }"><router-link :to="`/events/${row.event_id}`">{{ row.event_id }}</router-link><div class="muted">{{ row.source_kind }}</div></template></el-table-column>
        <el-table-column label="来源" min-width="150"><template #default="{ row }">{{ row.channel }}<div class="muted">{{ row.platform }}</div></template></el-table-column>
        <el-table-column prop="credential_type" label="凭据类型" min-width="150" show-overflow-tooltip />
        <el-table-column prop="redacted_value" label="脱敏候选" min-width="180" />
        <el-table-column prop="evidence_level" label="证据" width="82" />
        <el-table-column label="风险" width="105"><template #default="{ row }"><StateTag :value="row.risk_level" kind="risk" /></template></el-table-column>
        <el-table-column label="处置" width="125">
          <template #default="{ row }"><div class="disposition-cell"><StateTag :value="row.disposition" /></div></template>
        </el-table-column>
        <el-table-column label="时间" min-width="255">
          <template #default="{ row }">
            <div class="timeline-value">
              <span>{{ publicTimeLabel(row) }}：{{ formatTimestamp(row.published_at, "未记录") }}</span>
              <small>公开证据：{{ displayPublicEvidenceStatus(row) }}</small>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="规则检测" min-width="205"><template #default="{ row }">{{ formatTimestamp(row.detected_at, "-") }}</template></el-table-column>
      </el-table>
      <PaginationBar
        :page="pagination.pagination.page"
        :page-size="pagination.pagination.page_size"
        :total="pagination.pagination.total_items"
        @update:page="onPage"
        @update:page-size="onPageSize"
      />
    </el-card>
  </AppLayout>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from "vue";
import { ElMessage } from "element-plus";
import { useRoute, useRouter } from "vue-router";
import AppLayout from "../components/AppLayout.vue";
import PageHeader from "../components/PageHeader.vue";
import MetricGrid from "../components/MetricGrid.vue";
import PaginationBar from "../components/PaginationBar.vue";
import StateTag from "../components/StateTag.vue";
import * as api from "../api/events";
import {
  displayPublicEvidenceStatus,
  displayState,
  formatTimestamp,
} from "../utils/format";
import { useUrlPagination } from "../composables/useUrlPagination";

const items = ref([]);
const selected = ref([]);
const loading = ref(false);
const route = useRoute();
const router = useRouter();
const pagination = useUrlPagination();
const filters = reactive({
  channel: String(route.query.channel || ""),
  credential_type: String(route.query.credential_type || ""),
  risk_level: String(route.query.risk_level || ""),
  disposition: String(route.query.disposition || ""),
  source_kind: String(route.query.source_kind || ""),
  sort_by: String(route.query.sort_by || "event_id"),
  sort_order: String(route.query.sort_order || "asc"),
});
const filterOptions = reactive({ channel: [], credential_type: [], risk_level: [], disposition: [] });
const summary = ref({});
const summaryItems = computed(() => [
  { label: "候选数", value: summary.value.candidate_count, hint: "当前事件记录口径" },
  { label: "已分类数", value: summary.value.classified_count, hint: "L1-L4 证据" },
  { label: "唯一凭据数", value: summary.value.unique_credential_count, hint: "HMAC 指纹去重" },
  { label: "事件数", value: summary.value.event_count, hint: "当前持久化记录" },
]);
function publicTimeLabel(row) {
  return {
    known: "公开时间",
    reference_only: "来源时间",
    unknown: "公开时间未知",
  }[row.public_time_status] || "公开时间";
}
async function load() {
  loading.value = true;
  try {
    const payload = await api.getEvents({
      ...filters,
      page: pagination.pagination.page,
      page_size: pagination.pagination.page_size,
    });
    items.value = payload.items || [];
    pagination.applyResponse(payload.pagination);
    Object.assign(summary.value, payload.summary || {});
    Object.assign(filterOptions, payload.filters?.reduce((out, item) => { out[item.key] = item.options || []; return out; }, {}) || {});
  } finally {
    loading.value = false;
  }
}
function resetFilters() {
  Object.assign(filters, { channel: "", credential_type: "", risk_level: "", disposition: "", source_kind: "" });
  applyFilters();
}
async function applyFilters() {
  await router.replace({
    query: {
      ...route.query,
      channel: filters.channel || undefined,
      credential_type: filters.credential_type || undefined,
      risk_level: filters.risk_level || undefined,
      disposition: filters.disposition || undefined,
      source_kind: filters.source_kind || undefined,
    },
  });
  await pagination.setPage(1);
  await load();
}
function onPage(page) { pagination.setPage(page).then(load); }
function onPageSize(pageSize) { pagination.setPageSize(pageSize).then(load); }
async function batchConfirm() {
  if (!selected.value.length) return ElMessage.info("请先选择事件");
  await api.batchFeedback({ event_ids: selected.value.map((item) => item.event_id), action: "confirm_public", note: "" });
  ElMessage.success("批量反馈已记录");
  await load();
}
async function exportSelected() {
  const data = await api.exportEvents(selected.value.map((item) => item.event_id));
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "events-masked.json";
  anchor.click();
  URL.revokeObjectURL(url);
}
onMounted(load);
</script>

<style scoped>
.disposition-cell {
  display: flex;
  min-width: 0;
  align-items: center;
}
</style>
