<template>
  <AppLayout>
    <main class="admins-page" v-loading="loading">
      <header class="admins-header"><h2>管理员</h2><el-button :icon="Refresh" @click="load">刷新</el-button></header>
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
      <section class="admin-create">
        <h3>创建管理员</h3>
        <el-form :model="form" label-position="top" @submit.prevent="create">
          <el-form-item label="用户名" :error="fieldErrors.username"><el-input v-model="form.username" maxlength="32" aria-label="新管理员用户名" autocomplete="off" /></el-form-item>
          <el-form-item label="初始密码" :error="fieldErrors.password"><el-input v-model="form.password" type="password" show-password maxlength="128" aria-label="新管理员初始密码" autocomplete="new-password" /></el-form-item>
          <el-form-item><el-button type="primary" native-type="submit" :icon="Plus" :loading="saving">创建管理员</el-button></el-form-item>
        </el-form>
      </section>
      <el-table v-if="!compact" :data="items" empty-text="暂无管理员">
        <el-table-column prop="username" label="用户名" min-width="150" />
        <el-table-column label="权限" min-width="140"><template #default="{ row }">{{ row.is_superadmin ? "超级管理员" : "管理员" }}</template></el-table-column>
        <el-table-column label="账号状态" min-width="130"><template #default="{ row }"><el-tag :type="row.is_active ? 'success' : 'danger'">{{ row.is_active ? "启用" : "停用" }}</el-tag></template></el-table-column>
        <el-table-column label="密码状态" min-width="150"><template #default="{ row }">{{ row.needs_password_change ? "首次登录待修改" : "已设置" }}</template></el-table-column>
        <el-table-column label="操作" width="125"><template #default="{ row }">
          <el-button v-if="!row.is_superadmin" :icon="row.is_active ? CircleClose : CircleCheck" text :type="row.is_active ? 'danger' : 'primary'" :disabled="saving" @click="setStatus(row)">{{ row.is_active ? "停用" : "启用" }}</el-button>
        </template></el-table-column>
      </el-table>
      <ul v-else class="record-list" aria-label="管理员列表">
        <li v-for="row in items" :key="row.id" class="record-item">
          <div class="record-heading"><div class="record-identity"><strong>{{ row.username }}</strong><span>{{ row.is_superadmin ? '超级管理员' : '管理员' }}</span></div><el-tag :type="row.is_active ? 'success' : 'danger'">{{ row.is_active ? '启用' : '停用' }}</el-tag></div>
          <dl class="record-fields"><div class="record-field-wide"><dt>密码状态</dt><dd>{{ row.needs_password_change ? '首次登录待修改' : '已设置' }}</dd></div></dl>
          <div v-if="!row.is_superadmin" class="record-actions"><el-button :icon="row.is_active ? CircleClose : CircleCheck" plain :type="row.is_active ? 'danger' : 'primary'" :disabled="saving" @click="setStatus(row)">{{ row.is_active ? '停用' : '启用' }}</el-button></div>
        </li>
      </ul>
      <div v-if="compact && !items.length" class="record-empty">暂无管理员</div>
    </main>
  </AppLayout>
</template>
<script setup>
import { onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { CircleCheck, CircleClose, Plus, Refresh } from "@element-plus/icons-vue";
import AppLayout from "../components/AppLayout.vue";
import { createAdmin, listAdmins, setAdminStatus } from "../api/auth";
import { admissionError, admissionFieldErrors } from "../api/admission";
import { useCompactLayout } from "../composables/useCompactLayout";

const compact = useCompactLayout();
const items = ref([]), loading = ref(false), saving = ref(false), error = ref(""), fieldErrors = ref({});
const form = reactive({ username: "", password: "" });
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
    form.username = ""; form.password = ""; error.value = "";
    ElMessage.success("管理员已创建，首次登录必须修改初始密码。"); await load();
  } catch (e) { error.value = admissionError(e); fieldErrors.value = admissionFieldErrors(e); }
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
.admins-header { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 20px; }
.admins-header h2 { margin: 0; font-size: 23px; }
.admin-create { padding: 18px 0; margin: 0 0 22px; border-block: 1px solid var(--line); }
.admin-create h3 { font-size: 16px; margin: 0 0 18px; }
.admin-create .el-form { display: grid; grid-template-columns: minmax(150px, 260px) minmax(180px, 300px) auto; align-items: end; gap: 16px; }
.admin-create :deep(.el-form-item) { margin-bottom: 0; }
@media (max-width: 800px) { .admin-create .el-form { grid-template-columns: minmax(0, 1fr); } }
</style>
