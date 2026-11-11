<template>
  <AppLayout>
    <PageHeader
      title="监控任务"
      description="统一查看监控对象、采集方式和最近一次运行结果。"
    >
      <el-button type="primary" @click="openCreate">新建监控任务</el-button>
      <el-button @click="load">刷新</el-button>
    </PageHeader>

    <el-alert
      v-if="releaseNotice"
      :title="releaseNotice"
      type="info"
      :closable="false"
      show-icon
    />

    <nav class="section-nav" aria-label="监控任务分区">
      <a href="#task-summary">任务概览</a>
      <a href="#task-filters">筛选条件</a>
      <a href="#task-list">任务列表</a>
    </nav>

    <section id="task-summary" class="page-section">
      <MetricGrid :items="summary" :show-hints="false" />
    </section>

    <section id="task-filters" class="page-section">
      <el-card shadow="never" class="filter-card">
        <el-form
          :model="filters"
          inline
          class="filter-toolbar monitoring-filter-toolbar"
          @submit.prevent="applyFilters"
        >
          <el-form-item label="渠道">
            <el-select v-model="filters.channel" clearable placeholder="全部渠道">
              <el-option
                v-for="value in options.channel || []"
                :key="value"
                :label="optionLabel('channel', value)"
                :value="value"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="采集方式">
            <el-select
              v-model="filters.collection_method"
              clearable
              placeholder="全部方式"
            >
              <el-option
                v-for="value in options.collection_method || []"
                :key="value"
                :label="optionLabel('collection_method', value)"
                :value="value"
              />
            </el-select>
          </el-form-item>
          <el-form-item>
            <el-button type="primary" native-type="submit">应用筛选</el-button>
            <el-button @click="resetFilters">重置</el-button>
          </el-form-item>
        </el-form>
      </el-card>
    </section>

    <section id="task-list" class="page-section">
      <el-card shadow="never">
        <template #header>
          <div class="card-heading">
            <span>任务列表</span>
            <span class="muted">{{ pagination.pagination.total_items }} 个任务</span>
          </div>
        </template>
        <el-table
          v-loading="loading"
          :data="pageItems"
          row-key="task_id"
          empty-text="当前筛选条件下没有监控任务"
        >
          <el-table-column prop="task_name" label="任务" min-width="190">
            <template #default="{ row }">
              <strong>{{ row.task_name }}</strong>
              <div class="muted">{{ row.task_id }}</div>
            </template>
          </el-table-column>
          <el-table-column label="渠道 / 平台" min-width="180">
            <template #default="{ row }">
              <span>{{ optionLabel("channel", row.channel) }}</span>
              <div class="muted">{{ row.platform }}</div>
            </template>
          </el-table-column>
          <el-table-column prop="monitor_object" label="监控对象" min-width="220" show-overflow-tooltip />
          <el-table-column label="采集方式" min-width="150">
            <template #default="{ row }">{{ collectionMethodLabel(row.collection_method) }}</template>
          </el-table-column>
          <el-table-column prop="schedule" label="周期" min-width="100" />
          <el-table-column label="最近运行" min-width="165">
            <template #default="{ row }">{{ formatTimestamp(row.last_run, "尚未运行") }}</template>
          </el-table-column>
          <el-table-column label="操作" width="300" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" @click="openDetails(row)">详情</el-button>
              <el-button link type="primary" @click="openEdit(row)">修改</el-button>
              <el-button link type="primary" @click="run(row)">运行</el-button>
              <el-button link @click="toggle(row)">
                {{ row.status === "已暂停" ? "恢复" : "暂停" }}
              </el-button>
              <el-button link type="danger" @click="remove(row)">删除</el-button>
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

    <el-drawer v-model="detailsVisible" title="监控任务详情" size="520px">
      <template v-if="selectedTask">
        <el-descriptions :column="1" border>
          <el-descriptions-item label="任务名称">{{ selectedTask.task_name }}</el-descriptions-item>
          <el-descriptions-item label="任务 ID">{{ selectedTask.task_id }}</el-descriptions-item>
          <el-descriptions-item label="渠道 / 平台">
            {{ optionLabel("channel", selectedTask.channel) }} / {{ selectedTask.platform }}
          </el-descriptions-item>
          <el-descriptions-item label="监控对象">{{ selectedTask.monitor_object }}</el-descriptions-item>
          <el-descriptions-item label="采集方式">
            {{ collectionMethodLabel(selectedTask.collection_method) }}
          </el-descriptions-item>
          <el-descriptions-item label="运行周期">{{ selectedTask.schedule }}</el-descriptions-item>
          <el-descriptions-item label="最近状态">
            <StateTag :value="selectedTask.status" />
          </el-descriptions-item>
          <el-descriptions-item v-if="selectedTask.last_error" label="最近错误">
            {{ selectedTask.last_error }}
          </el-descriptions-item>
        </el-descriptions>

        <el-divider content-position="left">已解析配置</el-divider>
        <el-alert
          v-if="selectedTask.config_parse_error"
          title="配置内容无法解析"
          description="当前任务配置不是有效的 JSON 对象，请检查来源配置。"
          type="warning"
          :closable="false"
          show-icon
        />
        <el-empty
          v-else-if="!configEntries.length"
          description="当前任务没有可展示的配置参数"
        />
        <el-descriptions v-else :column="1" border>
          <el-descriptions-item
            v-for="entry in configEntries"
            :key="entry.key"
            :label="entry.label"
          >
            {{ entry.value }}
          </el-descriptions-item>
        </el-descriptions>

        <el-collapse class="raw-json-collapse">
          <el-collapse-item title="查看原始 JSON" name="raw">
            <pre class="json-block">{{ rawConfig }}</pre>
          </el-collapse-item>
        </el-collapse>
      </template>
    </el-drawer>

    <el-dialog
      v-model="dialogVisible"
      :title="editingTask ? '修改监控任务' : '新增监控任务'"
      width="720px"
      :close-on-click-modal="false"
      @closed="resetDialog"
    >
      <el-form ref="formRef" :model="form" label-width="100px">
        <el-form-item
          v-for="item in formFields"
          :key="item.key"
          :label="item.label"
          :prop="item.key"
          :rules="{ required: true, message: `请输入${item.label}` }"
        >
          <el-input
            v-model="form[item.key]"
            :disabled="editingTask && ['task_id', 'channel', 'platform', 'collection_method'].includes(item.key)"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveTask">
          {{ editingTask ? "保存修改" : "保存配置" }}
        </el-button>
      </template>
    </el-dialog>
  </AppLayout>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { useRoute, useRouter } from "vue-router";
