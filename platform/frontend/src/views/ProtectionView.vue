<template>
  <AppLayout :title="title" subtitle="Samlab / 实验室安全管理">
    <main class="protect-page" v-loading="loading">
      <div class="protect-toolbar">
        <div class="protect-heading"><span class="eyebrow">SecertProtect</span><h2>{{ title }}</h2></div>
        <div class="protect-actions">
          <span class="refresh-time">最近同步 {{ date(state.last_success) }}</span>
          <el-tooltip content="刷新本地记录"><el-button :icon="Refresh" aria-label="刷新本地记录" :disabled="loading" @click="load" /></el-tooltip>
          <el-button type="primary" :icon="Connection" :loading="syncing" :disabled="!state.configured" @click="sync">同步引擎</el-button>
        </div>
      </div>
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
      <div class="engine-strip" role="status">
        <span class="engine-indicator" :class="live ? 'online' : 'offline'" /><strong>{{ connectionLabel }}</strong>
        <span>{{ state.error || (state.stale ? '尚无近期引擎数据；当前记录不能证明防护有效。' : '引擎数据已同步；拦截效果仍以执行组件验收为准。') }}</span>
        <router-link v-if="mode !== 'engine'" to="/engine">接入状态<el-icon><ArrowRight /></el-icon></router-link>
      </div>
      <template v-if="mode === 'overview'">
        <section class="protect-metrics" aria-label="防护指标">
          <div><span>待确认攻击事件</span><strong>{{ metric(state.counts?.new) }}</strong><small>本地已接收记录</small></div>
          <div><span>引擎活动决策</span><strong>{{ metric(state.decisions_count) }}</strong><small>最近一次同步快照</small></div>
          <div><span>已确认事件</span><strong>{{ metric(state.counts?.confirmed) }}</strong><small>管理员人工确认</small></div>
          <div><span>邮件发送成功</span><strong>{{ state.notifications?.sent || 0 }}</strong><small>{{ state.notifications_configured ? 'SMTP 已配置' : 'SMTP 未配置' }}</small></div>
        </section>
        <div class="protect-columns">
          <section class="protect-section">
            <div class="section-heading"><h3>最近攻击事件</h3><router-link to="/attacks">查看全部<el-icon><ArrowRight /></el-icon></router-link></div>
            <el-table :data="events.slice(0, 6)" empty-text="暂无引擎事件" @row-click="inspect">
              <el-table-column prop="scenario" label="检测场景" min-width="180" show-overflow-tooltip /><el-table-column prop="source" label="来源" min-width="140" />
              <el-table-column label="处置" width="105"><template #default="{ row }"><el-tag :type="tagType(row.status)" effect="plain">{{ statuses[row.status] }}</el-tag></template></el-table-column>
            </el-table>
          </section>
          <section class="protect-section">
            <div class="section-heading"><h3>组件状态</h3><router-link to="/engine">详情</router-link></div>
            <ul class="component-list">
              <li><el-icon><Connection /></el-icon><div><strong>CrowdSec 引擎</strong><span>日志分析与防护决策</span></div><el-tag :type="live ? 'success' : 'warning'">{{ connectionLabel }}</el-tag></li>
              <li><el-icon><Timer /></el-icon><div><strong>独立同步服务</strong><span>事件同步与邮件发送</span></div><el-tag :type="state.worker_online ? 'success' : 'info'">{{ state.worker_online ? '运行中' : '未运行' }}</el-tag></li>
              <li><el-icon><Lock /></el-icon><div><strong>拦截执行组件</strong><span>Bouncer</span></div><el-tag type="info">未验收</el-tag></li>
              <li><el-icon><UserFilled /></el-icon><div><strong>人员准入</strong><span>永久白名单与审批</span></div><router-link to="/admission">管理</router-link></li>
            </ul>
          </section>
        </div>
        <section class="protect-section">
          <div class="section-heading"><h3>安全工作区</h3></div>
          <div class="workspace-links">
            <router-link to="/admission"><el-icon><UserFilled /></el-icon><div><strong>实验室人员准入</strong><span>名单导入 / 手动添加 / 注册审批</span></div><el-icon><ArrowRight /></el-icon></router-link>
            <router-link to="/findings"><el-icon><Key /></el-icon><div><strong>凭证泄露检测</strong><span>检测记录 / 脱敏证据 / 修复跟踪</span></div><el-icon><ArrowRight /></el-icon></router-link>
            <router-link to="/security-audit"><el-icon><Document /></el-icon><div><strong>防护审计与通知</strong><span>操作留痕 / 通知投递记录</span></div><el-icon><ArrowRight /></el-icon></router-link>
          </div>
        </section>
      </template>
      <template v-else-if="mode === 'engine'">
        <section class="protect-section">
          <div class="section-heading"><h3>CrowdSec 连接</h3><el-tag effect="plain">v{{ state.version }}</el-tag></div>
          <dl class="engine-facts">
            <div><dt>机器身份</dt><dd>{{ state.configured ? '已配置凭据文件' : '未配置' }}</dd></div><div><dt>最近连接</dt><dd>{{ date(state.last_attempt) }}</dd></div>
            <div><dt>最近成功同步</dt><dd>{{ date(state.last_success) }}</dd></div><div><dt>连续失败</dt><dd>{{ state.failures || 0 }}</dd></div>
            <div><dt>独立同步服务</dt><dd>{{ state.worker_online ? '运行中' : '未运行' }}</dd></div><div><dt>同步间隔</dt><dd>{{ state.sync_interval }} 秒</dd></div>
            <div><dt>解封权限</dt><dd>{{ state.remediation_enabled ? '服务器已启用' : '只读' }}</dd></div><div><dt>拦截效果</dt><dd>尚未完成真实入口验收</dd></div>
          </dl>
        </section>
        <section class="protect-section"><div class="section-heading"><h3>模块与边界</h3></div><el-table :data="modules"><el-table-column prop="name" label="模块" min-width="160" /><el-table-column prop="capability" label="职责" min-width="220" /><el-table-column prop="dependency" label="接入条件" min-width="260" /></el-table></section>
        <section class="protect-section"><div class="section-heading"><h3>接入要求</h3></div><ul class="setup-notes">
          <li>由服务器运维配置专用机器身份（CROWDSEC_CREDENTIALS_FILE）和只读决策查询密钥文件（CROWDSEC_BOUNCER_KEY_FILE）。</li><li>配置授权日志来源与检测规则，再单独安装 Bouncer；引擎连接成功不代表网站已经受到拦截保护。</li>
          <li>仅同步最近 {{ state.alert_window }} 条引擎事件。本地保留已接收记录；停机期间高流量事件可能超出同步窗口。</li><li>邮件凭据仅从服务器配置读取，不在浏览器中录入或显示。</li>
        </ul></section>
      </template>
      <template v-else-if="mode === 'attacks' || mode === 'decisions'">
        <div class="table-toolbar">
          <el-input v-model="query" :prefix-icon="Search" clearable placeholder="搜索场景、来源或编号" aria-label="搜索事件" />
          <el-select v-if="mode === 'attacks'" v-model="filter" aria-label="处置状态"><el-option label="全部状态" value="" /><el-option v-for="(label, value) in statuses" :key="value" :label="label" :value="value" /></el-select>
          <el-button :icon="Download" :disabled="!filtered.length" @click="download">导出脱敏记录</el-button>
        </div>
        <el-table :data="paged" empty-text="暂无记录" class="protect-table">
          <el-table-column prop="id" label="编号" width="85" /><el-table-column :prop="mode === 'attacks' ? 'source' : 'value'" label="来源" min-width="160" /><el-table-column prop="scenario" label="检测场景" min-width="230" show-overflow-tooltip />
          <template v-if="mode === 'attacks'">
            <el-table-column label="模式" width="100"><template #default="{ row }">{{ row.simulated ? '模拟场景' : '普通场景' }}</template></el-table-column>
            <el-table-column label="处置状态" width="115"><template #default="{ row }"><el-tag :type="tagType(row.status)" effect="plain">{{ statuses[row.status] }}</el-tag></template></el-table-column>
            <el-table-column label="操作" width="100" fixed="right"><template #default="{ row }"><el-button text type="primary" :icon="View" @click="inspect(row)">详情</el-button></template></el-table-column>
          </template>
          <template v-else>
            <el-table-column label="决策" width="100"><template #default="{ row }">{{ decisionTypes[row.type] || row.type }}</template></el-table-column><el-table-column prop="duration" label="快照剩余时长" min-width="150" />
            <el-table-column label="操作" width="110" fixed="right"><template #default="{ row }"><el-button text type="danger" :icon="Unlock" :disabled="!state.remediation_enabled || state.stale" @click="removal = row; note = ''; confirmed = false">申请解封</el-button></template></el-table-column>
          </template>
        </el-table>
        <div class="protect-pagination"><span>共 {{ filtered.length }} 条{{ mode === 'attacks' ? ' · 最多显示 1000 条本地事件' : '' }}</span><el-pagination v-model:current-page="page" :page-size="20" :total="filtered.length" layout="prev, pager, next" /></div>
      </template>
      <template v-else>
        <el-tabs v-model="auditTab">
          <el-tab-pane label="操作审计" name="audit"><el-table :data="audits" empty-text="暂无防护操作记录"><el-table-column label="时间" min-width="180"><template #default="{ row }">{{ date(row.created_at) }}</template></el-table-column><el-table-column prop="actor" label="操作人" min-width="130" /><el-table-column label="操作" min-width="190"><template #default="{ row }">{{ actions[row.action] || row.action }}</template></el-table-column><el-table-column prop="subject" label="关联编号" width="100" /><el-table-column prop="note" label="记录" min-width="220" show-overflow-tooltip /></el-table></el-tab-pane>
          <el-tab-pane label="邮件投递" name="notifications"><el-alert v-if="!state.notifications_configured" title="SMTP 未配置，待发送记录不会被标记为已送达。" type="warning" :closable="false" show-icon /><el-table :data="notifications" empty-text="暂无投递记录"><el-table-column prop="event_id" label="事件编号" width="110" /><el-table-column label="状态" min-width="130"><template #default="{ row }">{{ deliveryStates[row.status] || row.status }}</template></el-table-column><el-table-column prop="attempts" label="尝试次数" width="110" /><el-table-column label="发送时间" min-width="180"><template #default="{ row }">{{ date(row.sent_at) }}</template></el-table-column><el-table-column prop="last_error" label="失败原因" min-width="240" /></el-table></el-tab-pane>
        </el-tabs>
      </template>
      <footer class="protect-foot"><span>数据来源：{{ state.last_success ? 'CrowdSec API 同步快照' : '尚未接入引擎数据' }}</span><span>管理服务 0.1.0</span></footer>
      <el-drawer :model-value="!!selected" title="攻击事件详情" size="min(540px, 100vw)" @close="selected = null">
        <template v-if="selected"><h3>{{ selected.scenario }}</h3><dl class="event-detail"><div><dt>编号</dt><dd>{{ selected.id }}</dd></div><div><dt>来源</dt><dd>{{ selected.source }}</dd></div><div><dt>引擎事件数</dt><dd>{{ selected.events_count }}</dd></div><div><dt>首次接收</dt><dd>{{ date(selected.first_seen) }}</dd></div><div><dt>引擎记录时间</dt><dd>{{ selected.created_at || '未提供' }}</dd></div></dl>
          <el-form label-position="top"><el-form-item label="处置状态"><el-select v-model="reviewState" aria-label="更新处置状态"><el-option v-for="(label, value) in statuses" :key="value" :label="label" :value="value" /></el-select></el-form-item><el-button type="primary" :icon="Check" :loading="saving" @click="review">保存处置</el-button></el-form>
        </template>
      </el-drawer>
      <el-dialog :model-value="!!removal" title="确认申请解封" width="min(480px, calc(100vw - 32px))" :close-on-click-modal="false" @close="!saving && (removal = null)">
        <p>决策 #{{ removal?.id }} · {{ removal?.value }}</p><el-form label-position="top"><el-form-item label="处理原因（5—300 字）"><el-input v-model="note" type="textarea" maxlength="300" /></el-form-item></el-form><el-checkbox v-model="confirmed">确认删除此条引擎决策</el-checkbox>
        <template #footer><el-button :disabled="saving" @click="removal = null">取消</el-button><el-button type="danger" :loading="saving" :disabled="!confirmed || note.trim().length < 5" @click="removeDecision">确认解封</el-button></template>
      </el-dialog>
    </main>
  </AppLayout>
