<template>
  <AppLayout>
    <PageHeader title="事件详情" description="查看脱敏证据、来源定位与处置记录。">
      <el-button @click="router.back()">返回</el-button>
      <el-button :disabled="!hasOperationalRecord" @click="download">导出脱敏证据</el-button>
    </PageHeader>

    <el-skeleton v-if="loading" :rows="8" animated />
    <el-result v-else-if="error" icon="error" title="事件读取失败" :sub-title="error" />
    <template v-else>
      <el-card shadow="never">
        <el-descriptions class="event-summary-descriptions" :column="descriptionColumns" border>
          <el-descriptions-item label="事件 ID">{{ event.event_id }}</el-descriptions-item>
          <el-descriptions-item label="来源类型">{{ event.source_kind }}</el-descriptions-item>
          <el-descriptions-item label="渠道">{{ event.channel }}</el-descriptions-item>
          <el-descriptions-item label="平台">{{ event.platform }}</el-descriptions-item>
          <el-descriptions-item label="凭据类型">{{ event.credential_type }}</el-descriptions-item>
          <el-descriptions-item label="风险"><StateTag :value="event.risk_level" kind="risk" /></el-descriptions-item>
          <el-descriptions-item label="脱敏候选">{{ event.redacted_value }}</el-descriptions-item>
          <el-descriptions-item label="指纹">{{ event.credential_fingerprint || "-" }}</el-descriptions-item>
          <el-descriptions-item label="来源定位" :span="2">{{ event.source_locator }}</el-descriptions-item>
          <el-descriptions-item label="公开/内容时间">{{ formatTimestamp(event.published_at, "未记录") }}</el-descriptions-item>
          <el-descriptions-item label="公开时间状态">{{ publicTimeStatusLabel }}</el-descriptions-item>
          <el-descriptions-item label="公开时间来源">{{ displayTimeSource(event.published_source) }}</el-descriptions-item>
          <el-descriptions-item label="系统首次发现">{{ formatFirstDiscovery(event, "未记录") }}</el-descriptions-item>
          <el-descriptions-item label="发现延迟">{{ formatDiscoveryLatency(event) }}</el-descriptions-item>
          <el-descriptions-item label="内容版本修订">{{ formatTimestamp(event.source_updated_at, "未记录") }}</el-descriptions-item>
          <el-descriptions-item label="首次观测">{{ formatTimestamp(event.first_observed_at, "未记录") }}</el-descriptions-item>
          <el-descriptions-item label="采集完成">{{ formatTimestamp(event.fetched_at, "未记录") }}</el-descriptions-item>
          <el-descriptions-item label="规则检测">{{ formatTimestamp(event.detected_at, "未记录") }}</el-descriptions-item>
        </el-descriptions>
        <div class="detail-actions">
          <el-button type="primary" :disabled="!hasOperationalRecord" @click="feedback('confirm_public')">{{ confirmationLabel }}</el-button>
          <el-button :disabled="!hasOperationalRecord" @click="feedback('mark_false_positive')">标记误报</el-button>
          <el-button :disabled="!hasOperationalRecord" @click="feedback('recheck')">重新检查</el-button>
          <el-button :disabled="!hasOperationalRecord" @click="createAlert">创建告警</el-button>
        </div>
      </el-card>

      <div class="content-grid two-columns">
        <el-card shadow="never" class="event-evidence-card">
          <template #header>证据与时间线</template>
          <el-descriptions class="event-evidence-descriptions" :column="1" label-width="132px" border>
            <el-descriptions-item v-for="item in event.evidence || []" :key="item.label" :label="item.label">
              {{ item.value }}
            </el-descriptions-item>
          </el-descriptions>
          <el-timeline v-if="timelinePageItems.length">
            <el-timeline-item
              v-for="item in timelinePageItems"
              :key="`${item.time}-${item.title}`"
              :timestamp="formatTimestamp(item.time)"
            >
              {{ item.title }}
              <div class="muted">{{ item.detail }}</div>
            </el-timeline-item>
          </el-timeline>
          <PaginationBar
            v-if="(event.timeline || []).length > 10"
            :page="timelinePagination.pagination.page"
            :page-size="timelinePagination.pagination.page_size"
            :total="timelinePagination.pagination.total_items"
            @update:page="timelinePagination.setPage"
            @update:page-size="timelinePagination.setPageSize"
          />
        </el-card>

        <el-card shadow="never">
          <template #header>告警与反馈</template>
          <el-empty v-if="!event.alerts?.length" description="暂无告警">
            <el-button type="primary" :disabled="!hasOperationalRecord" @click="createAlert">生成离线告警记录</el-button>
          </el-empty>
          <div v-for="alert in alertPageItems" :key="alert.alert_id" class="alert-row">
            <span>{{ alert.alert_id }} / {{ displayState(alert.status) }}</span>
            <el-button size="small" @click="deliver(alert)">记录离线通知</el-button>
          </div>
          <PaginationBar
            v-if="(event.alerts || []).length > 10"
            :page="alertPagination.pagination.page"
            :page-size="alertPagination.pagination.page_size"
            :total="alertPagination.pagination.total_items"
            @update:page="alertPagination.setPage"
            @update:page-size="alertPagination.setPageSize"
          />
          <el-divider />
          <div v-for="item in actionPageItems" :key="item.label" class="action-row">
            <span>{{ item.label }}：{{ displayState(item.status) }}</span>
            <el-button v-if="item.action" size="small" @click="feedback(item.action)">执行</el-button>
          </div>
          <PaginationBar
            v-if="(event.actions || []).length > 10"
            :page="actionPagination.pagination.page"
            :page-size="actionPagination.pagination.page_size"
            :total="actionPagination.pagination.total_items"
            @update:page="actionPagination.setPage"
            @update:page-size="actionPagination.setPageSize"
          />
        </el-card>
      </div>

      <el-card shadow="never">
        <template #header>原始响应结构（脱敏）</template>
        <pre class="json-block">{{ JSON.stringify(rawResponse, null, 2) }}</pre>
      </el-card>
    </template>
  </AppLayout>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from "vue";
