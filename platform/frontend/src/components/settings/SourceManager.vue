<template>
  <div class="source-manager">
    <SettingCard
      title="统一后端来源"
      subtitle="配置 JSON 只保存目标和预算，不在此处填写 Token"
    >
      <el-form :model="form" label-position="top" class="source-form">
        <el-form-item label="来源名称">
          <el-input v-model="form.name" />
        </el-form-item>
        <el-form-item label="连接器">
          <el-select v-model="form.collector_key">
            <el-option
              v-for="item in catalog"
              :key="item.key"
              :label="item.title"
              :value="item.key"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="运行模式">
          <el-select v-model="form.mode">
            <el-option label="手动" value="manual" />
            <el-option label="持续调度" value="continuous" />
          </el-select>
        </el-form-item>
        <el-form-item label="配置 JSON">
          <el-input v-model="form.configText" type="textarea" rows="4" />
          <small class="muted">最小示例：{{ hint }}</small>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
        </el-form-item>
        <el-button type="primary" @click="create">保存来源</el-button>
      </el-form>
    </SettingCard>

    <SettingCard title="来源与任务记录" class="source-record-card">
      <el-table v-loading="loading" :data="pageSources" row-key="id">
        <el-table-column prop="name" label="来源" min-width="170" />
        <el-table-column prop="platform" label="平台" min-width="150" />
        <el-table-column prop="collector_key" label="连接器" min-width="150" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <StateTag :value="row.enabled ? '启用' : '禁用'" />
          </template>
        </el-table-column>
        <el-table-column label="任务" min-width="120">
          <template #default="{ row }"><StateTag :value="row.task?.status || '未创建'" /></template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="run(row)">运行一次</el-button>
            <el-button link @click="toggle(row)">
              {{ row.enabled ? "禁用" : "启用" }}
            </el-button>
            <el-button link type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <PaginationBar
        class="source-list-pagination"
        :page="pagination.pagination.page"
        :page-size="pagination.pagination.page_size"
        :total="pagination.pagination.total_items"
        :page-sizes="[5, 10, 20, 50]"
        @update:page="pagination.setPage"
        @update:page-size="pagination.setPageSize"
      />
    </SettingCard>
  </div>
</template>

<script setup>
import { computed, reactive, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import SettingCard from "./SettingCard.vue";
import StateTag from "../StateTag.vue";
import PaginationBar from "../PaginationBar.vue";
import * as api from "../../api/settings";
import { sourceHints } from "../../constants";
import { slicePage, useUrlPagination } from "../../composables/useUrlPagination";

const props = defineProps({ catalog: Array, sources: Array });
const emit = defineEmits(["refresh"]);
const loading = ref(false);
const pagination = useUrlPagination({
  prefix: "sources",
  defaultPageSize: 5,
  pageSizes: [5, 10, 20, 50],
});
const form = reactive({
  name: "本地授权目录实测",
  collector_key: "local_dir",
  mode: "manual",
  configText: '{"path":"C:/authorized/project-copy"}',
  enabled: true,
});
const hint = computed(() => (
  props.catalog?.find((item) => item.key === form.collector_key)?.config_hint
  || sourceHints[form.collector_key]
  || "{}"
));
const pageSources = computed(() => slicePage(props.sources || [], pagination.pagination).items);

watch(
  () => form.collector_key,
  () => {
    form.configText = sourceHints[form.collector_key]
      || props.catalog?.find((item) => item.key === form.collector_key)?.config_hint
      || "{}";
  },
);

watch(
  () => props.sources,
  (value) => {
    const total = Array.isArray(value) ? value.length : 0;
    pagination.pagination.total_items = total;
    pagination.pagination.total_pages = total
      ? Math.ceil(total / pagination.pagination.page_size)
      : 0;
  },
  { immediate: true },
);

async function create() {
  let config;
  try {
    config = JSON.parse(form.configText);
  } catch {
    ElMessage.error("配置必须是合法 JSON");
    return;
  }
  await api.createSource({
    name: form.name,
    collector_key: form.collector_key,
    config,
    mode: form.mode,
    interval_sec: 300,
    enabled: form.enabled,
  });
  ElMessage.success("来源已保存");
  emit("refresh");
}

async function run(row) {
  loading.value = true;
  try {
    await api.runSource(row.id);
    ElMessage.success("来源运行完成");
    emit("refresh");
  } finally {
    loading.value = false;
  }
}

async function toggle(row) {
  await api.patchSource(row.id, { enabled: !row.enabled });
  ElMessage.success("来源状态已更新");
  emit("refresh");
}

async function remove(row) {
  await ElMessageBox.confirm(
    "确认删除该来源？已有发现不会自动删除。",
    "删除确认",
  );
  await api.deleteSource(row.id);
  ElMessage.success("来源已删除");
  emit("refresh");
}
</script>