</template>
<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { ElMessage } from "element-plus";
import { ArrowRight, Check, Connection, Document, Download, Key, Lock, Refresh, Search, Timer, Unlock, UserFilled, View } from "@element-plus/icons-vue";
import AppLayout from "../components/AppLayout.vue";
import request from "../utils/request";
const route = useRoute();
const titles = { overview: "安全总览", attacks: "攻击事件", decisions: "封禁决策", engine: "引擎与接入", "security-audit": "防护审计与通知" };
const mode = computed(() => route.path.slice(1) || "overview"), title = computed(() => titles[mode.value]);
const state = ref({}), events = ref([]), decisions = ref([]), audits = ref([]), notifications = ref([]);
const loading = ref(false), syncing = ref(false), saving = ref(false), error = ref("");
const query = ref(""), filter = ref(""), page = ref(1), selected = ref(null), reviewState = ref("new"), removal = ref(null), confirmed = ref(false), note = ref(""), auditTab = ref("audit");
const statuses = { new: "待确认", confirmed: "已确认", false_positive: "误报", resolved: "已处理" };
const decisionTypes = { ban: "封禁", captcha: "验证码挑战" };
const deliveryStates = { pending: "待发送", sending: "发送中", sent: "发送成功", retry: "等待重试", failed: "发送失败" };
const actions = { "engine.sync.started": "开始同步", "engine.sync.succeeded": "同步成功", "engine.sync.failed": "同步失败", "event.review": "更新事件处置", "decision.delete.requested": "申请解封", "decision.delete.accepted": "引擎接受解封", "decision.delete.failed": "解封失败" };
const modules = [
  { name: "CrowdSec 引擎", capability: "日志分析、攻击检测与决策", dependency: "机器身份、授权日志和检测规则" },
  { name: "AppSec / Bouncer", capability: "请求检查与决策执行", dependency: "反向代理适配与真实入口验证" },
  { name: "人员准入", capability: "永久白名单、导入、审批与审计", dependency: "Samlab 后端签名接口；未完成生产验收" },
  { name: "凭证检测", capability: "授权来源采集与凭证风险识别", dependency: "授权目标、独立检测 worker 与规则验证" },
  { name: "邮件通知", capability: "持久化发送队列、去重与重试", dependency: "SMTP over TLS、实际收件验证" },
];
const live = computed(() => state.value.connection === "connected" && !state.value.stale);
const connectionLabel = computed(() => ({ not_configured: "引擎未配置", pending: "等待连接", error: "连接失败", connected: state.value.stale ? "数据已过期" : "引擎已连接" })[state.value.connection] || "正在读取");
const filtered = computed(() => (mode.value === "decisions" ? decisions.value : events.value).filter(row => (!filter.value || row.status === filter.value) && [row.id, row.scenario, row.source, row.value].join(" ").toLowerCase().includes(query.value.toLowerCase())));
const paged = computed(() => filtered.value.slice((page.value - 1) * 20, page.value * 20));
watch([query, filter, mode], () => { page.value = 1; });
watch(mode, () => { query.value = ""; filter.value = ""; selected.value = null; load(); });
function date(value) { return value ? new Date(typeof value === "number" ? value * 1000 : value).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : "尚无记录"; }
function metric(value) { return state.value.last_success ? value || 0 : "—"; }
function tagType(status) { return { new: "warning", confirmed: "danger", false_positive: "info", resolved: "success" }[status]; }
async function load() {
  loading.value = true; error.value = "";
  try {
    const [s, e, d, a, n] = await Promise.all(["status", "events", "decisions", "audit", "notifications"].map(path => request.get(`/protection/${path}`)));
    state.value = s; events.value = e.items; decisions.value = d.items; audits.value = a.items; notifications.value = n.items;
  } catch (e) { error.value = e.response?.data?.detail || "无法读取防护记录。"; }
  finally { loading.value = false; }
}
async function sync() { syncing.value = true; try { await request.post("/protection/sync"); ElMessage.success("引擎数据已同步"); } catch {} finally { syncing.value = false; await load(); } }
function inspect(row) { selected.value = row; reviewState.value = row.status; }
async function review() { saving.value = true; try { await request.patch(`/protection/events/${selected.value.id}`, { status: reviewState.value }); selected.value = null; await load(); } catch {} finally { saving.value = false; } }
async function removeDecision() { saving.value = true; try { const result = await request.post(`/protection/decisions/${removal.value.id}/delete`, { confirmed: confirmed.value, note: note.value.trim() }); ElMessage.success(result.message); removal.value = null; await load(); } catch {} finally { saving.value = false; } }
function download() {
  const blob = new Blob([JSON.stringify({ source: "CrowdSec", snapshot_at: state.value.last_success, items: filtered.value }, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob), a = document.createElement("a"); a.href = url; a.download = `secertprotect-${mode.value}.json`; a.click(); URL.revokeObjectURL(url);
}
onMounted(load);
</script>
