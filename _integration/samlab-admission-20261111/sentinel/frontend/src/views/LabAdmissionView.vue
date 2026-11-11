<template>
  <AppLayout>
    <main class="samlab-admission" v-loading="loading">
      <header class="admission-header management-heading">
        <div><h2>Samlab 人员准入</h2><a :href="data.platform.url" target="_blank" rel="noopener noreferrer">{{ data.platform.url }}</a></div>
        <div class="admission-header-actions"><el-tooltip content="刷新" placement="top"><el-button class="management-icon-button" aria-label="刷新" :icon="Refresh" :disabled="loading" @click="load" /></el-tooltip><el-button type="primary" :icon="Upload" @click="importVisible = true">导入名单</el-button></div>
      </header>
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
      <el-alert :title="data.integration.service_connected ? '隔离候选环境已连接；生产平台尚未发布此改造。' : '尚未收到近期服务连接，账号状态以实际回执为准。'" type="warning" :closable="false" show-icon />
      <dl class="admission-status">
        <div><dt>待审批</dt><dd>{{ pending.length }}</dd></div>
        <div><dt>有效白名单</dt><dd>{{ data.members.filter(m => m.status === 'active').length }}</dd></div>
        <div><dt>待平台处理</dt><dd>{{ data.members.filter(m => ['pending_create', 'pending_revoke'].includes(m.sync_state)).length }}</dd></div>
        <div><dt>服务签名</dt><dd>{{ data.integration.signature_configured ? "已配置" : "未配置" }}</dd></div>
      </dl>
      <div class="admission-toolbar management-toolbar">
        <el-input v-model="query" clearable :prefix-icon="Search" :aria-label="tab === 'audit' ? '搜索审计记录' : '搜索用户名或邮箱'" :placeholder="tab === 'audit' ? '操作人、关联编号或依据' : '用户名或邮箱'" />
        <el-select v-if="tab === 'requests'" v-model="requestStatus" aria-label="审批状态" placeholder="全部审批状态">
          <el-option label="全部审批状态" value="" /><el-option v-for="(label, value) in decisionLabels" :key="value" :label="label" :value="value" />
        </el-select>
        <el-select v-if="tab !== 'audit'" v-model="source" aria-label="人员来源" placeholder="全部来源">
          <el-option label="全部来源" value="" /><el-option v-for="(label, value) in sourceLabels" :key="value" :label="label" :value="value" />
        </el-select>
        <span>最近成功刷新：{{ updated || "尚未刷新" }}</span>
      </div>
      <el-tabs v-model="tab">
        <el-tab-pane :label="`注册申请（${pending.length} 待审批）`" name="requests">
          <el-table v-if="!compact" class="management-table" :data="visibleRequests" :empty-text="emptyRequests">
            <el-table-column label="人员 / 邮箱" min-width="240"><template #default="{ row }"><PersonIdentity :name="row.username" :detail="row.email" /></template></el-table-column>
            <el-table-column label="实验室名单" min-width="125"><template #default="{ row }">{{ row.roster_match ? '名单内' : '未匹配导入名单' }}</template></el-table-column>
            <el-table-column label="来源" min-width="130"><template #default="{ row }">{{ sourceLabels[row.source] || '用户申请' }}</template></el-table-column>
            <el-table-column label="收到时间" min-width="170"><template #default="{ row }">{{ date(row.submitted_at) }}</template></el-table-column>
            <el-table-column label="审批" width="100"><template #default="{ row }"><el-tag :type="decisionType(row.status)">{{ decisionLabels[row.status] }}</el-tag></template></el-table-column>
            <el-table-column label="操作 / 审批人" min-width="130"><template #default="{ row }">
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
          <div class="member-actions">
            <span aria-live="polite">已选择 {{ selectedMembers.length }} 人</span>
            <el-button :icon="Delete" type="danger" plain :disabled="!selectedMembers.length || loading || saving" @click="openRemoval(selectedMembers)">批量删除</el-button>
            <el-checkbox v-if="compact && visibleMembers.length" :model-value="allPageSelected" :indeterminate="selectedMembers.length > 0 && !allPageSelected" @change="selectPage">选择本页</el-checkbox>
          </div>
          <el-table v-if="!compact" class="management-table" ref="memberTable" row-key="id" :data="visibleMembers" :empty-text="emptyMembers" @selection-change="selectedMembers = $event">
            <el-table-column type="selection" width="48" />
            <el-table-column label="人员 / 邮箱" min-width="250"><template #default="{ row }"><PersonIdentity :name="row.username" :detail="row.email" /></template></el-table-column>
            <el-table-column label="来源" min-width="130"><template #default="{ row }">{{ sourceLabels[row.source] || '用户申请' }}</template></el-table-column>
            <el-table-column label="准入" width="110"><template #default="{ row }"><el-tag :type="row.status === 'active' ? 'success' : 'danger'">{{ row.status === "active" ? "永久有效" : "已停用" }}</el-tag></template></el-table-column>
            <el-table-column label="平台状态" min-width="160"><template #default="{ row }">{{ platformState(row) }}</template></el-table-column>
            <el-table-column label="平台账号编号" min-width="120"><template #default="{ row }">{{ row.account_id || '尚未关联' }}</template></el-table-column>
            <el-table-column label="操作" width="110"><template #default="{ row }">
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
                  <div><dt>平台状态</dt><dd>{{ platformState(row) }}</dd></div>
                  <div class="record-field-wide"><dt>平台账号编号</dt><dd>{{ row.account_id || '尚未关联' }}</dd></div>
                </dl>
                <div class="record-actions"><el-button :icon="Delete" type="danger" plain :disabled="saving" @click="openRemoval([row])">删除</el-button></div>
              </li>
            </ul>
            <div v-if="!visibleMembers.length" class="record-empty">{{ emptyMembers }}</div>
          </template>
        </el-tab-pane>
        <el-tab-pane label="审批审计" name="audit">
          <el-table v-if="!compact" class="management-table" :data="visibleAudits" :empty-text="emptyAudits">
            <el-table-column label="时间" min-width="185"><template #default="{ row }">{{ date(row.created_at) }}</template></el-table-column>
            <el-table-column prop="actor" label="操作人" min-width="145" />
            <el-table-column label="操作" min-width="210"><template #default="{ row }">{{ auditLabels[row.action] || row.action }}</template></el-table-column>
            <el-table-column prop="subject" label="关联编号" min-width="240" />
            <el-table-column prop="note" label="依据" min-width="260" />
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
      <PaginationBar v-model:page="currentPage" v-model:page-size="pageSize" :total="activeRows.length" />
      <p class="admission-footnote">列表最多显示最近 {{ data.list_limit }} 条记录。历史试点身份 {{ data.legacy_people_count }} 条，原数据保留，未自动映射为 Samlab 账号。</p>
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
    </main>
  </AppLayout>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { Delete, EditPen, Refresh, Search, Upload } from "@element-plus/icons-vue";
