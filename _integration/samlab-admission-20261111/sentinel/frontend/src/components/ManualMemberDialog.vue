<template>
  <el-dialog v-model="visible" title="手动添加人员" width="480px" class="samlab-dialog management-dialog" align-center :close-on-click-modal="false" :close-on-press-escape="!busy" :show-close="!busy" @closed="clear">
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <el-form v-if="!preview" id="manual-member-form" :model="form" label-position="top" @submit.prevent="check">
      <el-form-item label="用户名" :error="fieldErrors.username"><el-input v-model="form.username" maxlength="100" aria-label="人员用户名" autocomplete="off" :disabled="busy" /></el-form-item>
      <el-form-item label="邮箱" :error="fieldErrors.email"><el-input v-model="form.email" maxlength="180" aria-label="人员邮箱" autocomplete="off" :disabled="busy" /></el-form-item>
      <el-form-item label="平台 ID" :error="fieldErrors.account_id"><el-input v-model="form.account_id" maxlength="19" inputmode="numeric" aria-label="人员平台 ID" autocomplete="off" :disabled="busy" /></el-form-item>
    </el-form>
    <template v-else>
      <PersonIdentity :name="item.username" :detail="item.email" />
      <dl class="manual-details"><div><dt>平台 ID</dt><dd>{{ item.account_id }}</dd></div><div><dt>准入</dt><dd>永久有效</dd></div></dl>
      <el-alert :title="item.message" :type="item.state === 'error' ? 'error' : item.state === 'restore' ? 'warning' : 'info'" :closable="false" show-icon />
    </template>
    <template #footer>
      <div :class="['manual-actions', { 'is-confirming': preview }]">
        <el-button :disabled="busy" @click="visible = false">取消</el-button>
        <el-button v-if="preview" :disabled="busy" @click="preview = null; error = ''">返回修改</el-button>
        <el-button v-if="!preview" type="primary" native-type="submit" form="manual-member-form" :icon="Search" :loading="busy">检查信息</el-button>
        <el-button v-else type="primary" :icon="Plus" :loading="busy" :disabled="!['add', 'update', 'restore'].includes(item.state)" @click="save">{{ item.state === "restore" ? "确认恢复" : "确认添加" }}</el-button>
      </div>
    </template>
  </el-dialog>
</template>
<script setup>
import { computed, reactive, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { Plus, Search } from "@element-plus/icons-vue";
import PersonIdentity from "./PersonIdentity.vue";
import { admissionApi as api, admissionError, admissionFieldErrors } from "../api/admission";

const props = defineProps({ modelValue: Boolean });
const emit = defineEmits(["update:modelValue", "saved"]);
const visible = computed({ get: () => props.modelValue, set: value => emit("update:modelValue", value) });
const form = reactive({ username: "", email: "", account_id: "" });
const busy = ref(false), error = ref(""), fieldErrors = ref({}), preview = ref(null);
const item = computed(() => preview.value?.rows[0]);
function clear() { Object.assign(form, { username: "", email: "", account_id: "" }); fieldErrors.value = {}; error.value = ""; preview.value = null; }
watch(() => props.modelValue, value => { if (value) clear(); });
async function check() {
  if (busy.value) return;
  fieldErrors.value = {}; error.value = "";
  const values = Object.fromEntries(Object.entries(form).map(([key, value]) => [key, value.trim()]));
  if (!values.username || /[\u0000-\u001f]/u.test(values.username) || /^[=+\-@]/u.test(values.username)) fieldErrors.value.username = "请输入 1 至 100 个字符的有效用户名。";
  if (!/^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}$/.test(values.email) || values.email.includes("..")) fieldErrors.value.email = "请输入有效邮箱地址。";
  if (!/^[1-9][0-9]{0,18}$/.test(values.account_id) || BigInt(values.account_id) > 9223372036854775807n) fieldErrors.value.account_id = "平台 ID 须为 1 至 9223372036854775807 的整数，不能含前导零。";
  if (Object.keys(fieldErrors.value).length) return;
  busy.value = true;
  try { preview.value = (await api.post("/admin/members/preview", values)).data; }
  catch (e) { error.value = admissionError(e); fieldErrors.value = admissionFieldErrors(e); }
  finally { busy.value = false; }
}
async function save() {
  if (busy.value || !preview.value || !["add", "update", "restore"].includes(item.value.state)) return;
  busy.value = true; error.value = "";
  try {
    const { data } = await api.post("/admin/roster/commit", { batch_id: preview.value.batch_id });
    ElMessage.success(data.summary.restore ? "已恢复永久白名单，远端权限以平台同步为准。" : data.summary.add ? "已添加至永久白名单。" : data.summary.update ? "已补录平台 ID。" : "该人员已在白名单中，未重复添加。");
    visible.value = false; emit("saved");
  } catch (e) { error.value = admissionError(e); }
  finally { busy.value = false; }
}
</script>
<style scoped>
.manual-details { display: grid; gap: 12px; margin: 20px 0; font-size: 13px; }
.manual-details div { display: flex; justify-content: space-between; gap: 16px; }
.manual-details dt { color: var(--muted-strong); }
.manual-details dd { margin: 0; overflow-wrap: anywhere; }
.el-alert { margin-bottom: 16px; }
.el-form-item :deep(.el-form-item__error) { position: static; width: 100%; padding-top: 6px; line-height: 1.5; }
.manual-actions { display: flex; justify-content: flex-end; gap: 8px; }
.manual-actions .el-button { margin-left: 0; }
@media (max-width: 400px) {
  .manual-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .manual-actions.is-confirming .el-button:last-child { grid-column: 1 / -1; }
}
</style>
