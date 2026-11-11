<template>
  <AppLayout>
    <main class="samlab-admission" v-loading="loading">
      <header class="admission-header">
        <div><h2>Samlab 人员准入</h2><a :href="data.platform.url" target="_blank" rel="noopener noreferrer">{{ data.platform.url }}</a></div>
        <div class="header-actions"><el-button :icon="Upload" @click="importVisible = true">导入名单</el-button><el-button :icon="Refresh" :disabled="loading" @click="load">刷新</el-button></div>
      </header>
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
      <el-alert :title="data.integration.service_connected ? '隔离候选环境已连接；生产平台尚未发布此改造。' : '尚未收到近期服务连接，账号状态以实际回执为准。'" type="warning" :closable="false" show-icon />
      <dl class="admission-status">
        <div><dt>待审批</dt><dd>{{ pending.length }}</dd></div>
        <div><dt>有效白名单</dt><dd>{{ data.members.filter(m => m.status === 'active' && !m.expired).length }}</dd></div>
        <div><dt>待平台处理</dt><dd>{{ data.members.filter(m => ['pending_create', 'pending_revoke'].includes(m.sync_state) || m.expired).length }}</dd></div>
        <div><dt>服务签名</dt><dd>{{ data.integration.signature_configured ? "已配置" : "未配置" }}</dd></div>
      </dl>
      <div class="admission-toolbar">
        <el-input v-model="query" clearable :prefix-icon="Search" aria-label="搜索用户名或邮箱" placeholder="用户名或邮箱" />
        <span>最近成功刷新：{{ updated || "尚未刷新" }}</span>
      </div>
      <el-tabs v-model="tab">
        <el-tab-pane :label="`注册申请（${pending.length} 待审批）`" name="requests">
          <el-table :data="filtered(data.requests)" empty-text="暂无来自 Samlab 的注册申请">
            <el-table-column prop="username" label="用户名" min-width="135" />
            <el-table-column prop="email" label="邮箱" min-width="230" />
            <el-table-column label="实验室名单" min-width="125"><template #default="{ row }">{{ row.roster_match ? '名单内' : '未匹配导入名单' }}</template></el-table-column>
            <el-table-column label="来源" min-width="165"><template #default="{ row }">{{ sourceLabels[row.source] || '用户申请' }}</template></el-table-column>
            <el-table-column label="收到时间" min-width="185"><template #default="{ row }">{{ date(row.submitted_at) }}</template></el-table-column>
            <el-table-column label="审批" min-width="105"><template #default="{ row }"><el-tag :type="decisionType(row.status)">{{ decisionLabels[row.status] }}</el-tag></template></el-table-column>
            <el-table-column label="操作" width="115" fixed="right"><template #default="{ row }">
              <el-button v-if="row.status === 'pending'" :icon="EditPen" text type="primary" @click="openReview(row)">审批</el-button>
              <span v-else>{{ row.reviewed_by }}</span>
            </template></el-table-column>
          </el-table>
        </el-tab-pane>
        <el-tab-pane label="白名单" name="members">
          <el-table :data="filtered(data.members)" empty-text="暂无已批准的 Samlab 人员">
            <el-table-column prop="username" label="用户名" min-width="135" />
            <el-table-column prop="email" label="邮箱" min-width="230" />
            <el-table-column label="来源" min-width="165"><template #default="{ row }">{{ sourceLabels[row.source] || '用户申请' }}</template></el-table-column>
            <el-table-column label="准入" min-width="105"><template #default="{ row }"><el-tag :type="row.expired || row.status !== 'active' ? 'danger' : 'success'">{{ row.expired ? "已到期" : row.status === "active" ? "有效" : "已撤销" }}</el-tag></template></el-table-column>
            <el-table-column label="平台状态" min-width="165"><template #default="{ row }">{{ row.expired ? "已到期，待平台核对" : row.awaiting_registration ? "待注册或确认申请" : accountLabels[row.sync_state] }}</template></el-table-column>
            <el-table-column prop="account_id" label="平台账号编号" min-width="140" />
            <el-table-column label="有效期" min-width="185"><template #default="{ row }">{{ date(row.expires_at) || '未设置' }}</template></el-table-column>
            <el-table-column label="操作" width="245" fixed="right"><template #default="{ row }">
              <el-button :icon="Calendar" text :disabled="row.status !== 'active' || row.expired" @click="openRenewal(row)">续期</el-button>
              <el-button :icon="CircleClose" text :disabled="row.status !== 'active'" @click="openRemoval(row, 'revoke')">撤销</el-button>
              <el-button :icon="Delete" text type="danger" @click="openRemoval(row, 'delete')">删除</el-button>
            </template></el-table-column>
          </el-table>
        </el-tab-pane>
        <el-tab-pane label="审批审计" name="audit">
          <el-table :data="data.audits" empty-text="暂无审批审计">
            <el-table-column label="时间" min-width="185"><template #default="{ row }">{{ date(row.created_at) }}</template></el-table-column>
            <el-table-column prop="actor" label="操作人" min-width="145" />
            <el-table-column label="操作" min-width="210"><template #default="{ row }">{{ auditLabels[row.action] || row.action }}</template></el-table-column>
            <el-table-column prop="subject" label="关联编号" min-width="240" />
            <el-table-column prop="note" label="依据" min-width="260" />
          </el-table>
        </el-tab-pane>
      </el-tabs>
      <p class="admission-footnote">列表最多显示最近 {{ data.list_limit }} 条记录。历史试点身份 {{ data.legacy_people_count }} 条，原数据保留，未自动映射为 Samlab 账号。</p>
      <el-dialog v-model="reviewVisible" title="注册准入审批" width="480px" class="samlab-dialog" :close-on-click-modal="false">
        <p class="subject">{{ selected?.username }}</p><p class="subject">{{ selected?.email }}</p>
        <el-alert v-if="dialogError" :title="dialogError" type="error" :closable="false" />
        <el-radio-group v-model="decision" aria-label="是否批准进入白名单"><el-radio value="approved">同意</el-radio><el-radio value="rejected">不同意</el-radio></el-radio-group>
        <el-form v-if="decision === 'approved'" label-position="top"><el-form-item label="有效期（可选）"><el-date-picker v-model="expiry" type="datetime" placeholder="选择到期时间" /></el-form-item></el-form>
        <template #footer><el-button :disabled="saving" @click="reviewVisible = false">取消</el-button><el-button type="primary" :loading="saving" :disabled="!decision" @click="review">确认审批</el-button></template>
      </el-dialog>
      <el-dialog v-model="renewalVisible" title="白名单续期" width="480px" class="samlab-dialog">
        <p class="subject">{{ selected?.username }} · {{ selected?.email }}</p>
        <el-alert v-if="dialogError" :title="dialogError" type="error" :closable="false" />
        <el-date-picker v-model="expiry" type="datetime" aria-label="新的到期时间" placeholder="新的到期时间" />
        <template #footer><el-button @click="renewalVisible = false">取消</el-button><el-button type="primary" :loading="saving" :disabled="!expiry" @click="renew">确认续期</el-button></template>
      </el-dialog>
      <el-dialog v-model="removalVisible" :title="removalAction === 'delete' ? '删除白名单人员' : '撤销准入'" width="480px" class="samlab-dialog" :close-on-click-modal="false">
        <p class="subject">{{ selected?.username }} · {{ selected?.email }}</p>
        <el-alert v-if="dialogError" :title="dialogError" type="error" :closable="false" />
        <el-form label-position="top"><el-form-item label="处理原因（5—500 字）"><el-input v-model="note" type="textarea" :rows="3" maxlength="500" aria-label="处理原因" /></el-form-item></el-form>
        <el-checkbox v-model="confirmed">确认停止该人员准入，等待平台执行停用并回执</el-checkbox>
        <template #footer><el-button :disabled="saving" @click="removalVisible = false">取消</el-button><el-button type="danger" :loading="saving" :disabled="!confirmed || note.trim().length < 5" @click="remove">确认处理</el-button></template>
      </el-dialog>
      <RosterImportDialog v-model="importVisible" @imported="tab = 'members'; load()" />
    </main>
  </AppLayout>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from "vue";
