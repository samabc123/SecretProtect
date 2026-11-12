<template>
  <el-dialog v-model="visible" title="导入实验室名单" width="760px" class="roster-dialog management-dialog" align-center :close-on-click-modal="false" :close-on-press-escape="!busy" :show-close="!busy">
    <div class="file-toolbar">
      <el-button :icon="Download" :disabled="busy" @click="download">下载 CSV 模板</el-button>
      <label class="file-picker">名单文件（用户名、邮箱、ID；最大 1 MiB）
        <input ref="input" type="file" accept=".csv,text/csv" :disabled="busy" aria-label="名单文件" @change="selectFile">
      </label>
    </div>
    <el-alert title="ID 须填写 Samlab 已有用户 ID；导入记录不代表已核实远端账号，也不会创建账号。" type="info" :closable="false" show-icon />
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <template v-if="preview">
      <dl class="import-summary">
        <div><dt>可新增</dt><dd>{{ preview.summary.add }}</dd></div>
        <div><dt>可补录 ID</dt><dd>{{ preview.summary.update }}</dd></div>
        <div><dt>可恢复</dt><dd>{{ preview.summary.restore }}</dd></div>
        <div><dt>重复跳过</dt><dd>{{ preview.summary.skip }}</dd></div>
        <div><dt>无效行</dt><dd>{{ preview.summary.error }}</dd></div>
      </dl>
      <el-table v-if="!compact" class="management-table" :data="preview.rows" max-height="360" empty-text="暂无名单">
        <el-table-column prop="line" label="行" width="60" />
        <el-table-column prop="email" label="邮箱" min-width="220" />
        <el-table-column prop="username" label="用户名" min-width="120" />
        <el-table-column prop="account_id" label="平台 ID" min-width="100" />
        <el-table-column label="结果" min-width="240"><template #default="{ row }"><el-tag :type="stateTypes[row.state]">{{ states[row.state] }}</el-tag> {{ row.message }}</template></el-table-column>
      </el-table>
      <ul v-else class="record-list" aria-label="名单导入预览">
        <li v-for="row in preview.rows" :key="row.line" class="record-item">
          <div class="record-heading"><div class="record-identity"><strong>第 {{ row.line }} 行 · {{ row.username || '未填写用户名' }}</strong><span>{{ row.email }}</span></div><el-tag :type="stateTypes[row.state]">{{ states[row.state] }}</el-tag></div>
          <dl class="record-fields"><div class="record-field-wide"><dt>平台 ID</dt><dd>{{ row.account_id || '未填写' }}</dd></div></dl>
          <p class="preview-result">{{ row.message }}</p>
        </li>
      </ul>
    </template>
    <template #footer>
      <el-button :disabled="busy" @click="visible = false">取消</el-button>
      <el-button type="primary" :icon="Upload" :loading="busy" :disabled="!preview || preview.summary.error > 0 || preview.summary.add + preview.summary.update + preview.summary.restore === 0" @click="commit">确认导入</el-button>
    </template>
  </el-dialog>
</template>
<script setup>
import { computed, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { Download, Upload } from "@element-plus/icons-vue";
import { admissionApi as api, admissionError } from "../api/admission";
import { useCompactLayout } from "../composables/useCompactLayout";

const props = defineProps({ modelValue: Boolean });
const emit = defineEmits(["update:modelValue", "imported"]);
const visible = computed({ get: () => props.modelValue, set: value => emit("update:modelValue", value) });
const preview = ref(null), error = ref(""), busy = ref(false), input = ref(null);
const states = { add: "新增", update: "补录 ID", restore: "恢复", skip: "跳过", error: "无效" };
const stateTypes = { add: "success", update: "warning", restore: "warning", skip: "info", error: "danger" };
const compact = useCompactLayout();
watch(() => props.modelValue, value => { if (value) { preview.value = null; error.value = ""; if (input.value) input.value.value = ""; } });
async function selectFile(event) {
  preview.value = null; error.value = "";
  const file = event.target.files?.[0];
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".csv") || file.size > 1024 * 1024) {
    error.value = "请选择不超过 1 MiB 的 CSV 文件。"; return;
  }
  busy.value = true;
  try { preview.value = (await api.post("/admin/roster/preview", file, { headers: { "Content-Type": "text/csv" } })).data; }
  catch (e) { error.value = admissionError(e); }
  finally { busy.value = false; }
}
async function download() {
  busy.value = true; error.value = "";
  try {
    const response = await api.get("/admin/roster/template", { responseType: "blob" });
    const url = URL.createObjectURL(response.data), a = document.createElement("a");
    a.href = url; a.download = "samlab-roster.csv"; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch { error.value = "模板下载失败，请稍后重试。"; }
  finally { busy.value = false; }
}
async function commit() {
  if (busy.value || !preview.value || preview.value.summary.error) return;
  busy.value = true; error.value = "";
  try {
    const { data } = await api.post("/admin/roster/commit", { batch_id: preview.value.batch_id });
    ElMessage.success(`已新增 ${data.summary.add} 人，补录 ID ${data.summary.update} 人，恢复 ${data.summary.restore} 人，跳过 ${data.summary.skip} 行。`);
    visible.value = false; emit("imported");
  } catch (e) { error.value = admissionError(e); }
  finally { busy.value = false; }
}
</script>
<style scoped>
.file-toolbar { display: flex; flex-wrap: wrap; gap: 16px; align-items: center; margin-bottom: 18px; }
.file-picker { display: grid; gap: 8px; min-width: 0; font-size: 13px; }
.file-picker input { max-width: 100%; }
.import-summary { display: flex; flex-wrap: wrap; gap: 24px; padding: 16px 0; }
.import-summary div { display: flex; gap: 10px; align-items: baseline; }
.import-summary dd { margin: 0; font-weight: 600; font-variant-numeric: tabular-nums; }
.el-alert { margin-bottom: 12px; }
.preview-result { margin: 12px 0 0; color: var(--muted-strong); font-size: 13px; line-height: 1.7; overflow-wrap: anywhere; }
:deep(.el-table .cell) { overflow-wrap: anywhere; }
@media (max-width: 700px) {
  .file-toolbar { align-items: stretch; flex-direction: column; }
  .file-toolbar .el-button { align-self: flex-start; min-height: 40px; }
  .import-summary { gap: 12px 20px; }
}
</style>
