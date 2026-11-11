<template>
  <AppLayout>
    <PageHeader
      title="运行审计"
      description="统一查看连接器执行批次、候选、写入和被过滤数量。"
    >
      <el-button @click="load">刷新</el-button>
    </PageHeader>

    <nav class="section-nav" aria-label="运行审计分区">
      <a href="#audit-summary">审计概览</a>
      <a href="#audit-list">运行列表</a>
      <a href="#audit-definition">口径说明</a>
    </nav>

    <section id="audit-summary" class="page-section">
      <div class="sticky-summary">
        <MetricGrid :items="summaryItems" :show-hints="false" />
      </div>
    </section>

    <section id="audit-list" class="page-section">
      <el-card shadow="never">
        <template #header>
          <div class="card-heading">
            <span>运行批次</span>
            <el-switch
              v-model="includeFailed"
              active-text="显示失败"
              inactive-text="隐藏失败"
              @change="toggleFailed"
            />
          </div>
        </template>
        <el-alert
          v-if="!includeFailed && auditSummary.hidden_failed_runs"
          title="失败批次默认隐藏，未删除数据库记录"
          :description="`当前隐藏 ${auditSummary.hidden_failed_runs} 条失败批次；打开“显示失败”即可恢复查看。`"
          type="info"
          :closable="false"
          show-icon
          class="inline-alert"
        />
        <el-table
          v-loading="loading"
          :data="items"
          row-key="run_id"
          empty-text="当前筛选条件下没有运行记录"
        >
          <el-table-column prop="run_id" label="运行 ID" min-width="130" />
          <el-table-column prop="connector_name" label="连接器" min-width="130" />
          <el-table-column prop="target" label="目标" min-width="180" show-overflow-tooltip />
          <el-table-column prop="duration_seconds" label="耗时（秒）" width="105" />
          <el-table-column label="状态" width="105">
            <template #default="{ row }"><StateTag :value="row.status" /></template>
          </el-table-column>
          <el-table-column prop="items_seen" label="扫描项" width="82" />
          <el-table-column prop="candidates" label="候选" width="82" />
          <el-table-column label="写入" width="82">
            <template #default="{ row }">{{ row.inserted ?? 0 }}</template>
          </el-table-column>
          <el-table-column prop="filtered" label="被过滤" width="90" />
          <el-table-column label="批次结果" min-width="280">
            <template #default="{ row }">
              <span v-if="row.filtered">{{ row.filtered_reason }}</span>
              <span v-else-if="row.detection_note">{{ row.detection_note }}</span>
              <span v-else class="muted">无差异</span>
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

    <section id="audit-definition" class="page-section">
      <el-card shadow="never">
        <template #header>数量口径与本次差异结论</template>
        <el-descriptions :column="1" border>
          <el-descriptions-item label="候选">
            连接器本批次产生并提交给统一后端的事件数量。
          </el-descriptions-item>
          <el-descriptions-item label="被过滤">
            本批次候选中被跳过的数量；当前实现对应 ingest_events 的 skipped_occurrences，
            由 occurrences 的唯一位置约束跳过重复记录。
          </el-descriptions-item>
          <el-descriptions-item label="失败判定">
            状态为“失败”。成功但存在被过滤记录仍保留为成功批次，不会误判为失败。
          </el-descriptions-item>
        </el-descriptions>
      </el-card>
    </section>
  </AppLayout>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import AppLayout from "../components/AppLayout.vue";
import PageHeader from "../components/PageHeader.vue";
import PaginationBar from "../components/PaginationBar.vue";
import MetricGrid from "../components/MetricGrid.vue";
import StateTag from "../components/StateTag.vue";
import * as api from "../api/system";
import { useUrlPagination } from "../composables/useUrlPagination";

const items = ref([]);
const loading = ref(false);
const route = useRoute();
const router = useRouter();
const includeFailed = ref(route.query.includeFailed === "1");
const auditSummary = ref({});
const pagination = useUrlPagination({ prefix: "runs" });

const summaryItems = computed(() => [
  { label: "运行总数", value: auditSummary.value.total_runs ?? 0, hint: "数据库 scan_runs" },
  { label: "当前显示", value: auditSummary.value.visible_runs ?? 0, hint: "按当前失败开关" },
  { label: "失败批次", value: auditSummary.value.failed_runs ?? 0, hint: "可切换查看，不做删除" },
  {
    label: "存在过滤的成功批次",
    value: auditSummary.value.success_with_filtering ?? 0,
    hint: "候选与被过滤数量不一致",
  },
]);

async function load() {
  loading.value = true;
  try {
    const data = await api.getRuns({
      page: pagination.pagination.page,
      page_size: pagination.pagination.page_size,
      include_failed: includeFailed.value,
    });
    items.value = data.items || [];
    auditSummary.value = data.summary || {};
    pagination.applyResponse(data.pagination);
  } finally {
    loading.value = false;
  }
}

async function toggleFailed(value) {
  includeFailed.value = value;
  await router.replace({
    query: { ...route.query, includeFailed: value ? "1" : undefined },
  });
  await pagination.setPage(1);
  await load();
}

function onPage(page) { pagination.setPage(page).then(load); }
function onPageSize(pageSize) { pagination.setPageSize(pageSize).then(load); }

onMounted(load);
</script>
