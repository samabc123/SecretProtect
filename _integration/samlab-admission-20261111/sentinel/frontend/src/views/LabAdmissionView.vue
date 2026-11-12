<template>
  <AppLayout table-page title="Samlab 人员准入" subtitle="实验室人员、注册审批与准入权限">
    <main class="samlab-admission" v-loading="loading">
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
      <div class="admission-toolbar">
        <el-input v-model="query" clearable :prefix-icon="Search" :aria-label="tab === 'audit' ? '搜索审计记录' : '搜索用户名或邮箱'" :placeholder="tab === 'audit' ? '操作人、关联编号或依据' : '用户名或邮箱'" />
        <div class="toolbar-actions">
          <span v-if="tab === 'members' && selectedMembers.length" class="selection-count" aria-live="polite">已选择 {{ selectedMembers.length }} 人</span>
          <el-tooltip content="刷新" placement="top"><el-button class="management-icon-button" aria-label="刷新" :icon="Refresh" :disabled="loading" @click="load" /></el-tooltip>
          <el-tooltip content="导出当前筛选结果" placement="top"><el-button :icon="Download" :disabled="!activeRows.length" @click="exportRows">导出名单</el-button></el-tooltip>
          <el-popover v-if="tab !== 'audit'" v-model:visible="filtersVisible" placement="bottom-end" :width="260" trigger="click" popper-class="admission-options">
            <template #reference><el-button :icon="Filter" :aria-expanded="filtersVisible" aria-label="筛选设置">筛选设置<span v-if="filterCount" class="filter-count">{{ filterCount }}</span></el-button></template>
            <el-form label-position="top">
              <el-form-item v-if="tab === 'requests'" label="审批状态"><el-select v-model="requestStatus" aria-label="审批状态"><el-option label="全部审批状态" value="" /><el-option v-for="(label, value) in decisionLabels" :key="value" :label="label" :value="value" /></el-select></el-form-item>
              <el-form-item label="人员来源"><el-select v-model="source" aria-label="人员来源"><el-option label="全部来源" value="" /><el-option v-for="(label, value) in sourceOptions" :key="value" :label="label" :value="value" /></el-select></el-form-item>
              <el-button text :disabled="!filterCount" @click="requestStatus = ''; source = ''">重置筛选</el-button>
            </el-form>
          </el-popover>
          <el-popover v-if="!compact" v-model:visible="columnsVisible" placement="bottom-end" :width="220" trigger="click" popper-class="admission-options">
            <template #reference><el-button :icon="Operation" aria-label="列设置" :aria-expanded="columnsVisible">列设置</el-button></template>
            <div class="column-options" role="group" aria-label="显示的表格列"><el-checkbox v-for="column in columnOptions" :key="column.key" :model-value="isColumnVisible(column.key)" @change="toggleColumn(column.key, $event)">{{ column.label }}</el-checkbox></div>
            <el-button text @click="hiddenColumns[tab] = []">恢复默认列</el-button>
          </el-popover>
          <el-button v-if="tab === 'members'" :icon="Delete" type="danger" :plain="!selectedMembers.length" :disabled="!selectedMembers.length || loading || saving" @click="openRemoval(selectedMembers)">批量删除</el-button>
          <el-button :icon="Plus" :disabled="saving" @click="manualVisible = true">手动添加</el-button>
          <el-button type="primary" :icon="Upload" @click="importVisible = true">导入名单</el-button>
        </div>
      </div>
      <div class="integration-strip" role="status">
        <span class="integration-notice"><el-icon><Warning /></el-icon>{{ data.integration.service_connected ? '隔离候选环境已连接；生产平台尚未发布此改造。' : '尚未收到近期服务连接，账号状态以实际回执为准。' }}</span>
        <a :href="data.platform.url" target="_blank" rel="noopener noreferrer">Samlab 平台<el-icon><TopRight /></el-icon></a>
      </div>
      <el-tabs v-model="tab" class="admission-tabs" @tab-change="filtersVisible = false; columnsVisible = false">
        <el-tab-pane :label="`注册申请（${pending.length} 待审批）`" name="requests">
          <el-table v-if="!compact" class="management-table" height="100%" :data="visibleRequests" :empty-text="emptyRequests" @sort-change="sortRows($event, 'requests')">
            <el-table-column label="用户" prop="email" sortable="custom" fixed="left" width="300"><template #default="{ row }"><PersonIdentity email-first :name="row.username" :detail="row.email" /></template></el-table-column>
            <el-table-column v-if="isColumnVisible('username')" label="用户名" prop="username" sortable="custom" min-width="150" />
            <el-table-column v-if="isColumnVisible('roster_match')" label="实验室名单" min-width="160"><template #default="{ row }"><span :class="['admission-state', row.roster_match ? 'is-success' : 'is-neutral']">{{ row.roster_match ? '名单内' : '未匹配导入名单' }}</span></template></el-table-column>
            <el-table-column v-if="isColumnVisible('source')" label="来源" min-width="160"><template #default="{ row }"><span class="source-label">{{ sourceLabels[row.source] || '用户申请' }}</span></template></el-table-column>
            <el-table-column v-if="isColumnVisible('submitted_at')" label="收到时间" prop="submitted_at" sortable="custom" min-width="180"><template #default="{ row }"><span class="table-time">{{ date(row.submitted_at) }}</span></template></el-table-column>
            <el-table-column v-if="isColumnVisible('status')" label="审批" width="120"><template #default="{ row }"><span :class="['admission-state', `is-${decisionType(row.status)}`]">{{ decisionLabels[row.status] }}</span></template></el-table-column>
            <el-table-column label="操作 / 审批人" fixed="right" width="132"><template #default="{ row }">
              <el-button v-if="row.status === 'pending'" :icon="EditPen" text type="primary" @click="openReview(row)">审批</el-button>
              <span v-else>{{ row.reviewed_by || '—' }}</span>
            </template></el-table-column>
          </el-table>
          <template v-else>
            <ul class="record-list" aria-label="注册申请列表">
              <li v-for="row in visibleRequests" :key="row.request_id" class="record-item">
                <div class="record-heading"><PersonIdentity :name="row.username" :detail="row.email" /><el-tag :type="decisionType(row.status)">{{ decisionLabels[row.status] }}</el-tag></div>
                <dl class="record-fields">
                  <div><dt>实验室名单</dt><dd>{{ row.roster_match ? '名单内' : '未匹配导入名单' }}</dd></div>
                  <div><dt>来源</dt><dd>{{ sourceLabels[row.source] || '用户申请' }}</dd></div>
                  <div class="record-field-wide"><dt>收到时间</dt><dd>{{ date(row.submitted_at) }}</dd></div>
                  <div v-if="row.reviewed_by"><dt>审批人</dt><dd>{{ row.reviewed_by }}</dd></div>
                </dl>
                <div v-if="row.status === 'pending'" class="record-actions"><el-button :icon="EditPen" type="primary" plain @click="openReview(row)">审批</el-button></div>
              </li>
            </ul>
            <div v-if="!visibleRequests.length" class="record-empty">{{ emptyRequests }}</div>
          </template>
        </el-tab-pane>
        <el-tab-pane label="白名单" name="members">
          <div v-if="compact && visibleMembers.length" class="member-actions">
            <el-checkbox :model-value="allPageSelected" :indeterminate="selectedMembers.length > 0 && !allPageSelected" @change="selectPage">选择本页</el-checkbox>
            <span>已选择 {{ selectedMembers.length }} 人</span>
          </div>
          <el-table v-if="!compact" class="management-table" height="100%" ref="memberTable" row-key="id" :data="visibleMembers" :default-sort="sorts.members.order ? sorts.members : memberDefaultSort" :empty-text="emptyMembers" @selection-change="selectedMembers = $event" @sort-change="sortRows($event, 'members')">
            <el-table-column type="selection" width="52" fixed="left" />
            <el-table-column label="用户" prop="email" sortable="custom" fixed="left" width="300"><template #default="{ row }"><PersonIdentity email-first :name="row.username" :detail="row.email" /></template></el-table-column>
            <el-table-column v-if="isColumnVisible('username')" label="用户名" prop="username" sortable="custom" min-width="150" />
            <el-table-column v-if="isColumnVisible('source')" label="来源" min-width="160"><template #default="{ row }"><span class="source-label">{{ sourceLabels[row.source] || '用户申请' }}</span></template></el-table-column>
            <el-table-column v-if="isColumnVisible('status')" label="准入" width="120"><template #default="{ row }"><span :class="['admission-state', row.status === 'active' ? 'is-success' : 'is-danger']">{{ row.status === "active" ? "永久有效" : "已停用" }}</span></template></el-table-column>
            <el-table-column v-if="isColumnVisible('account_id')" label="平台账号编号" prop="account_id" sortable="custom" min-width="150"><template #default="{ row }">{{ row.account_id || '尚未关联' }}</template></el-table-column>
            <el-table-column label="操作" fixed="right" width="110"><template #default="{ row }">
              <el-button :icon="Delete" text type="danger" :disabled="saving" @click="openRemoval([row])">删除</el-button>
            </template></el-table-column>
          </el-table>
          <template v-else>
            <ul class="record-list" aria-label="白名单人员列表">
              <li v-for="row in visibleMembers" :key="row.id" class="record-item">
                <div class="record-heading">
                  <el-checkbox :model-value="selectedIds.has(row.id)" :aria-label="`选择 ${row.email}`" @change="toggleMember(row, $event)" />
                  <PersonIdentity :name="row.username" :detail="row.email" />
                  <el-tag :type="row.status === 'active' ? 'success' : 'danger'">{{ row.status === 'active' ? '永久有效' : '已停用' }}</el-tag>
                </div>
                <dl class="record-fields">
                  <div><dt>来源</dt><dd>{{ sourceLabels[row.source] || '用户申请' }}</dd></div>
                  <div class="record-field-wide"><dt>平台账号编号</dt><dd>{{ row.account_id || '尚未关联' }}</dd></div>
                </dl>
                <div class="record-actions"><el-button :icon="Delete" type="danger" plain :disabled="saving" @click="openRemoval([row])">删除</el-button></div>
              </li>
            </ul>
            <div v-if="!visibleMembers.length" class="record-empty">{{ emptyMembers }}</div>
          </template>
        </el-tab-pane>
        <el-tab-pane label="审批审计" name="audit">
          <el-table v-if="!compact" class="management-table" height="100%" :data="visibleAudits" :empty-text="emptyAudits" @sort-change="sortRows($event, 'audit')">
            <el-table-column label="时间" prop="created_at" sortable="custom" fixed="left" width="200"><template #default="{ row }"><span class="table-time">{{ date(row.created_at) }}</span></template></el-table-column>
            <el-table-column v-if="isColumnVisible('actor')" prop="actor" label="操作人" sortable="custom" min-width="145" />
            <el-table-column v-if="isColumnVisible('action')" label="操作" min-width="210"><template #default="{ row }">{{ auditLabels[row.action] || row.action }}</template></el-table-column>
            <el-table-column v-if="isColumnVisible('subject')" prop="subject" label="关联编号" min-width="240" />
            <el-table-column v-if="isColumnVisible('note')" prop="note" label="依据" min-width="260" />
          </el-table>
          <template v-else>
            <ul class="record-list" aria-label="审批审计列表">
              <li v-for="row in visibleAudits" :key="row.id" class="record-item">
                <div class="record-identity"><strong>{{ auditLabels[row.action] || row.action }}</strong><span>{{ date(row.created_at) }}</span></div>
                <dl class="record-fields">
                  <div><dt>操作人</dt><dd>{{ row.actor }}</dd></div>
                  <div class="record-field-wide"><dt>关联编号</dt><dd>{{ row.subject || '—' }}</dd></div>
                  <div class="record-field-wide"><dt>依据</dt><dd>{{ row.note || '—' }}</dd></div>
                </dl>
              </li>
            </ul>
            <div v-if="!visibleAudits.length" class="record-empty">{{ emptyAudits }}</div>
          </template>
        </el-tab-pane>
      </el-tabs>
      <PaginationBar table-style v-model:page="currentPage" v-model:page-size="pageSize" :total="activeRows.length" />
      <footer class="admission-footnote">
        <span>{{ tab === 'audit' ? '审计列表最多显示最近 100 条' : tab === 'members' ? `列表最多显示 ${data.list_limit} 人` : `列表最多显示最近 ${data.list_limit} 条` }}<template v-if="data.legacy_people_count"> · 历史试点身份 {{ data.legacy_people_count }} 条，未自动映射</template></span>
        <span :title="updated">最近刷新 {{ updated ? updated.split(' ')[1] : '尚未刷新' }} · 服务签名{{ data.integration.signature_configured ? '已配置' : '未配置' }}</span>
      </footer>
      <el-dialog v-model="reviewVisible" title="注册准入审批" width="480px" class="samlab-dialog management-dialog" align-center :close-on-click-modal="false" :close-on-press-escape="!saving" :show-close="!saving">
        <PersonIdentity :name="selected?.username" :detail="selected?.email" />
        <el-alert v-if="dialogError" :title="dialogError" type="error" :closable="false" />
        <el-radio-group v-model="decision" aria-label="是否批准进入白名单"><el-radio value="approved">同意</el-radio><el-radio value="rejected">不同意</el-radio></el-radio-group>
        <template #footer><el-button :disabled="saving" @click="reviewVisible = false">取消</el-button><el-button type="primary" :loading="saving" :disabled="!decision" @click="review">确认审批</el-button></template>
      </el-dialog>
      <el-dialog v-model="removalVisible" :title="removalTargets.length > 1 ? '批量删除白名单人员' : '删除白名单人员'" width="560px" class="samlab-dialog management-dialog" align-center :close-on-click-modal="false" :close-on-press-escape="!saving" :show-close="!saving">
        <p class="subject">将删除 {{ removalTargets.length }} 名人员，并停止关联账号准入。</p>
        <ul class="removal-subjects" aria-label="待删除人员"><li v-for="row in removalTargets" :key="row.id">{{ row.username }} · {{ row.email }}</li></ul>
        <el-alert v-if="dialogError" :title="dialogError" type="error" :closable="false" />
        <el-form label-position="top"><el-form-item label="处理原因（5—500 字）"><el-input v-model="note" type="textarea" :rows="3" maxlength="500" aria-label="处理原因" /></el-form-item></el-form>
        <el-checkbox v-model="confirmed">确认删除所选人员，停止关联账号准入并保留审计</el-checkbox>
        <template #footer><el-button :disabled="saving" @click="removalVisible = false">取消</el-button><el-button type="danger" :icon="Delete" :loading="saving" :disabled="!confirmed || note.trim().length < 5" @click="remove">确认删除</el-button></template>
      </el-dialog>
      <RosterImportDialog v-model="importVisible" @imported="tab = 'members'; load()" />
      <ManualMemberDialog v-model="manualVisible" @saved="tab = 'members'; query = ''; source = ''; load()" />
    </main>
  </AppLayout>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { Delete, Download, EditPen, Filter, Operation, Plus, Refresh, Search, TopRight, Upload, Warning } from "@element-plus/icons-vue";
