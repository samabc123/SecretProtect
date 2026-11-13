<template>
  <AppLayout>
    <PageHeader
      title="系统设置"
      :description="activeTab === 'account'
        ? '管理个人账号信息与安全设置，保障您的账号安全。'
        : '账号、平台授权、来源任务与检测边界。'"
    >
      <el-button @click="load">重新读取</el-button>
    </PageHeader>

    <el-tabs v-model="activeTab" :tab-position="compactSettings ? 'top' : 'left'" class="settings-tabs">
      <el-tab-pane v-for="item in settingsTabs" :key="item[0]" :name="item[0]" :label="item[1]">
        <template #label>
          <div class="tab-label">
            <strong>{{ item[1] }}</strong>
            <small>{{ item[2] }}</small>
          </div>
        </template>

        <section v-if="activeTab === 'account'" class="account-settings-page">
          <el-card shadow="never" class="account-card account-summary-card">
            <template #header>
              <div class="account-card-heading">
                <h3>账户信息</h3>
              </div>
            </template>
            <div class="account-summary">
              <div class="account-avatar" aria-hidden="true">
                <el-icon><UserFilled /></el-icon>
              </div>
              <div class="account-primary">
                <span class="account-label">用户名</span>
                <strong>{{ userStore.user?.username || "未登录用户" }}</strong>
                <p>{{ userStore.user?.permission_mode || "只读用户" }}</p>
              </div>
              <div class="account-facts">
                <div class="account-fact">
                  <span>权限模式</span>
                  <el-tag type="success" effect="plain">{{ userStore.user?.permission_mode || "只读用户" }}</el-tag>
                </div>
                <div class="account-fact">
                  <span>会话有效期</span>
                  <strong>8 小时</strong>
                </div>
              </div>
            </div>
          </el-card>

          <el-alert
            v-if="userStore.user?.needs_password_change"
            title="当前使用初始演示密码，请先修改。"
            type="warning"
            show-icon
            class="account-password-alert"
          />

          <el-card shadow="never" class="account-card password-card">
            <template #header>
              <div class="account-card-heading">
                <h3>修改密码</h3>
                <p>为保障账号安全，建议您定期更换密码。</p>
              </div>
            </template>
            <el-form
              ref="passwordRef"
              :model="password"
              :rules="passwordRules"
              label-width="110px"
              class="account-password-form"
            >
              <el-form-item label="当前密码" prop="current_password">
                <el-input v-model="password.current_password" type="password" show-password placeholder="请输入当前密码" />
              </el-form-item>
              <el-form-item label="新密码" prop="new_password">
                <el-input v-model="password.new_password" type="password" show-password placeholder="请输入新密码" />
              </el-form-item>
              <el-form-item label="确认新密码" prop="confirm_password">
                <el-input v-model="password.confirm_password" type="password" show-password placeholder="请再次输入新密码" />
              </el-form-item>
              <el-form-item class="account-password-action">
                <el-button type="primary" @click="changePwd">修改密码</el-button>
              </el-form-item>
            </el-form>
          </el-card>
        </section>

        <template v-else-if="activeTab === 'general'">
          <SettingStatus :runtime="payload.runtime" />
          <SettingCard title="数据源配置">
            <SettingField
              v-for="key in ['DATA_SOURCE', 'EVENTS_SOURCE', 'MONITORING_SOURCE', 'RESPONSE_SOURCE']"
              :key="key"
              :item="itemByKey(key)"
              v-model="values[key]"
            />
            <template #footer>
              <el-button type="primary" @click="saveGroup(['DATA_SOURCE', 'EVENTS_SOURCE', 'MONITORING_SOURCE', 'RESPONSE_SOURCE'])">
                保存数据源配置
              </el-button>
            </template>
          </SettingCard>
        </template>

        <template v-else-if="activeTab === 'connections'">
          <SettingCard title="共用采集配置" subtitle="GitHub / Gitee">
            <SettingField :item="itemByKey('CONNECTOR_NETWORK_ENABLED')" v-model="values.CONNECTOR_NETWORK_ENABLED" />
            <SettingField :item="itemByKey('CREDENTIAL_HMAC_KEY')" v-model="values.CREDENTIAL_HMAC_KEY" />
            <template #footer>
              <el-button type="primary" @click="saveGroup(['CONNECTOR_NETWORK_ENABLED', 'CREDENTIAL_HMAC_KEY'])">
                保存共用配置
              </el-button>
            </template>
          </SettingCard>
          <div class="content-grid two-columns">
            <PlatformCard title="GitHub 授权连接" prefix="GITHUB" :values="values" :item-by-key="itemByKey" @update-value="({ key, value }) => values[key] = value" @save="saveGroup" @test="testPlatform" />
            <PlatformCard title="Gitee 授权连接" prefix="GITEE" :values="values" :item-by-key="itemByKey" @update-value="({ key, value }) => values[key] = value" @save="saveGroup" @test="testPlatform" />
          </div>
          <SettingCard title="微博 OAuth API 授权连接" subtitle="Token 只用于官方 OAuth API，保存后不回显">
            <SettingField :item="itemByKey('WEIBO_ACCESS_TOKEN')" v-model="values.WEIBO_ACCESS_TOKEN" />
            <template #footer>
              <el-button type="primary" @click="saveGroup(['WEIBO_ACCESS_TOKEN'])">
                保存微博授权
              </el-button>
            </template>
          </SettingCard>
        </template>

        <template v-else-if="activeTab === 'sources'">
          <SourceManager :catalog="payload.source_catalog || []" :sources="sources" @refresh="loadSources" />
        </template>

        <template v-else-if="activeTab === 'budget'">
          <SettingCard title="采集预算">
            <SettingField
              v-for="key in ['CONNECTOR_TIMEOUT_SECONDS', 'CONNECTOR_MAX_FILES', 'CONNECTOR_MAX_FILE_BYTES', 'CONNECTOR_MIN_INTERVAL_SECONDS', 'CONNECTOR_MAX_RETRIES']"
              :key="key"
              :item="itemByKey(key)"
              v-model="values[key]"
            />
            <template #footer>
              <el-button type="primary" @click="saveGroup(['CONNECTOR_TIMEOUT_SECONDS', 'CONNECTOR_MAX_FILES', 'CONNECTOR_MAX_FILE_BYTES', 'CONNECTOR_MIN_INTERVAL_SECONDS', 'CONNECTOR_MAX_RETRIES'])">
                保存采集预算
              </el-button>
            </template>
          </SettingCard>
        </template>

        <template v-else-if="activeTab === 'verification'">
          <SettingCard title="授权凭据验证">
            <SettingField :item="itemByKey('VERIFICATION_NETWORK_ENABLED')" v-model="values.VERIFICATION_NETWORK_ENABLED" />
            <SettingField :item="itemByKey('VERIFICATION_TIMEOUT_SECONDS')" v-model="values.VERIFICATION_TIMEOUT_SECONDS" />
            <SettingField :item="itemByKey('DOCKERHUB_USERNAME')" v-model="values.DOCKERHUB_USERNAME" />
            <SettingField :item="itemByKey('VERIFICATION_ALLOWED_FINGERPRINTS')" v-model="values.VERIFICATION_ALLOWED_FINGERPRINTS" />
            <p class="muted">未开启联网或指纹不在白名单时，只记录无法判断，不会发起请求。平台专用验证器优先使用其固定身份端点。</p>
            <template #footer>
              <el-button type="primary" @click="saveGroup(['VERIFICATION_NETWORK_ENABLED', 'VERIFICATION_TIMEOUT_SECONDS', 'DOCKERHUB_USERNAME', 'VERIFICATION_ALLOWED_FINGERPRINTS'])">
                保存验证配置
              </el-button>
            </template>
          </SettingCard>
          <SettingCard
            title="明确授权验证端点"
            subtitle="按平台向导配置自有系统或平台端点；已有专用验证器的类型无需重复配置"
            class="endpoint-settings-card"
          >
            <AuthorizedEndpointBuilder
              v-model="authorizedEndpointsText"
              @open-connections="activeTab = 'connections'"
            />
            <template #footer>
              <el-button type="primary" @click="saveAuthorizedEndpoints">保存授权端点</el-button>
              <el-button @click="resetAuthorizedEndpoints">恢复当前配置</el-button>
            </template>
          </SettingCard>
        </template>

        <template v-else>
          <el-card shadow="never">
            <template #header>安全审计</template>
            <el-table v-loading="auditLoading" :data="auditPageItems">
              <el-table-column label="时间"><template #default="{ row }">{{ formatTimestamp(row.created_at) }}</template></el-table-column>
              <el-table-column prop="role" label="角色" />
              <el-table-column prop="action" label="动作" />
              <el-table-column prop="resource_type" label="资源" />
              <el-table-column prop="decision" label="决策"><template #default="{ row }"><StateTag :value="row.decision" /></template></el-table-column>
              <el-table-column prop="reason" label="原因" show-overflow-tooltip />
              <el-table-column prop="source_ip" label="来源地址" />
            </el-table>
            <PaginationBar
              :page="auditPagination.pagination.page"
              :page-size="auditPagination.pagination.page_size"
              :total="auditPagination.pagination.total_items"
              @update:page="auditPagination.setPage"
              @update:page-size="auditPagination.setPageSize"
            />
          </el-card>
        </template>
      </el-tab-pane>
    </el-tabs>
  </AppLayout>
