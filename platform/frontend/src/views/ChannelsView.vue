<template>
  <AppLayout>
    <PageHeader title="渠道与规则" description="查看采集渠道和检测规则的真实运行证据。">
      <el-button :loading="loading" @click="load">刷新矩阵</el-button>
      <el-button type="primary" :loading="experimentRunningKey === '__all__'" @click="experiments">
        运行全部扩展性实验
      </el-button>
    </PageHeader>

    <div class="quick-links">
      <el-button plain @click="router.push('/settings#connections')">平台授权</el-button>
      <el-button plain @click="router.push('/settings#sources')">来源任务</el-button>
    </div>

    <el-card shadow="never" class="filter-card">
      <el-form :model="filters" inline class="filter-toolbar" @submit.prevent="applyFilters">
        <el-form-item label="关键词">
          <el-input v-model="filters.q" clearable placeholder="连接器、平台或证据" />
        </el-form-item>
        <el-form-item label="渠道">
          <el-select v-model="filters.channel" clearable placeholder="全部渠道">
            <el-option v-for="item in filterOptions.channel" :key="item" :label="item" :value="item" />
          </el-select>
        </el-form-item>
        <el-form-item label="平台">
          <el-select v-model="filters.platform" clearable placeholder="全部平台">
            <el-option v-for="item in filterOptions.platform" :key="item" :label="item" :value="item" />
          </el-select>
        </el-form-item>
        <el-form-item label="综合状态">
          <el-select v-model="filters.status" clearable placeholder="全部状态">
            <el-option v-for="item in filterOptions.status" :key="item" :label="displayState(item)" :value="item" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" native-type="submit">检索</el-button>
          <el-button @click="resetFilters">重置</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="card-heading">
          <span>渠道覆盖矩阵</span>
          <span class="muted">以已完成的任务为准</span>
        </div>
      </template>
      <el-table v-loading="loading" :data="collectorPageItems" row-key="name" class="channel-matrix" empty-text="暂无渠道数据">
        <el-table-column prop="name" label="连接器" min-width="150" />
        <el-table-column prop="channel" label="渠道" min-width="90" />
        <el-table-column prop="platform" label="平台" min-width="130" />
        <el-table-column label="运行模式" min-width="90">
          <template #default="{ row }">{{ modeLabels[row.mode] || row.mode }}</template>
        </el-table-column>
        <el-table-column label="代码实现" min-width="100">
          <template #default="{ row }"><StateTag :value="row.implementation_status" /></template>
        </el-table-column>
        <el-table-column label="离线验证" min-width="100">
          <template #default="{ row }"><StateTag :value="row.offline_test_status" /></template>
        </el-table-column>
        <el-table-column label="平台/载体实测" min-width="125">
          <template #default="{ row }"><StateTag :value="row.real_platform_test_status" /></template>
        </el-table-column>
        <el-table-column label="综合状态" min-width="125">
          <template #default="{ row }"><StateTag :value="row.status" /></template>
        </el-table-column>
        <el-table-column label="实测证据" min-width="225">
          <template #default="{ row }"><div class="matrix-evidence">{{ row.evidence_ref }}</div></template>
        </el-table-column>
      </el-table>
      <PaginationBar
        :page="collectorPagination.pagination.page"
        :page-size="collectorPagination.pagination.page_size"
        :total="collectorPagination.pagination.total_items"
        @update:page="collectorPagination.setPage"
        @update:page-size="collectorPagination.setPageSize"
      />
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="card-heading">
          <span>凭据规则</span>
          <span class="muted">规则命中后立即记录实测批次</span>
        </div>
      </template>
      <el-table :data="rulePageItems" row-key="id" empty-text="暂无规则数据">
        <el-table-column prop="id" label="规则 ID" min-width="185" />
        <el-table-column prop="title" label="规则名称" min-width="190" />
        <el-table-column prop="type" label="凭据类型" min-width="150" />
        <el-table-column prop="vendor" label="厂商" min-width="100" />
        <el-table-column prop="version" label="版本" width="75" />
        <el-table-column label="状态" min-width="135">
          <template #default="{ row }"><StateTag :value="row.status" /></template>
        </el-table-column>
        <el-table-column label="启用" width="82">
          <template #default="{ row }"><el-switch v-model="row.enabled" @change="toggle(row)" /></template>
        </el-table-column>
        <el-table-column label="证据" min-width="300">
          <template #default="{ row }">
            <div class="rule-evidence">{{ friendlyRuleEvidence(row) }}</div>
          </template>
        </el-table-column>
      </el-table>
      <PaginationBar
        :page="rulePagination.pagination.page"
        :page-size="rulePagination.pagination.page_size"
        :total="rulePagination.pagination.total_items"
        @update:page="rulePagination.setPage"
        @update:page-size="rulePagination.setPageSize"
      />
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="card-heading">
          <span>扩展性实验记录</span>
          <span class="muted">仅检查发布包运行时加载；真实平台实测请运行来源任务</span>
        </div>
      </template>
      <el-table :data="experimentPageItems" row-key="experiment_key">
        <el-table-column prop="connector_key" label="连接器" min-width="190" />
        <el-table-column prop="run_status" label="运行状态" min-width="120">
          <template #default="{ row }"><StateTag :value="row.run_status" /></template>
        </el-table-column>
        <el-table-column prop="runtime_check_status" label="发布包检查" min-width="120">
          <template #default="{ row }"><StateTag :value="row.runtime_check_status" /></template>
        </el-table-column>
        <el-table-column prop="code_lines" label="代码量" width="95" />
        <el-table-column prop="config_fields" label="配置项" width="95" />
        <el-table-column prop="evidence_ref" label="证据/备注" min-width="280" show-overflow-tooltip />
        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button
              link
              type="primary"
              :loading="experimentRunningKey === row.experiment_key"
              @click="runExperiment(row)"
            >
              运行此项
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <PaginationBar
        :page="experimentPagination.pagination.page"
        :page-size="experimentPagination.pagination.page_size"
        :total="experimentPagination.pagination.total_items"
        @update:page="experimentPagination.setPage"
        @update:page-size="experimentPagination.setPageSize"
      />
    </el-card>
  </AppLayout>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { ElMessage } from "element-plus";