import RosterImportDialog from "../components/RosterImportDialog.vue";
import ManualMemberDialog from "../components/ManualMemberDialog.vue";
import AppLayout from "../components/AppLayout.vue";
import PaginationBar from "../components/PaginationBar.vue";
import PersonIdentity from "../components/PersonIdentity.vue";
import { useCompactLayout } from "../composables/useCompactLayout";
import { admissionApi as api, admissionError } from "../api/admission";
import { encodeCsv, sortedRows } from "../utils/admissionTable";

const data = reactive({ platform: { url: "https://researchchat.samlab.cn/" }, requests: [], members: [], audits: [], integration: {}, legacy_people_count: 0, list_limit: 500 });
const loading = ref(false), saving = ref(false), error = ref(""), query = ref(""), tab = ref("requests"), updated = ref("");
const selected = ref(null), decision = ref(""), reviewVisible = ref(false), dialogError = ref("");
const removalVisible = ref(false), removalTargets = ref([]), note = ref(""), confirmed = ref(false);
const memberTable = ref(null), selectedMembers = ref([]);
const importVisible = ref(false), manualVisible = ref(false);
const requestStatus = ref(""), source = ref("");
const filtersVisible = ref(false), columnsVisible = ref(false);
const hiddenColumns = reactive({ requests: [], members: [], audit: [] });
const columnDefinitions = {
  requests: [{ key: "username", label: "用户名" }, { key: "roster_match", label: "实验室名单" }, { key: "source", label: "来源" }, { key: "submitted_at", label: "收到时间" }, { key: "status", label: "审批" }],
  members: [{ key: "username", label: "用户名" }, { key: "source", label: "来源" }, { key: "status", label: "准入" }, { key: "account_id", label: "平台账号编号" }],
  audit: [{ key: "actor", label: "操作人" }, { key: "action", label: "操作" }, { key: "subject", label: "关联编号" }, { key: "note", label: "依据" }],
};
try {
  const stored = JSON.parse(localStorage.getItem("secretradar:admission-columns") || "{}");
  for (const key of Object.keys(hiddenColumns)) {
    if (Array.isArray(stored[key])) hiddenColumns[key] = stored[key].filter(value => columnDefinitions[key].some(column => column.key === value));
  }
} catch { /* Use the default columns if preferences are unavailable. */ }
watch(hiddenColumns, value => {
  try { localStorage.setItem("secretradar:admission-columns", JSON.stringify(value)); } catch { /* Keep in-memory preferences. */ }
}, { deep: true });
const columnOptions = computed(() => columnDefinitions[tab.value]);
const isColumnVisible = key => !hiddenColumns[tab.value].includes(key);
function toggleColumn(key, checked) {
  hiddenColumns[tab.value] = checked ? hiddenColumns[tab.value].filter(value => value !== key) : [...hiddenColumns[tab.value], key];
}
const filterCount = computed(() => Number(Boolean(source.value)) + Number(tab.value === "requests" && Boolean(requestStatus.value)));
const memberDefaultSort = { prop: "account_id", order: "descending" };
const sorts = reactive({ requests: {}, members: { ...memberDefaultSort }, audit: {} });
function sortRows({ prop, order }, target = tab.value) {
  sorts[target] = { prop, order };
  pages[target] = 1;
  clearSelection();
}
const pending = computed(() => data.requests.filter(r => r.status === "pending"));
const compact = useCompactLayout();
const pages = reactive({ requests: 1, members: 1, audit: 1 }), pageSize = ref(20);
const filteredRequests = computed(() => filtered(data.requests).filter(row => !requestStatus.value || row.status === requestStatus.value));
const filteredMembers = computed(() => filtered(data.members));
const filteredAudits = computed(() => {
  const q = query.value.trim().toLowerCase();
  return data.audits.filter(row => !q || `${row.actor} ${row.subject} ${row.note} ${auditLabels[row.action] || row.action}`.toLowerCase().includes(q));
});
const activeRows = computed(() => ({ requests: filteredRequests.value, members: filteredMembers.value, audit: filteredAudits.value })[tab.value]);
const currentPage = computed({ get: () => pages[tab.value], set: value => { pages[tab.value] = value; } });
const currentSort = computed(() => tab.value === "members" && !sorts.members.order ? memberDefaultSort : sorts[tab.value]);
const pageRows = rows => sortedRows(rows, currentSort.value).slice((currentPage.value - 1) * pageSize.value, currentPage.value * pageSize.value);
const visibleRequests = computed(() => tab.value === "requests" ? pageRows(filteredRequests.value) : []);
const visibleMembers = computed(() => tab.value === "members" ? pageRows(filteredMembers.value) : []);
const visibleAudits = computed(() => tab.value === "audit" ? pageRows(filteredAudits.value) : []);
const selectedIds = computed(() => new Set(selectedMembers.value.map(row => row.id)));
const allPageSelected = computed(() => visibleMembers.value.length > 0 && visibleMembers.value.every(row => selectedIds.value.has(row.id)));
const emptyRequests = computed(() => query.value.trim() || source.value || requestStatus.value ? "没有匹配的注册申请" : "暂无来自 Samlab 的注册申请");
const emptyMembers = computed(() => query.value.trim() || source.value ? "没有匹配的白名单人员" : "暂无已批准的 Samlab 人员");
const emptyAudits = computed(() => query.value.trim() ? "没有匹配的审批审计" : "暂无审批审计");
const decisionLabels = { pending: "待审批", approved: "已同意", rejected: "不同意" };
const sourceOptions = { self: "用户申请", admin_direct: "管理员直接创建", manual: "管理员手动添加", roster: "文件导入" };
const sourceLabels = { ...sourceOptions, admin_invite: "历史邀请记录" };
const auditLabels = { "whitelist.imported": "导入名单人员", "whitelist.id_imported": "补录平台 ID", "whitelist.manual_added": "管理员手动添加人员", "member.manual.committed": "确认手动添加批次", "whitelist.restored": "恢复白名单", "roster.import.committed": "确认名单导入批次", "application.received": "收到注册申请", "application.approved": "同意申请", "application.rejected": "不同意申请", "whitelist.approved": "批准加入白名单", "whitelist.admin_provisioned": "管理员直接授权白名单", "account.admin_created": "记录管理员创建账号", "whitelist.revoked": "历史撤销准入", "whitelist.deleted": "删除白名单人员", "whitelist.batch_deleted": "批量删除白名单人员", "whitelist.permanent_enabled": "转为永久白名单", "whitelist.legacy_deleted": "合并历史停用状态", "platform.active.acknowledged": "平台回执开通", "platform.disabled.acknowledged": "平台回执停用" };
const date = value => value ? new Date(value * 1000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : "";
const decisionType = value => ({ pending: "warning", approved: "success", rejected: "danger" })[value];
function filtered(rows) { const q = query.value.trim().toLowerCase(); return rows.filter(r => (!q || `${r.username} ${r.email}`.toLowerCase().includes(q)) && (!source.value || r.source === source.value)); }
function exportRows() {
  const rows = sortedRows(activeRows.value, currentSort.value);
  const csv = tab.value === "audit"
    ? [["时间", "操作人", "操作", "关联编号", "依据"], ...rows.map(row => [date(row.created_at), row.actor, auditLabels[row.action] || row.action, row.subject, row.note])]
    : tab.value === "members"
      ? [["邮箱", "用户名", "来源", "准入", "平台账号编号"], ...rows.map(row => [row.email, row.username, sourceLabels[row.source] || row.source, row.status === "active" ? "永久有效" : "已停用", row.account_id])]
      : [["邮箱", "用户名", "实验室名单", "来源", "收到时间", "审批", "审批人"], ...rows.map(row => [row.email, row.username, row.roster_match ? "名单内" : "未匹配导入名单", sourceLabels[row.source] || row.source, date(row.submitted_at), decisionLabels[row.status], row.reviewed_by])];
  const url = URL.createObjectURL(new Blob([encodeCsv(csv)], { type: "text/csv;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url; link.download = `samlab-${tab.value}.csv`; link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function load(background = false) {
  if (loading.value) return;
  loading.value = true;
  try {
    Object.assign(data, (await api.get("/admin/dashboard")).data);
    memberTable.value?.clearSelection();
    selectedMembers.value = [];
    updated.value = date(Date.now() / 1000); error.value = "";
  } catch (e) { error.value = admissionError(e); if (!background) ElMessage.error(error.value); }
  finally { loading.value = false; }
}
function openReview(row) { selected.value = row; decision.value = ""; dialogError.value = ""; reviewVisible.value = true; }
function openRemoval(rows) {
  if (!rows.length || saving.value) return;
  removalTargets.value = [...rows]; note.value = ""; confirmed.value = false; dialogError.value = ""; removalVisible.value = true;
}
function clearSelection() { memberTable.value?.clearSelection(); selectedMembers.value = []; }
function selectPage(checked) { selectedMembers.value = checked ? [...visibleMembers.value] : []; }
function toggleMember(row, checked) {
  selectedMembers.value = checked ? [...selectedMembers.value.filter(item => item.id !== row.id), row] : selectedMembers.value.filter(item => item.id !== row.id);
}
watch([query, tab, pageSize, requestStatus, source], () => { pages.requests = 1; pages.members = 1; pages.audit = 1; clearSelection(); });
watch([currentPage, compact], clearSelection);
watch(() => activeRows.value.length, total => { currentPage.value = Math.min(currentPage.value, Math.max(1, Math.ceil(total / pageSize.value))); });
async function review() {
  if (!decision.value || saving.value) return;
  saving.value = true;
  try {
    await api.post(`/admin/requests/${selected.value.request_id}/review`, { decision: decision.value });
    reviewVisible.value = false;
    ElMessage.success(decision.value === "approved" ? "已加入白名单，等待 Samlab 平台开通回执。" : "已拒绝本次申请。");
    await load();
  } catch (e) { dialogError.value = admissionError(e); }
  finally { saving.value = false; }
}
async function remove() {
  if (saving.value || !removalTargets.value.length || !confirmed.value || note.value.trim().length < 5) return;
  saving.value = true;
  try {
    const result = await api.post("/admin/members/batch-delete", { member_ids: removalTargets.value.map(row => row.id), note: note.value, confirmed: confirmed.value });
    removalVisible.value = false; ElMessage.success(result.data.message); await load();
  } catch (e) { dialogError.value = admissionError(e); }
  finally { saving.value = false; }
}
let timer;
onMounted(() => { load(); timer = window.setInterval(() => { if (!document.hidden && !reviewVisible.value && !removalVisible.value && !importVisible.value && !manualVisible.value && !filtersVisible.value && !columnsVisible.value && !selectedMembers.value.length) load(true); }, 15000); });
onUnmounted(() => window.clearInterval(timer));
</script>

<style scoped>
.samlab-admission { width: 100%; min-width: 0; letter-spacing: 0; color: #344254; }
.samlab-admission :deep(*) { letter-spacing: 0; }
.admission-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; margin-bottom: 16px; }
.admission-toolbar > .el-input { width: 280px; max-width: 100%; }
.toolbar-actions { display: flex; align-items: center; justify-content: flex-end; flex-wrap: wrap; gap: 8px; margin-left: auto; }
.toolbar-actions .el-button { min-height: 38px; border-radius: 8px; margin-left: 0; font-size: 13px; }
.toolbar-actions .management-icon-button { width: 38px; }
.selection-count { color: #526171; font-size: 12px; }
.filter-count { display: inline-grid; place-items: center; min-width: 18px; height: 18px; margin-left: 6px; border-radius: 50%; color: #07675f; background: #d9f7ee; font-size: 11px; }
.integration-strip { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px 16px; margin-bottom: 14px; color: #74602d; font-size: 12px; line-height: 1.6; }
.integration-notice { display: inline-flex; align-items: center; gap: 6px; }
.integration-notice .el-icon { flex: 0 0 15px; }
.integration-strip a { display: inline-flex; align-items: center; gap: 4px; color: #08756e; font-weight: 500; white-space: nowrap; }
.samlab-admission > .el-alert { margin-bottom: 12px; flex-shrink: 0; }
.admission-tabs :deep(.el-tabs__header) { margin-bottom: 14px; }
.admission-tabs :deep(.el-tabs__nav-wrap::after) { height: 1px; background: #e0e7eb; }
.admission-tabs :deep(.el-tabs__item) { font-size: 13px; font-weight: 500; }
.samlab-admission :deep(.management-table) { min-width: 0; --el-table-header-bg-color: #f8fafb; --el-table-row-hover-bg-color: #f1faf7; --el-table-border-color: #eef1f4; color: #48566a; }
.samlab-admission :deep(.management-table th.el-table__cell) { height: 52px; color: #526174; font-size: 13px; font-weight: 500; text-transform: none; }
.samlab-admission :deep(.management-table td.el-table__cell) { height: 78px; padding-block: 14px; font-size: 13px; }
.samlab-admission :deep(.management-table .cell) { padding-inline: 16px; overflow-wrap: anywhere; }
.samlab-admission :deep(.management-table .el-table__row:nth-child(even) td.el-table__cell) { background: #f9fafb; }
.samlab-admission :deep(.management-table .el-table__row:hover td.el-table__cell) { background: #f1faf7; }
.samlab-admission :deep(.management-table .el-table-fixed-column--right) { border-left: 1px solid #eef1f4; }
.samlab-admission :deep(.management-table .el-table-fixed-column--right .cell) { padding-inline: 8px; }
.samlab-admission :deep(.management-table .el-table-fixed-column--right .el-button) { padding-inline: 6px; margin: 0; }
.samlab-admission :deep(.el-table__body-wrapper) { scrollbar-gutter: stable; }
.source-label { display: inline-block; padding: 3px 8px; border-radius: 4px; background: #f0f2f5; color: #526171; font-size: 12px; line-height: 1.5; }
.admission-state { display: inline-flex; align-items: center; gap: 7px; font-size: 12px; line-height: 1.6; white-space: nowrap; }
.admission-state::before { content: ""; display: block; flex: 0 0 7px; height: 7px; border-radius: 50%; background: currentColor; }
.is-success { color: #16834d; }
.is-warning { color: #91650f; }
.is-danger { color: #bd353e; }
.is-neutral { color: #657384; }
.table-time { color: #677488; font-size: 12px; font-variant-numeric: tabular-nums; }
.admission-footnote { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 6px 16px; margin-top: 10px; color: #657384; font-size: 11px; line-height: 1.7; }
.column-options { display: grid; gap: 4px; margin-bottom: 8px; }
.column-options .el-checkbox { margin-right: 0; }
.subject { overflow-wrap: anywhere; font-size: 14px; }
.management-dialog :deep(.person-identity) { margin-bottom: 24px; }
.member-actions { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px 16px; margin: 0 0 14px; min-height: 34px; font-size: 13px; }
.member-actions > span { color: var(--muted-strong); }
.member-actions > .el-checkbox { margin-right: 0; }
.record-heading > .el-checkbox { margin-right: 0; padding-block: 2px; }
.samlab-admission :deep(.el-table .cell) { overflow-wrap: anywhere; }
.removal-subjects { max-height: 200px; overflow: auto; padding-left: 22px; font-size: 13px; line-height: 1.8; overflow-wrap: anywhere; }
:deep(.el-checkbox) { height: auto; white-space: normal; }
:deep(.el-checkbox__label) { white-space: normal; line-height: 1.7; }
@media (min-width: 901px) {
  .samlab-admission { display: flex; flex-direction: column; min-height: 0; }
  .admission-toolbar, .integration-strip, .admission-footnote { flex: 0 0 auto; }
  .admission-tabs { display: flex; flex-direction: column; flex: 1 1 0; min-height: 0; margin-bottom: 18px; }
  .admission-tabs :deep(.el-tabs__header) { flex: 0 0 auto; }
  .admission-tabs :deep(.el-tabs__content) { flex: 1 1 0; min-height: 0; overflow: hidden; }
  .admission-tabs :deep(.el-tab-pane) { height: 100%; }
}
@media (max-width: 900px) {
  .admission-toolbar { gap: 12px; }
  .admission-toolbar > .el-input { width: 100%; }
  .toolbar-actions { width: 100%; justify-content: flex-start; margin-left: 0; }
  .toolbar-actions .el-button { min-height: 40px; }
  .integration-strip { margin-top: 4px; }
  .admission-tabs { margin-bottom: 16px; }
  .samlab-admission :deep(.pagination-bar .el-pagination) { margin-left: 0; max-width: 100%; }
  .samlab-admission :deep(.pagination-bar .el-pagination__jump),
  .samlab-admission :deep(.pagination-bar .btn-first),
  .samlab-admission :deep(.pagination-bar .btn-last) { display: none; }
}
@media (prefers-reduced-motion: no-preference) {
  .admission-tabs :deep(.el-tabs__active-bar) { transition: transform 180ms cubic-bezier(.16, 1, .3, 1); }
}
</style>

<style>
.admission-options { --el-color-primary: #087f78; --el-color-primary-light-9: #edf9f6; }
.admission-options .el-checkbox__input.is-checked .el-checkbox__inner { background: #087f78; border-color: #087f78; }
.admission-options .el-checkbox__input.is-checked + .el-checkbox__label { color: #08756e; }
</style>
