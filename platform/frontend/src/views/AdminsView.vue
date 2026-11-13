<template>
  <AppLayout>
    <main class="admins-page" v-loading="loading">
      <header class="management-heading">
        <div><h2>管理员</h2><p class="management-subtitle">安全平台账号与访问权限</p></div>
        <div class="admins-actions">
          <el-tooltip content="刷新" placement="top"><el-button :icon="Refresh" class="management-icon-button" aria-label="刷新" :disabled="loading" @click="load" /></el-tooltip>
          <el-button type="primary" :icon="Plus" @click="openCreate">创建管理员</el-button>
        </div>
      </header>
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
      <dl class="management-summary">
        <div><dt>管理员总数</dt><dd>{{ items.length }}</dd></div>
        <div><dt>已启用</dt><dd>{{ items.filter(row => row.is_active).length }}</dd></div>
        <div><dt>待修改初始密码</dt><dd>{{ items.filter(row => row.needs_password_change).length }}</dd></div>
      </dl>
      <div class="management-toolbar">
        <el-input v-model="query" clearable :prefix-icon="Search" placeholder="搜索用户名" aria-label="搜索管理员" />
        <el-select v-model="status" aria-label="管理员状态" placeholder="全部状态"><el-option label="全部状态" value="" /><el-option label="启用" value="active" /><el-option label="停用" value="disabled" /></el-select>
      </div>
      <el-table v-if="!compact" class="management-table" :data="visibleItems" :empty-text="emptyText">
        <el-table-column label="用户名" min-width="180"><template #default="{ row }"><PersonIdentity :name="row.username" /></template></el-table-column>
        <el-table-column label="权限" min-width="140"><template #default="{ row }"><el-tag :type="row.is_superadmin ? 'primary' : 'info'" effect="plain">{{ row.is_superadmin ? "超级管理员" : "管理员" }}</el-tag></template></el-table-column>
        <el-table-column label="账号状态" min-width="130"><template #default="{ row }"><el-tag :type="row.is_active ? 'success' : 'danger'">{{ row.is_active ? "启用" : "停用" }}</el-tag></template></el-table-column>
        <el-table-column label="密码状态" min-width="150"><template #default="{ row }">{{ row.needs_password_change ? "首次登录待修改" : "已设置" }}</template></el-table-column>
        <el-table-column label="操作" width="125"><template #default="{ row }">
          <el-button v-if="!row.is_superadmin" :icon="row.is_active ? CircleClose : CircleCheck" text :type="row.is_active ? 'danger' : 'primary'" :disabled="saving" @click="setStatus(row)">{{ row.is_active ? "停用" : "启用" }}</el-button>
          <span v-else class="protected-account"><el-icon><Lock /></el-icon>受保护</span>
        </template></el-table-column>
      </el-table>
      <ul v-else class="record-list" aria-label="管理员列表">
        <li v-for="row in visibleItems" :key="row.id" class="record-item">
          <div class="record-heading"><PersonIdentity :name="row.username" :detail="row.is_superadmin ? '超级管理员' : '管理员'" /><el-tag :type="row.is_active ? 'success' : 'danger'">{{ row.is_active ? '启用' : '停用' }}</el-tag></div>
          <dl class="record-fields"><div class="record-field-wide"><dt>密码状态</dt><dd>{{ row.needs_password_change ? '首次登录待修改' : '已设置' }}</dd></div></dl>
          <div v-if="!row.is_superadmin" class="record-actions"><el-button :icon="row.is_active ? CircleClose : CircleCheck" plain :type="row.is_active ? 'danger' : 'primary'" :disabled="saving" @click="setStatus(row)">{{ row.is_active ? '停用' : '启用' }}</el-button></div>
        </li>
      </ul>
      <div v-if="compact && !visibleItems.length" class="record-empty">{{ emptyText }}</div>
      <PaginationBar v-model:page="page" v-model:page-size="pageSize" :total="filteredItems.length" />
      <el-dialog v-model="createVisible" title="创建管理员" width="480px" class="samlab-dialog management-dialog" align-center :close-on-click-modal="false" :close-on-press-escape="!saving" :show-close="!saving" @closed="clearForm">
        <el-alert v-if="createError" :title="createError" type="error" :closable="false" show-icon />
        <el-form id="create-admin-form" :model="form" label-position="top" @submit.prevent="create">
          <el-form-item label="用户名" :error="fieldErrors.username"><el-input v-model="form.username" maxlength="32" aria-label="新管理员用户名" autocomplete="off" /></el-form-item>
          <el-form-item label="初始密码" :error="fieldErrors.password"><el-input v-model="form.password" type="password" show-password maxlength="128" aria-label="新管理员初始密码" autocomplete="new-password" /></el-form-item>
        </el-form>
        <template #footer><el-button :disabled="saving" @click="createVisible = false">取消</el-button><el-button type="primary" native-type="submit" form="create-admin-form" :icon="Plus" :loading="saving">创建管理员</el-button></template>
      </el-dialog>
    </main>
  </AppLayout>