import { useRoute, useRouter } from "vue-router";
import AppLayout from "../components/AppLayout.vue";
import PageHeader from "../components/PageHeader.vue";
import PaginationBar from "../components/PaginationBar.vue";
import StateTag from "../components/StateTag.vue";
import * as systemApi from "../api/system";
import * as ruleApi from "../api/rules";
import { slicePage, useUrlPagination } from "../composables/useUrlPagination";
import { displayState } from "../utils/format";

const router = useRouter();
const route = useRoute();
const data = ref({});
const rules = ref([]);
const loading = ref(false);
const filters = ref({
  q: String(route.query.channelQuery || ""),
  channel: String(route.query.channel || ""),
  platform: String(route.query.platform || ""),
  status: String(route.query.channelStatus || ""),
});
const filterOptions = ref({ channel: [], platform: [], status: [] });
const modeLabels = {
  offline: "离线导入",
  "offline-authorized": "授权离线",
  "authorized-artifact": "本地文件",
  "online-authorized": "授权接口",
  "online-public": "公开接口",
};
const coverageChannelOrder = [
  "Wiki",
  "微博",
  "代码托管平台",
  "知识共享平台",
  "容器镜像",
  "App",
  "小程序",
];
const coverageChannelAliases = {
  代码托管: "代码托管平台",
  知识共享: "知识共享平台",
  知识分享: "知识共享平台",
  应用包: "App",
};
const coverageChannelRanks = new Map(
  coverageChannelOrder.map((channel, index) => [channel, index]),
);
const collectorPagination = useUrlPagination({ prefix: "channels" });
const rulePagination = useUrlPagination({ prefix: "rules" });
const experimentPagination = useUrlPagination({ prefix: "experiments" });
const experimentRunningKey = ref("");
function channelSortRank(value) {
  const label = String(value || "").trim();
  const canonical = coverageChannelAliases[label] || label;
  if (coverageChannelRanks.has(canonical)) return coverageChannelRanks.get(canonical);
  return coverageChannelOrder.length;
}

