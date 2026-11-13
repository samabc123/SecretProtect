<template>
  <AppLayout>
    <PageHeader title="指标报告" description="查看检测规模、渠道覆盖、时间明细和数据口径。">
      <el-button type="primary" @click="download">导出报告</el-button>
    </PageHeader>

    <nav class="section-nav" aria-label="指标报告分区">
      <a href="#report-summary">核心指标</a>
      <a href="#report-time-samples">时间明细</a>
      <a href="#report-definitions">指标口径</a>
    </nav>

    <LoadingState :loading="loading" :error="error" @retry="load" />
    <template v-if="!loading && !error">
      <section id="report-summary" class="page-section">
        <div class="sticky-summary">
          <MetricGrid :items="metrics" :show-hints="false" />
        </div>
        <el-alert
          v-if="!metrics.length"
          title="指标接口已连接，但当前没有可统计的真实数据"
          description="请先完成监控任务运行或导入授权数据，再刷新本页面。"
          type="info"
          :closable="false"
          show-icon
        />
      </section>

      <section id="report-time-samples" class="page-section">
        <el-card shadow="never" class="time-sample-table">
          <template #header>
            <div class="card-heading">
              <span>凭据时间明细</span>
            </div>
          </template>
          <el-table :data="timeSamplePageItems" size="small" empty-text="暂无真实来源时间样本">
            <el-table-column prop="event_id" label="事件" min-width="180" show-overflow-tooltip />
            <el-table-column prop="credential_type" label="凭据类型" min-width="155" />
            <el-table-column label="凭据公开时间" min-width="190">
              <template #default="{ row }">
                <div class="timeline-value">
                  <span>{{ formatTimestamp(row.published_at, "未记录") }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="公开证据状态" min-width="150">
              <template #default="{ row }">
                {{ displayPublicEvidenceStatus(row) }}
              </template>
            </el-table-column>
            <el-table-column label="系统首次发现时间" min-width="180">
              <template #default="{ row }">{{ formatFirstDiscovery(row, "未记录") }}</template>
            </el-table-column>
            <el-table-column label="发现延迟" min-width="165">
              <template #default="{ row }">{{ formatDiscoveryLatency(row, { concise: true }) }}</template>
            </el-table-column>
            <el-table-column label="详情" width="90" fixed="right">
              <template #default="{ row }">
                <router-link :to="`/events/${row.event_id}`">查看详情</router-link>
              </template>
            </el-table-column>
          </el-table>
          <PaginationBar
            :page="timeSamplePagination.pagination.page"
            :page-size="timeSamplePagination.pagination.page_size"
            :total="timeSamplePagination.pagination.total_items"
            @update:page="timeSamplePagination.setPage"
            @update:page-size="timeSamplePagination.setPageSize"
          />
        </el-card>
      </section>

      <section id="report-definitions" class="page-section">
        <el-card shadow="never">
          <template #header>指标映射与边界</template>
          <el-table
            v-if="report.evaluation_mapping?.length"
            :data="evaluationMappingPageItems"
            row-key="evaluation"
            size="small"
          >
            <el-table-column prop="evaluation" label="指标" min-width="220" />
            <el-table-column prop="module" label="数据模块" min-width="180" />
            <el-table-column prop="current" label="当前值" min-width="220" />
            <el-table-column prop="definition" label="统计定义" min-width="300" />
          </el-table>
          <PaginationBar
            v-if="(report.evaluation_mapping || []).length > 10"
            :page="evaluationMappingPagination.pagination.page"
            :page-size="evaluationMappingPagination.pagination.page_size"
            :total="evaluationMappingPagination.pagination.total_items"
            @update:page="evaluationMappingPagination.setPage"
            @update:page-size="evaluationMappingPagination.setPageSize"
          />
          <el-empty v-if="!(report.evaluation_mapping || []).length" description="暂无指标口径映射" />
          <el-divider />
          <div class="data-list">
            <div v-for="item in report.boundaries || []" :key="item.label" class="data-list-row">
              <span>{{ item.label }}</span>
              <strong>{{ item.value }}</strong>
            </div>
          </div>
        </el-card>
      </section>
    </template>
  </AppLayout>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import AppLayout from "../components/AppLayout.vue";
import PageHeader from "../components/PageHeader.vue";
import MetricGrid from "../components/MetricGrid.vue";
import PaginationBar from "../components/PaginationBar.vue";
import LoadingState from "../components/LoadingState.vue";
import * as api from "../api/system";
import {
  displayPublicEvidenceStatus,
  downloadJson,
  formatDiscoveryLatency,
  formatFirstDiscovery,
  formatTimestamp,
} from "../utils/format";
import { slicePage, useUrlPagination } from "../composables/useUrlPagination";

const report = ref({});
const loading = ref(true);
const error = ref("");
const timeSamplePagination = useUrlPagination({ prefix: "timeSamples" });
function shuffle(items) {
  const shuffled = [...items];
  for (let index = shuffled.length - 1; index > 0; index -= 1) {
    const swapIndex = Math.floor(Math.random() * (index + 1));
    [shuffled[index], shuffled[swapIndex]] = [shuffled[swapIndex], shuffled[index]];
  }
  return shuffled;
}

const timeSampleItems = computed(() => {
  const samples = [...(report.value.time_samples || [])];
  const calculable = shuffle(
    samples.filter((item) => item.discovery_latency_status === "calculated"),
  );
  const notCalculable = samples
    .filter((item) => item.discovery_latency_status !== "calculated")
    .sort((left, right) => {
  const leftTime = Date.parse(left.last_observed_at || left.last_seen_at || left.first_observed_at || "") || 0;
  const rightTime = Date.parse(right.last_observed_at || right.last_seen_at || right.first_observed_at || "") || 0;
  return rightTime - leftTime;
    });
  return [...calculable, ...notCalculable];
});
const timeSamplePageItems = computed(() => slicePage(timeSampleItems.value, timeSamplePagination.pagination).items);
const evaluationMappingPagination = useUrlPagination({ prefix: "reportMapping" });
const evaluationMappingPageItems = computed(() => slicePage(report.value.evaluation_mapping || [], evaluationMappingPagination.pagination).items);

const metrics = computed(() => {
  const counts = report.value.counts;
  if (Array.isArray(counts)) return counts;
  if (!counts || typeof counts !== "object") return [];
  const definitions = [
    ["real_candidate_count", "真实候选数"],
    ["real_credential_count", "真实凭据实体"],
    ["credential_type_count", "支持检索凭据类型"],
    ["confirmed_public_count", "确认公开泄露数"],
  ];
  return definitions
    .filter(([key]) => Object.prototype.hasOwnProperty.call(counts, key))
    .map(([key, label]) => ({ label, value: counts[key], hint: "接口聚合结果" }));
});

async function load() {
  loading.value = true;
  error.value = "";
  try {
    report.value = await api.getReports();
    const timeSamples = timeSampleItems.value;
    timeSamplePagination.pagination.total_items = timeSamples.length;
    timeSamplePagination.pagination.total_pages = timeSamples.length
      ? Math.ceil(timeSamples.length / timeSamplePagination.pagination.page_size)
      : 0;
    evaluationMappingPagination.pagination.total_items = (report.value.evaluation_mapping || []).length;
    evaluationMappingPagination.pagination.total_pages = report.value.evaluation_mapping?.length
      ? Math.ceil(report.value.evaluation_mapping.length / evaluationMappingPagination.pagination.page_size)
      : 0;
  } catch (e) {
    error.value = e.response?.data?.detail || "报告读取失败";
  } finally {
    loading.value = false;
  }
}

async function download() {
  downloadJson("evaluation-report.json", await api.exportReport());
}

onMounted(load);
</script>