import RosterImportDialog from "../components/RosterImportDialog.vue";
import AppLayout from "../components/AppLayout.vue";
import PaginationBar from "../components/PaginationBar.vue";
import PersonIdentity from "../components/PersonIdentity.vue";
import { useCompactLayout } from "../composables/useCompactLayout";
import { admissionApi as api, admissionError } from "../api/admission";

const data = reactive({ platform: { url: "https://researchchat.samlab.cn/" }, requests: [], members: [], audits: [], integration: {}, legacy_people_count: 0, list_limit: 500 });
const loading = ref(false), saving = ref(false), error = ref(""), query = ref(""), tab = ref("requests"), updated = ref("");
const selected = ref(null), decision = ref(""), reviewVisible = ref(false), dialogError = ref("");
const removalVisible = ref(false), removalTargets = ref([]), note = ref(""), confirmed = ref(false);
const memberTable = ref(null), selectedMembers = ref([]);
const importVisible = ref(false);
const requestStatus = ref(""), source = ref("");
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
const pageRows = rows => rows.slice((currentPage.value - 1) * pageSize.value, currentPage.value * pageSize.value);
const visibleRequests = computed(() => tab.value === "requests" ? pageRows(filteredRequests.value) : []);
const visibleMembers = computed(() => tab.value === "members" ? pageRows(filteredMembers.value) : []);
const visibleAudits = computed(() => tab.value === "audit" ? pageRows(filteredAudits.value) : []);
const selectedIds = computed(() => new Set(selectedMembers.value.map(row => row.id)));
const allPageSelected = computed(() => visibleMembers.value.length > 0 && visibleMembers.value.every(row => selectedIds.value.has(row.id)));
const emptyRequests = computed(() => query.value.trim() || source.value || requestStatus.value ? "没有匹配的注册申请" : "暂无来自 Samlab 的注册申请");
const emptyMembers = computed(() => query.value.trim() || source.value ? "没有匹配的白名单人员" : "暂无已批准的 Samlab 人员");
const emptyAudits = computed(() => query.value.trim() ? "没有匹配的审批审计" : "暂无审批审计");
const decisionLabels = { pending: "待审批", approved: "已同意", rejected: "不同意" };
const sourceLabels = { self: "用户申请", admin_direct: "管理员直接创建", admin_invite: "管理员邀请开通", roster: "文件导入" };
const accountLabels = { pending_create: "待平台开通", active: "平台已回执开通", pending_revoke: "待平台停用", disabled: "平台已回执停用" };
const platformState = row => row.awaiting_registration ? "待注册或确认申请" : accountLabels[row.sync_state] || "等待平台状态";
const auditLabels = { "whitelist.imported": "导入名单人员", "roster.import.committed": "确认名单导入批次", "application.received": "收到注册申请", "application.approved": "同意申请", "application.rejected": "不同意申请", "whitelist.approved": "批准加入白名单", "whitelist.admin_provisioned": "管理员直接授权白名单", "account.admin_created": "记录管理员创建账号", "whitelist.revoked": "历史撤销准入", "whitelist.deleted": "删除白名单人员", "whitelist.batch_deleted": "批量删除白名单人员", "whitelist.permanent_enabled": "转为永久白名单", "whitelist.legacy_deleted": "合并历史停用状态", "platform.active.acknowledged": "平台回执开通", "platform.disabled.acknowledged": "平台回执停用" };
const date = value => value ? new Date(value * 1000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : "";
const decisionType = value => ({ pending: "warning", approved: "success", rejected: "danger" })[value];
function filtered(rows) { const q = query.value.trim().toLowerCase(); return rows.filter(r => (!q || `${r.username} ${r.email}`.toLowerCase().includes(q)) && (!source.value || r.source === source.value)); }
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
onMounted(() => { load(); timer = window.setInterval(() => { if (!document.hidden && !reviewVisible.value && !removalVisible.value && !importVisible.value && !selectedMembers.value.length) load(true); }, 15000); });
onUnmounted(() => window.clearInterval(timer));
</script>

<style scoped>
.samlab-admission { min-width: 0; letter-spacing: 0; }
.samlab-admission :deep(.el-table) { min-width: 0; }
.admission-header-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.admission-header-actions .el-button + .el-button { margin-left: 0; }
.admission-header, .admission-toolbar { display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; margin-bottom: 20px; }
.admission-header h2 { margin: 0 0 8px; font-size: 23px; }
.admission-header a { font-size: 13px; overflow-wrap: anywhere; }
.samlab-admission :deep(.el-alert) { margin-bottom: 18px; }
.admission-status { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 24px; margin: 24px 0; padding: 22px 0; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.admission-status div { display: grid; gap: 8px; min-width: 0; }
.admission-status dt, .admission-toolbar span, .admission-footnote { color: var(--muted-strong); font-size: 13px; }
.admission-status dd { margin: 0; font-size: 22px; font-weight: 650; font-variant-numeric: tabular-nums; }
.admission-status div:first-child dd { color: #996519; }
.admission-status div:nth-child(2) dd { color: #167850; }
.admission-status div:last-child dd { font-size: 17px; min-height: 27px; display: flex; align-items: center; }
.admission-toolbar > span { margin-left: auto; font-size: 12px; }
.admission-toolbar .el-input { max-width: 350px; }
.admission-footnote { line-height: 1.7; margin: 18px 0 0; }
.subject { overflow-wrap: anywhere; font-size: 14px; }
.management-dialog :deep(.person-identity) { margin-bottom: 24px; }
.member-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 12px 16px; margin: 0 0 14px; min-height: 34px; font-size: 13px; }
.member-actions > span { color: var(--muted-strong); }
.member-actions > .el-checkbox { margin-left: auto; margin-right: 0; }
.record-heading > .el-checkbox { margin-right: 0; padding-block: 2px; }
.samlab-admission :deep(.el-table .cell) { overflow-wrap: anywhere; }
.removal-subjects { max-height: 200px; overflow: auto; padding-left: 22px; font-size: 13px; line-height: 1.8; overflow-wrap: anywhere; }
:deep(.el-checkbox) { height: auto; white-space: normal; }
:deep(.el-checkbox__label) { white-space: normal; line-height: 1.7; }
@media (max-width: 700px) {
  .admission-status { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 20px 12px; }
  .admission-status div { display: grid; gap: 6px; }
  .admission-toolbar { align-items: stretch; flex-direction: column; gap: 10px; }
  .admission-toolbar .el-input { max-width: none; }
  .admission-toolbar > span { margin-left: 0; }
  .admission-header { align-items: flex-start; gap: 16px; }
  .admission-header-actions { width: 100%; }
  .admission-header-actions .el-button { min-height: 40px; }
}
</style>