const collectorPageItems = computed(() => {
  const sorted = [...(data.value.collectors || [])].sort((left, right) => (
    channelSortRank(left.channel) - channelSortRank(right.channel)
    || String(left.channel || "").localeCompare(String(right.channel || ""), "zh-CN")
    || String(left.platform || "").localeCompare(String(right.platform || ""), "zh-CN")
    || String(left.name || "").localeCompare(String(right.name || ""), "zh-CN")
  ));
  return slicePage(sorted, collectorPagination.pagination).items;
});
const rulePageItems = computed(() => slicePage(rules.value, rulePagination.pagination).items);
const experimentPageItems = computed(() => slicePage(data.value.extensibility_experiments || [], experimentPagination.pagination).items);

function setLocalTotals(items, pager) {
  pager.pagination.total_items = items.length;
  pager.pagination.total_pages = items.length ? Math.ceil(items.length / pager.pagination.page_size) : 0;
}

async function load() {
  loading.value = true;
  try {
    const [channels, ruleList] = await Promise.all([
      systemApi.getChannels({
        q: filters.value.q || undefined,
        channel: filters.value.channel || undefined,
        platform: filters.value.platform || undefined,
        status: filters.value.status || undefined,
      }),
      ruleApi.getRules(),
    ]);
    data.value = channels;
    rules.value = ruleList;
    filterOptions.value = channels.filters || { channel: [], platform: [], status: [] };
    setLocalTotals(data.value.collectors || [], collectorPagination);
    setLocalTotals(rules.value, rulePagination);
    setLocalTotals(data.value.extensibility_experiments || [], experimentPagination);
  } finally {
    loading.value = false;
  }
}

async function applyFilters() {
  await router.replace({
    query: {
      ...route.query,
      channelQuery: filters.value.q || undefined,
      channel: filters.value.channel || undefined,
      platform: filters.value.platform || undefined,
      channelStatus: filters.value.status || undefined,
    },
  });
  await collectorPagination.setPage(1);
  await load();
}

function resetFilters() {
  filters.value = { q: "", channel: "", platform: "", status: "" };
  applyFilters();
}

async function toggle(row) {
  const nextValue = Boolean(row.enabled);
  try {
    await ruleApi.toggleRule(row.id, nextValue);
    ElMessage.success("规则状态已更新");
    await load();
  } catch {
    row.enabled = !nextValue;
  }
}

async function experiments() {
  experimentRunningKey.value = "__all__";
  try {
    await ruleApi.runExperiments();
    ElMessage.success("全部扩展性实验已执行，结果已刷新");
    await load();
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || "扩展性实验执行失败");
  } finally {
    experimentRunningKey.value = "";
  }
}

async function runExperiment(row) {
  experimentRunningKey.value = row.experiment_key;
  try {
    await ruleApi.runExperiment(row.experiment_key);
    ElMessage.success(`${row.title} 已执行，结果已刷新`);
    await load();
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || "扩展性实验执行失败");
  } finally {
    experimentRunningKey.value = "";
  }
}

function friendlyRuleEvidence(row) {
  if (row.tested_at) {
    return row.test_count
      ? `已完成实际命中测试，发现 ${row.test_count} 条`
      : "已完成实际命中测试，当前未发现候选";
  }
  if (row.status === "已实测可运行" || row.offline_test_status === "已实测可运行") {
    return "内置正反样例实测通过；仅证明规则检测链路可运行，不代表凭据仍有效";
  }
  const evidence = String(row.evidence_ref || "");
  if (evidence.includes("受控夹具")) return "受控夹具已覆盖";
  if (evidence.includes("tests/") || evidence.includes("离线")) return "离线规则样例已覆盖";
  return "规则已注册，尚无专项测试";
}

onMounted(load);
</script>
