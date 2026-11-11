<template>
  <AppLayout>
    <main class="lab-invitations">
      <header class="flex flex-wrap items-center justify-between gap-4">
        <h2 class="text-xl font-semibold">实验室代注册</h2>
        <button class="btn btn-secondary" :disabled="busy" aria-label="刷新申请" title="刷新申请" @click="load"><Icon name="refresh" size="md" /></button>
      </header>
      <p v-if="error" role="alert" class="text-sm text-red-600">{{ error }}</p>
      <form class="invitation-form" @submit.prevent="submit">
        <label>用户名<input v-model.trim="form.username" class="input" required minlength="3" maxlength="32" pattern="[A-Za-z0-9_][A-Za-z0-9_.\-]{2,31}"></label>
        <label>邮箱<input v-model.trim="form.email" class="input" required type="email" maxlength="180"></label>
        <button class="btn btn-primary" :disabled="busy">创建邀请账号</button>
      </form>
      <section v-if="invitation" aria-live="polite" class="invite-link">
        <label for="issued-invitation">一次性激活链接（15分钟有效）</label>
        <input id="issued-invitation" class="input" :value="invitation" readonly>
        <button class="btn btn-secondary" @click="invitation = ''">关闭</button>
      </section>
      <div class="table-scroll">
        <table class="w-full text-left text-sm">
          <thead><tr><th>用户名</th><th>邮箱</th><th>准入状态</th><th>激活</th></tr></thead>
          <tbody><tr v-for="row in rows" :key="row.request_id">
            <td>{{ row.username }}</td><td>{{ row.email }}</td><td>{{ labels[row.decision] || row.decision }}</td>
            <td><button v-if="row.approved && row.activation_required" class="btn btn-secondary" :disabled="busy" @click="issue(row.request_id)">生成邀请</button><span v-else>{{ row.activation_required ? '等待同步' : '无需邀请' }}</span></td>
          </tr></tbody>
        </table>
      </div>
    </main>
  </AppLayout>
</template>
<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import AppLayout from '@/components/layout/AppLayout.vue'
import Icon from '@/components/icons/Icon.vue'
import { apiClient } from '@/api/client'
const busy = ref(false), error = ref(''), invitation = ref('')
const form = reactive({ request_id: crypto.randomUUID(), username: '', email: '' })
interface Row { request_id: string; username: string; email: string; decision: string; approved: boolean; activation_required: boolean }
const rows = ref<Row[]>([])
const labels: Record<string,string> = { pending:'待处理',approved:'已授权',rejected:'未通过',revoked:'已撤销' }
async function load() {
  try { rows.value = (await apiClient.get('/admin/lab-applications')).data.items }
  catch (e) { error.value = (e as {message?:string}).message || '记录暂不可用。' }
}
async function submit() {
  if (busy.value) return
  busy.value = true; error.value = ''
  try {
    await apiClient.post('/admin/lab-applications', form)
    form.request_id = crypto.randomUUID(); form.username = ''; form.email = ''; await load()
  } catch (e) { error.value = (e as {message?:string}).message || '提交未完成。' }
  finally { busy.value = false }
}
async function issue(requestID:string) {
  if (busy.value) return
  busy.value = true; error.value = ''
  try { invitation.value = new URL((await apiClient.post(`/admin/lab-applications/${requestID}/invitation`)).data.activation_path, location.origin).href }
  catch (e) { error.value = (e as {message?:string}).message || '邀请未生成。' }
  finally { busy.value = false }
}
onMounted(load)
</script>
<style scoped>
.lab-invitations { display:grid; gap:24px; min-width:0; letter-spacing:0; }
.invitation-form { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)) auto; gap:16px; align-items:end; padding:20px 0; border-bottom:1px solid #d1d5db; }
.invitation-form label { display:grid; gap:8px; font-size:14px; }
.table-scroll { overflow:auto; }
table { min-width:680px; }
th,td { padding:12px; border-bottom:1px solid #d1d5db; overflow-wrap:anywhere; }
.invite-link { display:grid; gap:10px; max-width:800px; }
@media(max-width:800px) { .invitation-form { grid-template-columns:minmax(0,1fr); } }
</style>