import AppLayout from "../components/AppLayout.vue";
import PageHeader from "../components/PageHeader.vue";
import PaginationBar from "../components/PaginationBar.vue";
import MetricGrid from "../components/MetricGrid.vue";
import StateTag from "../components/StateTag.vue";
import * as api from "../api/settings";
import { formatTimestamp } from "../utils/format";
import { slicePage, useUrlPagination } from "../composables/useUrlPagination";

const route = useRoute();
const router = useRouter();
const items = ref([]);
const releaseNotice = ref("");
const loading = ref(false);
const saving = ref(false);
const dialogVisible = ref(false);
const detailsVisible = ref(false);
const editingTask = ref(null);
const selectedTask = ref(null);
const formRef = ref();
const options = ref({});
const summary = ref([]);
const filters = reactive({
  channel: String(route.query.channel || ""),
  collection_method: String(route.query.collection_method || ""),
});
const pagination = useUrlPagination({ prefix: "tasks" });
const formFields = [
  { key: "task_id", label: "任务 ID" },
  { key: "task_name", label: "任务名称" },
  { key: "channel", label: "渠道" },
  { key: "platform", label: "平台" },
  { key: "monitor_object", label: "监控对象" },
  { key: "collection_method", label: "采集方式" },
  { key: "schedule", label: "周期" },
];
const form = reactive(Object.fromEntries(formFields.map((item) => [item.key, ""])));

const channelLabels = {
  ai_assets: "AI 模型与数据",
  app: "应用包",
  authorized_import: "授权离线导入",
  code_hosting: "代码托管",
  container: "容器镜像",
  controlled_test: "受控测试",
  knowledge_sharing: "知识分享",
  local_import: "本地导入",
  miniprogram: "小程序",
  package_registry: "包仓库",
  social_public: "社交媒体",
  wiki: "Wiki",
};
const monitoringChannelOrder = [
  "wiki",
  "social_public",
  "code_hosting",
  "knowledge_sharing",
  "container",
  "app",
  "miniprogram",
];
const monitoringChannelAliases = {
  Wiki: "wiki",
  微博: "social_public",
  "微博（官方 OAuth API）": "social_public",
  代码托管: "code_hosting",
  代码托管平台: "code_hosting",
  知识分享: "knowledge_sharing",
  知识共享平台: "knowledge_sharing",
  容器镜像: "container",
  App: "app",
  应用包: "app",
  小程序: "miniprogram",
};
const monitoringChannelRanks = new Map(
  monitoringChannelOrder.map((channel, index) => [channel, index]),
);

const collectionMethodLabels = {
  apk: "Android APK",
  authorized_jsonl: "授权 JSONL",
  controlled_fixtures: "受控夹具",
  crates_registry: "crates.io 包仓库",
  generic_web: "RSS/Atom/Sitemap/URL",
  gitee: "Gitee 授权 API",
  github: "GitHub 授权 API",
  huggingface_repo: "Hugging Face Hub",
  local_dir: "本地目录",
  mediawiki: "MediaWiki API",
  npm_registry: "npm 包仓库",
  nuget_registry: "NuGet 包仓库",
  oci_registry: "OCI 镜像仓库",
  pypi_registry: "PyPI 包仓库",
  rubygems_registry: "RubyGems 包仓库",
  stackoverflow: "Stack Overflow API",
  weibo_authorized_api: "微博官方 OAuth API",
  wxapkg: "未加密 wxapkg",
};