</template>

<script setup>
import { useCompactLayout } from "../composables/useCompactLayout";
const compactSettings = useCompactLayout("(max-width: 1100px)");
import { computed, onMounted, reactive, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { UserFilled } from "@element-plus/icons-vue";
import { useRoute, useRouter } from "vue-router";
import AppLayout from "../components/AppLayout.vue";
import PageHeader from "../components/PageHeader.vue";
import PaginationBar from "../components/PaginationBar.vue";
import StateTag from "../components/StateTag.vue";
import SettingCard from "../components/settings/SettingCard.vue";
import SettingField from "../components/settings/SettingField.vue";
import PlatformCard from "../components/settings/PlatformCard.vue";
import SettingStatus from "../components/settings/SettingStatus.vue";
import SourceManager from "../components/settings/SourceManager.vue";
import AuthorizedEndpointBuilder from "../components/settings/AuthorizedEndpointBuilder.vue";
import { settingsTabs } from "../constants";
import * as api from "../api/settings";
import * as authApi from "../api/auth";
import * as systemApi from "../api/system";
import { useUserStore } from "../store/modules/user";
import { formatTimestamp } from "../utils/format";
import { slicePage, useUrlPagination } from "../composables/useUrlPagination";

const route = useRoute();
const router = useRouter();
const userStore = useUserStore();
const validTabNames = new Set(settingsTabs.map(([name]) => name));
const tabFromHash = (hash) => {
  const name = String(hash || "").replace(/^#/, "");
  return validTabNames.has(name) ? name : "account";
};
const activeTab = ref(tabFromHash(route.hash));
const payload = ref({ items: [], runtime: {} });
const sources = ref([]);
const audit = ref([]);
const auditLoading = ref(false);
const values = reactive({});
const authorizedEndpointsText = ref("[]");
const password = reactive({ current_password: "", new_password: "", confirm_password: "" });
const passwordRef = ref();
const auditPagination = useUrlPagination({ prefix: "audit" });
const auditPageItems = computed(() => slicePage(audit.value, auditPagination.pagination).items);
const passwordRules = {
  current_password: [{ required: true, message: "请输入当前密码" }],
  new_password: [{ required: true, min: 6, message: "新密码至少 6 位" }],
  confirm_password: [{
    validator: (_, value, callback) => value !== password.new_password
      ? callback(new Error("两次密码不一致"))
      : callback(),
  }],
};

function itemByKey(key) {
  return payload.value.items?.find((item) => item.key === key)
    || { key, label: key, description: "", kind: "text", options: [] };
}

function hydrate() {
  payload.value.items?.forEach((item) => {
    values[item.key] = item.sensitive
      ? ""
      : Array.isArray(item.value) ? item.value.join("\n") : item.value;
  });
  authorizedEndpointsText.value = JSON.stringify(
    payload.value.runtime?.authorized_verification_endpoints || [],
    null,
    2,
  );
}

async function load() {
  payload.value = await api.getSettings();
  hydrate();
  await Promise.all([loadSources(), loadAudit()]);
}

async function loadSources() {
  sources.value = await api.getSources();
}

async function loadAudit() {
  auditLoading.value = true;
  try {
    audit.value = (await systemApi.getAudit()).items || [];
    auditPagination.pagination.total_items = audit.value.length;
    auditPagination.pagination.total_pages = audit.value.length
      ? Math.ceil(audit.value.length / auditPagination.pagination.page_size)
      : 0;
  } finally {
    auditLoading.value = false;
  }
}

async function saveGroup(keys) {
  const body = {};
  keys.forEach((key) => {
    const item = itemByKey(key);
    if (!item.sensitive || values[key]) {
      body[key] = ["GITHUB_REPOSITORIES", "GITEE_REPOSITORIES", "VERIFICATION_ALLOWED_FINGERPRINTS"].includes(key)
        ? String(values[key] || "").split(/\r?\n/).map((value) => value.trim()).filter(Boolean)
        : values[key];
    }
  });
  const result = await api.saveSettings(body);
  if (result.csrf_token) document.querySelector('meta[name="csrf-token"]').content = result.csrf_token;
  ElMessage.success(`设置已保存：${(result.changed || []).join("、")}`);
  await load();
}

function resetAuthorizedEndpoints() {
  authorizedEndpointsText.value = JSON.stringify(
    payload.value.runtime?.authorized_verification_endpoints || [],
    null,
    2,
  );
}

async function saveAuthorizedEndpoints() {
  let endpoints;
  try {
    endpoints = JSON.parse(authorizedEndpointsText.value || "[]");
  } catch {
    ElMessage.error("授权端点必须是合法 JSON 数组");
    return;
  }
  if (!Array.isArray(endpoints)) {
    ElMessage.error("授权端点必须是 JSON 数组");
    return;
  }
  try {
    const result = await api.saveSettings({
      AUTHORIZED_VERIFICATION_ENDPOINTS: endpoints,
    });
    if (result.csrf_token) document.querySelector('meta[name="csrf-token"]').content = result.csrf_token;
    ElMessage.success(`授权端点已保存：${(result.changed || []).join("、")}`);
    await load();
  } catch {
    // The request interceptor shows the server-side reason.
  }
}

async function testPlatform(prefix, type) {
  const repository = String(values[`${prefix}_REPOSITORIES`] || "")
    .split(/\r?\n/)
    .map((value) => value.trim())
    .filter(Boolean);
  if (repository.length !== 1) return ElMessage.warning("请只保留一个授权仓库后测试");
  const result = type === "token"
    ? await api.testGiteeToken()
    : prefix === "GITEE"
      ? await api.testGitee(repository[0])
      : await api.testGithub(repository[0]);
  ElMessage.success(result.message || "测试通过");
}

async function changePwd() {
  const form = Array.isArray(passwordRef.value) ? passwordRef.value[0] : passwordRef.value;
  if (!form) return;
  await form.validate();
  const result = await authApi.changePassword(password);
  document.querySelector('meta[name="csrf-token"]').content = result.csrf_token;
  Object.keys(password).forEach((key) => { password[key] = ""; });
  ElMessage.success("密码已更新");
  await userStore.load();
}

watch(() => route.hash, (hash) => {
  const nextTab = tabFromHash(hash);
  if (activeTab.value !== nextTab) activeTab.value = nextTab;
});

watch(activeTab, (value) => {
  const nextHash = `#${validTabNames.has(value) ? value : "account"}`;
  if (route.hash !== nextHash) router.replace({ hash: nextHash });
});

onMounted(load);
</script>

<style scoped>
.account-settings-page { min-width: 0; }
.account-card { margin-bottom: 18px; }
.account-card-heading { display: grid; gap: 6px; }
.account-card-heading h3 { margin: 0; color: #1e2e43; font-size: 16px; font-weight: 760; }
.account-card-heading p { margin: 0; color: #8a98aa; font-size: 12px; font-weight: 400; }
.account-summary {
  display: grid;
  grid-template-columns: 110px minmax(0, 1fr) minmax(220px, .72fr);
  align-items: center;
  gap: 28px;
  min-height: 122px;
}
.account-avatar { display: grid; place-items: center; width: 88px; height: 88px; border-radius: 50%; background: #e5edf7; color: #7890ad; }
.account-avatar .el-icon { font-size: 50px; }
.account-primary { display: grid; gap: 7px; min-width: 0; }
.account-label, .account-fact > span { color: #516078; font-size: 13px; font-weight: 680; }
.account-primary strong { color: #324963; font-size: 16px; font-weight: 680; overflow-wrap: anywhere; }
.account-primary p { margin: 0; color: #8795a7; font-size: 12px; }
.account-facts { display: grid; gap: 18px; padding-left: 28px; border-left: 1px solid var(--line-soft); }
.account-fact { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.account-fact strong { color: #2d4058; font-size: 16px; font-weight: 680; white-space: nowrap; }
.account-fact :deep(.el-tag) { border-color: #bfe6cf; background: #eaf7ef; color: #27805a; }
.account-password-alert { margin: 0 0 18px; }
.password-card :deep(.el-card__body) { padding: 24px 20px 26px; }
.account-password-form { max-width: 610px; margin-top: 2px; }
.account-password-form :deep(.el-form-item) { margin-bottom: 14px; }
.account-password-form :deep(.el-form-item__label) { color: #526178; font-size: 13px; font-weight: 650; }
.account-password-form :deep(.el-input) { max-width: 430px; }
.account-password-form :deep(.el-input__wrapper) { min-height: 40px; background: #fcfdff; }
.account-password-action { margin-top: 8px; }
.account-password-action :deep(.el-button) { min-width: 96px; }
.endpoint-settings-card :deep(.settings-grid) { grid-template-columns: minmax(0, 1fr); }
@media (max-width: 900px) {
  .account-summary { grid-template-columns: 88px minmax(0, 1fr); }
  .account-facts { grid-column: 1 / -1; padding: 16px 0 0; border-top: 1px solid var(--line-soft); border-left: 0; }
}
@media (max-width: 560px) {
  .account-summary { grid-template-columns: 1fr; justify-items: start; gap: 16px; }
  .account-facts { width: 100%; }
  .account-password-form :deep(.el-form-item) { display: block; }
  .account-password-form :deep(.el-form-item__label) { display: block; width: auto !important; margin-bottom: 6px; text-align: left; }
  .account-password-form :deep(.el-input) { max-width: none; }
  .account-password-action { margin-top: 18px; }
}
</style>