import { ElMessage } from "element-plus";
import { useRoute, useRouter } from "vue-router";
import AppLayout from "../components/AppLayout.vue";
import PageHeader from "../components/PageHeader.vue";
import PaginationBar from "../components/PaginationBar.vue";
import StateTag from "../components/StateTag.vue";
import * as api from "../api/events";
import {
  displayPublicEvidenceStatus,
  displayState,
  displayTimeSource,
  formatDiscoveryLatency,
  formatFirstDiscovery,
  formatTimestamp,
} from "../utils/format";
import { slicePage, useUrlPagination } from "../composables/useUrlPagination";

const route = useRoute();
const router = useRouter();
const event = ref({});
const rawResponse = ref({});
const loading = ref(true);
const error = ref("");
const descriptionColumns = ref(window.innerWidth <= 760 ? 1 : 2);
const timelinePagination = useUrlPagination({ prefix: "eventTimeline" });
const alertPagination = useUrlPagination({ prefix: "eventAlerts" });
const actionPagination = useUrlPagination({ prefix: "eventActions" });
const timelinePageItems = computed(() => slicePage(event.value.timeline || [], timelinePagination.pagination).items);
const alertPageItems = computed(() => slicePage(event.value.alerts || [], alertPagination.pagination).items);
const actionPageItems = computed(() => slicePage(event.value.actions || [], actionPagination.pagination).items);
const hasOperationalRecord = computed(() => Boolean(event.value?.event_id && event.value?.event_record_available));
const confirmationLabel = computed(() => (
  event.value?.source_kind === "真实检测结果" ? "确认公开" : "确认受控环境"
));
const publicTimeStatusLabel = computed(() => displayPublicEvidenceStatus(event.value));

function setDetailTotals(items, pagination) {
  pagination.pagination.total_items = items.length;
  pagination.pagination.total_pages = items.length
    ? Math.ceil(items.length / pagination.pagination.page_size)
    : 0;
}