function channelSortRank(value) {
  const raw = String(value || "").trim();
  const key = monitoringChannelAliases[raw] || raw;
  return monitoringChannelRanks.get(key) ?? monitoringChannelOrder.length;
}

const pageItems = computed(() => {
  const sorted = items.value
    .map((item, index) => ({ item, index }))
    .sort((left, right) => (
      channelSortRank(left.item.channel) - channelSortRank(right.item.channel)
      || left.index - right.index
    ))
    .map(({ item }) => item);
  return slicePage(sorted, pagination.pagination).items;
});

function optionLabel(key, value) {
  if (key === "channel") return channelLabels[value] || value;
  if (key === "collection_method") return collectionMethodLabels[value] || value;
  return value;
}

function collectionMethodLabel(value) {
  return optionLabel("collection_method", value);
}

const configEntries = computed(() => {
  const config = selectedTask.value?.config;
  if (!config || typeof config !== "object" || Array.isArray(config)) return [];
  return Object.entries(config).map(([key, value]) => ({
    key,
    label: key,
    value: formatConfigValue(value),
  }));
});

const rawConfig = computed(() => {
  if (!selectedTask.value) return "{}";
  if (typeof selectedTask.value.config_raw === "string") return selectedTask.value.config_raw;
  return JSON.stringify(selectedTask.value.config || {}, null, 2);
});

function formatConfigValue(value) {
  if (value === null || value === undefined || value === "") return "未配置";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}

async function load() {
  loading.value = true;
  try {
    const data = await api.getTasks(filters);
    items.value = data.items || [];
    releaseNotice.value = data.release_notice || "";
    options.value =
      data.filters?.reduce((out, item) => {
        out[item.key] = item.options || [];
        return out;
      }, {}) || {};
    if (Array.isArray(options.value.channel)) {
      options.value.channel = [...options.value.channel].sort(
        (left, right) => channelSortRank(left) - channelSortRank(right),
      );
    }
    summary.value = data.summary || [];
    pagination.pagination.total_items = items.value.length;
    pagination.pagination.total_pages = items.value.length
      ? Math.ceil(items.value.length / pagination.pagination.page_size)
      : 0;
  } finally {
    loading.value = false;
  }
}

async function resetFilters() {
  Object.assign(filters, { channel: "", collection_method: "" });
  await applyFilters();
}

async function applyFilters() {
  await router.replace({
    query: {
      ...route.query,
      channel: filters.channel || undefined,
      collection_method: filters.collection_method || undefined,
    },
  });
  await pagination.setPage(1);
  await load();
}

function openDetails(row) {
  selectedTask.value = row;
  detailsVisible.value = true;
}

function resetForm() {
  Object.keys(form).forEach((key) => {
    form[key] = "";
  });
}

function resetDialog() {
  editingTask.value = null;
  resetForm();
  formRef.value?.clearValidate();
}

function openCreate() {
  resetDialog();
  dialogVisible.value = true;
}

function openEdit(row) {
  editingTask.value = row;
  Object.assign(form, {
    task_id: row.task_id || "",
    task_name: row.task_name || "",
    channel: row.channel || "",
    platform: row.platform || "",
    monitor_object: row.monitor_object || "",
    collection_method: row.collection_method || "",
    schedule: row.schedule || "手动",
  });
  formRef.value?.clearValidate();
  dialogVisible.value = true;
}

async function saveTask() {
  await formRef.value.validate();
  saving.value = true;
  try {
    if (editingTask.value) {
      await api.updateTask(editingTask.value.task_id, { ...form });
      ElMessage.success("监控任务修改已保存");
    } else {
      await api.createTask({ ...form });
      ElMessage.success("监控任务已保存");
    }
    dialogVisible.value = false;
    editingTask.value = null;
    resetForm();
    await load();
  } finally {
    saving.value = false;
  }
}

async function run(row) {
  await api.taskAction(row.task_id, "run");
  ElMessage.success("任务运行已完成");
  await load();
}

async function toggle(row) {
  await api.taskAction(row.task_id, row.status === "已暂停" ? "resume" : "pause");
  ElMessage.success("任务状态已更新");
  await load();
}

async function remove(row) {
  await ElMessageBox.confirm(
    "确认删除该监控任务？已有发现记录不会自动删除。",
    "删除确认",
  );
  await api.deleteTask(row.task_id);
  ElMessage.success("任务已删除");
  await load();
}

function onPage(page) { pagination.setPage(page); }
function onPageSize(pageSize) { pagination.setPageSize(pageSize); }

onMounted(load);
</script>