</template>
<script setup>
import { computed, onMounted, reactive, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { CircleCheck, CircleClose, Lock, Plus, Refresh, Search } from "@element-plus/icons-vue";
import AppLayout from "../components/AppLayout.vue";
import PersonIdentity from "../components/PersonIdentity.vue";
import PaginationBar from "../components/PaginationBar.vue";
import { createAdmin, listAdmins, setAdminStatus } from "../api/auth";
import { admissionError, admissionFieldErrors } from "../api/admission";
import { useCompactLayout } from "../composables/useCompactLayout";

const compact = useCompactLayout();
const items = ref([]), loading = ref(false), saving = ref(false), error = ref(""), fieldErrors = ref({});
const form = reactive({ username: "", password: "" });
const createVisible = ref(false), createError = ref(""), query = ref(""), status = ref(""), page = ref(1), pageSize = ref(20);
const filteredItems = computed(() => items.value.filter(row =>
  row.username.toLowerCase().includes(query.value.trim().toLowerCase()) &&
  (!status.value || (status.value === "active" ? row.is_active : !row.is_active))));
const visibleItems = computed(() => filteredItems.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value));
const emptyText = computed(() => query.value.trim() || status.value ? "没有匹配的管理员" : "暂无管理员");
watch([query, status, pageSize], () => { page.value = 1; });
watch(() => filteredItems.value.length, total => { page.value = Math.min(page.value, Math.max(1, Math.ceil(total / pageSize.value))); });
function clearForm() { form.username = ""; form.password = ""; fieldErrors.value = {}; createError.value = ""; }
function openCreate() { clearForm(); createVisible.value = true; }
async function load() {
  loading.value = true;
  try { items.value = (await listAdmins()).items; error.value = ""; }
  catch (e) { error.value = admissionError(e); }
  finally { loading.value = false; }
}
async function create() {
  if (saving.value) return;
  fieldErrors.value = {};
  if (!/^[A-Za-z0-9_][A-Za-z0-9_.-]{2,31}$/.test(form.username.trim())) fieldErrors.value.username = "请输入 3—32 位合法用户名。";
  if (form.password.length < 6 || form.password.length > 128) fieldErrors.value.password = "初始密码须为 6—128 位。";
  if (Object.keys(fieldErrors.value).length) return;
  saving.value = true;
  try {
    await createAdmin({ username: form.username.trim(), password: form.password });
    form.password = ""; createVisible.value = false; error.value = "";
    ElMessage.success("管理员已创建，首次登录必须修改初始密码。"); await load();
  } catch (e) { createError.value = admissionError(e); fieldErrors.value = admissionFieldErrors(e); }
  finally { saving.value = false; }
}
async function setStatus(row) {
  try { await ElMessageBox.confirm(`${row.is_active ? "停用" : "启用"}管理员 ${row.username}？原有登录会话将失效。`, "确认管理员状态变更", { type: "warning", confirmButtonText: "确认", cancelButtonText: "取消" }); }
  catch { return; }
  saving.value = true;
  try { await setAdminStatus(row.id, !row.is_active); await load(); ElMessage.success("管理员状态已更新。"); }
  catch (e) { error.value = admissionError(e); }
  finally { saving.value = false; }
}
onMounted(load);
</script>
<style scoped>
.admins-page { min-width: 0; letter-spacing: 0; }
.admins-page :deep(.el-table) { min-width: 0; }
.admins-page :deep(.el-table .cell) { overflow-wrap: anywhere; }
.admins-actions { display: flex; gap: 8px; }
.admins-actions .el-button + .el-button { margin-left: 0; }
.protected-account { display: inline-flex; align-items: center; gap: 6px; color: var(--muted-strong); font-size: 12px; }
</style>
