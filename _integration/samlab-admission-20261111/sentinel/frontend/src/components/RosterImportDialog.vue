<template>
  <el-dialog v-model="visible" title="导入实验室名单" width="760px" class="roster-dialog" :close-on-click-modal="false">
    <div class="file-toolbar">
      <el-button :icon="Download" :disabled="busy" @click="download">下载 CSV 模板</el-button>
      <label class="file-picker">名单文件（CSV，最大 1 MiB）
        <input ref="input" type="file" accept=".csv,text/csv" :disabled="busy" aria-label="名单文件" @change="selectFile">
      </label>
    </div>
    <el-alert title="导入名单不代表账号已开通；用户申请仍需管理员确认。" type="info" :closable="false" show-icon />
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <template v-if="preview">
      <dl class="import-summary">
        <div><dt>可新增</dt><dd>{{ preview.summary.add }}</dd></div>
        <div><dt>重复跳过</dt><dd>{{ preview.summary.skip }}</dd></div>
        <div><dt>无效行</dt><dd>{{ preview.summary.error }}</dd></div>
      </dl>
      <el-table :data="preview.rows" max-height="360" empty-text="暂无名单">
        <el-table-column prop="line" label="行" width="60" />
        <el-table-column prop="email" label="邮箱" min-width="220" />
        <el-table-column prop="username" label="用户名" min-width="120" />
        <el-table-column label="有效期" min-width="160"><template #default="{ row }">{{ row.expires_at ? new Date(row.expires_at * 1000).toLocaleString('zh-CN') : '未设置' }}</template></el-table-column>
        <el-table-column label="结果" min-width="240"><template #default="{ row }"><el-tag :type="row.state === 'error' ? 'danger' : row.state === 'add' ? 'success' : 'info'">{{ states[row.state] }}</el-tag> {{ row.message }}</template></el-table-column>
      </el-table>
    </template>
    <template #footer>
      <el-button :disabled="busy" @click="visible = false">取消</el-button>
      <el-button type="primary" :icon="Upload" :loading="busy" :disabled="!preview || preview.summary.error > 0 || preview.summary.add === 0" @click="commit">确认导入</el-button>
    </template>
  </el-dialog>
</template>
<script setup>
import { computed, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { Download, Upload } from "@element-plus/icons-vue";
import { admissionApi as api, admissionError } from "../api/admission";

const props = defineProps({ modelValue: Boolean });
const emit = defineEmits(["update:modelValue", "imported"]);
const visible = computed({ get: () => props.modelValue, set: value => emit("update:modelValue", value) });
const preview = ref(null), error = ref(""), busy = ref(false), input = ref(null);
const states = { add: "新增", skip: "跳过", error: "无效" };
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
    ElMessage.success(`已导入 ${data.summary.add} 人，跳过 ${data.summary.skip} 行。`);
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
</style>
<style>.roster-dialog { max-width: calc(100vw - 28px); border-radius: 6px; }</style>