import { ElMessage } from "element-plus";
import { Calendar, CircleClose, Delete, EditPen, Refresh, Search, Upload } from "@element-plus/icons-vue";
import RosterImportDialog from "../components/RosterImportDialog.vue";
import AppLayout from "../components/AppLayout.vue";
import { admissionApi as api, admissionError } from "../api/admission";

const data = reactive({ platform: { url: "https://researchchat.samlab.cn/" }, requests: [], members: [], audits: [], integration: {}, legacy_people_count: 0, list_limit: 500 });
const loading = ref(false), saving = ref(false), error = ref(""), query = ref(""), tab = ref("requests"), updated = ref("");
const selected = ref(null), decision = ref(""), reviewVisible = ref(false), dialogError = ref("");
const removalVisible = ref(false), removalAction = ref(""), note = ref(""), confirmed = ref(false);
const renewalVisible = ref(false), expiry = ref(null);
const importVisible = ref(false);
const pending = computed(() => data.requests.filter(r => r.status === "pending"));
const decisionLabels = { pending: "待审批", approved: "已同意", rejected: "不同意" };
const sourceLabels = { self: "用户申请", admin_direct: "管理员直接创建", admin_invite: "管理员邀请开通", roster: "文件导入" };
const accountLabels = { pending_create: "待平台开通", active: "平台已回执开通", pending_revoke: "待平台停用", disabled: "平台已回执停用" };
const auditLabels = { "whitelist.imported": "导入名单人员", "roster.import.committed": "确认名单导入批次", "application.received": "收到注册申请", "application.approved": "同意申请", "application.rejected": "不同意申请", "whitelist.approved": "批准加入白名单", "whitelist.admin_provisioned": "管理员直接授权白名单", "account.admin_created": "记录管理员创建账号", "whitelist.revoked": "撤销准入", "whitelist.deleted": "删除白名单人员", "platform.active.acknowledged": "平台回执开通", "platform.disabled.acknowledged": "平台回执停用" };
const date = value => value ? new Date(value * 1000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : "";
const decisionType = value => ({ pending: "warning", approved: "success", rejected: "danger" })[value];
function filtered(rows) { const q = query.value.trim().toLowerCase(); return rows.filter(r => !q || `${r.username} ${r.email}`.toLowerCase().includes(q)); }
async function load(background = false) {
  if (loading.value) return;
  loading.value = true;
  try {
    Object.assign(data, (await api.get("/admin/dashboard")).data);
    updated.value = date(Date.now() / 1000); error.value = "";
  } catch (e) { error.value = admissionError(e); if (!background) ElMessage.error(error.value); }
  finally { loading.value = false; }
}
function openReview(row) { selected.value = row; decision.value = ""; expiry.value = null; dialogError.value = ""; reviewVisible.value = true; }
function openRenewal(row) { selected.value = row; expiry.value = null; dialogError.value = ""; renewalVisible.value = true; }
async function renew() {
  if (!expiry.value || saving.value) return;
  saving.value = true;
  try {
    await api.post(`/admin/members/${selected.value.id}/renew`, { expires_at: new Date(expiry.value).getTime() / 1000 });
    renewalVisible.value = false; ElMessage.success("已续期，等待平台同步。"); await load();
  } catch (e) { dialogError.value = admissionError(e); }
  finally { saving.value = false; }
}
function openRemoval(row, action) { selected.value = row; removalAction.value = action; note.value = ""; confirmed.value = false; dialogError.value = ""; removalVisible.value = true; }
async function review() {
  if (!decision.value || saving.value) return;
  saving.value = true;
  try {
    await api.post(`/admin/requests/${selected.value.request_id}/review`, { decision: decision.value, expires_at: decision.value === "approved" && expiry.value ? new Date(expiry.value).getTime() / 1000 : null });
    reviewVisible.value = false;
    ElMessage.success(decision.value === "approved" ? "已加入白名单，等待 Samlab 平台开通回执。" : "已拒绝本次申请。");
    await load();
  } catch (e) { dialogError.value = admissionError(e); }
  finally { saving.value = false; }
}
async function remove() {
  if (saving.value) return;
  saving.value = true;
  try {
    const result = await api.post(`/admin/members/${selected.value.id}/${removalAction.value}`, { note: note.value, confirmed: confirmed.value });
    removalVisible.value = false; ElMessage.success(result.data.message); await load();
  } catch (e) { dialogError.value = admissionError(e); }
  finally { saving.value = false; }
}
let timer;
onMounted(() => { load(); timer = window.setInterval(() => { if (!document.hidden && !reviewVisible.value && !removalVisible.value && !renewalVisible.value && !importVisible.value) load(true); }, 15000); });
onUnmounted(() => window.clearInterval(timer));
</script>

<style scoped>
.samlab-admission { min-width: 0; letter-spacing: 0; }
.samlab-admission :deep(.el-table) { min-width: 0; }
.header-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.admission-header, .admission-toolbar { display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; margin-bottom: 20px; }
.admission-header h2 { margin: 0 0 8px; font-size: 23px; }
.admission-header a { font-size: 13px; overflow-wrap: anywhere; }
.samlab-admission :deep(.el-alert) { margin-bottom: 18px; }
.admission-status { display: flex; flex-wrap: wrap; gap: 24px 36px; margin: 22px 0; padding: 16px 0; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.admission-status div { display: flex; align-items: baseline; gap: 10px; }
.admission-status dt, .admission-toolbar span, .admission-footnote { color: var(--muted-strong); font-size: 13px; }
.admission-status dd { margin: 0; font-size: 17px; font-weight: 650; font-variant-numeric: tabular-nums; }
.admission-toolbar .el-input { max-width: 350px; }
.admission-footnote { line-height: 1.7; margin: 18px 0 0; }
.subject { overflow-wrap: anywhere; font-size: 14px; }
:deep(.el-checkbox) { height: auto; white-space: normal; }
:deep(.el-checkbox__label) { white-space: normal; line-height: 1.7; }
@media (max-width: 700px) { .admission-status { gap: 16px; } .admission-toolbar { align-items: stretch; flex-direction: column; } }
</style>
<style>.samlab-dialog { max-width: calc(100vw - 28px); border-radius: 6px; }</style>