async function load() {
  try {
    const payload = await api.getEvent(route.params.eventId);
    rawResponse.value = payload;
    const summaryValue = (label) => payload.event?.summary?.find((item) => item.label === label)?.value;
    const evidenceValue = (label) => payload.evidence?.find((item) => item.label === label)?.value;
    const record = payload.event?.event_record || {};
    event.value = {
      ...record,
      event_id: record.event_id || payload.event?.event_id || route.params.eventId,
      source_kind: record.source_kind || summaryValue("数据来源") || payload.source || "未记录",
      channel: record.channel || summaryValue("渠道") || "未记录",
      platform: record.platform || "未记录",
      credential_type: record.credential_type || summaryValue("凭据类型") || "未记录",
      risk_level: record.risk_level || summaryValue("风险档") || "info",
      redacted_value: record.redacted_value || evidenceValue("脱敏候选") || "未记录",
      credential_fingerprint: record.credential_fingerprint || evidenceValue("凭据指纹") || evidenceValue("指纹") || "",
      source_locator: record.source_locator || evidenceValue("公开位置") || "未记录",
      published_at: record.published_at || evidenceValue("公开时间") || null,
      published_source: record.published_source || evidenceValue("公开时间来源") || "",
      published_meaning: record.published_meaning || evidenceValue("公开时间含义") || "",
      published_evidence: record.published_evidence || evidenceValue("公开时间证据") || "",
      public_time_status: record.public_time_status || "unknown",
      public_time_unknown_reason: record.public_time_unknown_reason || "",
      first_discovered_at: record.first_discovered_at || payload.discovery_latency?.first_discovered_at || null,
      first_discovery_time_status: record.first_discovery_time_status || payload.discovery_latency?.first_discovery_time_status || "unknown",
      first_discovery_unknown_reason: record.first_discovery_unknown_reason || payload.discovery_latency?.first_discovery_unknown_reason || "",
      discovery_latency_seconds: record.discovery_latency_seconds ?? payload.discovery_latency?.seconds ?? null,
      discovery_latency_status: record.discovery_latency_status || payload.discovery_latency?.status || "not_calculable",
      discovery_latency_reason: record.discovery_latency_reason || payload.discovery_latency?.reason || "",
      discovery_latency_calculation_type: record.discovery_latency_calculation_type
        || payload.discovery_latency?.calculation_type
        || "not_calculable",
      discovery_latency_basis: record.discovery_latency_basis
        || payload.discovery_latency?.basis
        || "",
      discovery_latency_end_at: record.discovery_latency_end_at
        || payload.discovery_latency?.end_at
        || null,
      source_updated_at: record.source_updated_at || evidenceValue("内容版本修订时间") || null,
      source_updated_source: record.source_updated_source || evidenceValue("内容版本时间来源") || "",
      first_observed_at: record.first_observed_at || null,
      fetched_at: record.fetched_at || evidenceValue("采集时间") || null,
      detected_at: record.detected_at || null,
      event_record_available: Boolean(payload.event?.event_record),
      summary: payload.event?.summary || [],
      evidence: payload.evidence || [],
      rechecks: payload.rechecks || [],
      timeline: payload.timeline || [],
      actions: payload.actions || [],
      alerts: payload.alerts || [],
      feedbacks: payload.feedbacks || [],
    };
    setDetailTotals(event.value.timeline, timelinePagination);
    setDetailTotals(event.value.alerts, alertPagination);
    setDetailTotals(event.value.actions, actionPagination);
  } catch (e) {
    error.value = e.response?.data?.detail || "事件不存在";
  } finally {
    loading.value = false;
  }
}
async function feedback(action) {
  if (!hasOperationalRecord.value) {
    ElMessage.info("当前为合成数据，反馈操作未接入");
    return;
  }
  await api.feedback(route.params.eventId, { action, note: "" });
  ElMessage.success("反馈已记录");
  await load();
}

async function createAlert() {
  if (!hasOperationalRecord.value) {
    ElMessage.info("当前为合成数据，告警操作未接入");
    return;
  }
  await api.createAlert(route.params.eventId);
  ElMessage.success("告警已写入离线队列");
  await load();
}

async function deliver(alert) {
  if (!hasOperationalRecord.value) {
    ElMessage.info("当前为合成数据，通知操作未接入");
    return;
  }
  await api.deliverAlert(alert.alert_id);
  ElMessage.success("离线通知已记录");
  await load();
}

async function download() {
  if (!hasOperationalRecord.value) {
    ElMessage.info("当前为合成数据，导出操作未接入");
    return;
  }
  const data = await api.exportEvidence(route.params.eventId);
  const blob = data instanceof Blob ? data : new Blob([JSON.stringify(data)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `event-${route.params.eventId}-evidence.json`;
  anchor.click();
  URL.revokeObjectURL(url);
}

function updateDescriptionColumns() {
  descriptionColumns.value = window.innerWidth <= 760 ? 1 : 2;
}

onMounted(() => {
  load();
  window.addEventListener("resize", updateDescriptionColumns);
});
onUnmounted(() => window.removeEventListener("resize", updateDescriptionColumns));
</script>
