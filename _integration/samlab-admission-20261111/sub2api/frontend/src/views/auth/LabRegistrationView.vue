<template>
  <AuthLayout>
    <div class="lab-register">
      <h2 class="text-2xl font-bold text-gray-900 dark:text-white">Samlab 实验室入组申请</h2>
      <fieldset class="mode-switch">
        <label><input v-model="mode" type="radio" value="apply"> 提交申请</label>
        <label><input v-model="mode" type="radio" value="status"> 查询进度</label>
      </fieldset>
      <p v-if="error" role="alert" class="text-sm text-red-600">{{ error }}</p>
      <section v-if="result" aria-live="polite" class="application-result">
        <h3 class="font-semibold">{{ statusLabels[result.decision] || '等待同步' }}</h3>
        <p v-if="result.ready">账号已开通</p>
        <p v-else-if="result.decision === 'pending'">申请已保存，等待管理员审批。</p>
        <p class="request-reference">{{ result.request_id }}</p>
        <RouterLink v-if="result.ready" to="/login" class="text-primary-600 underline">前往登录</RouterLink>
      </section>
      <form class="space-y-4" @submit.prevent="submit">
        <div v-if="mode === 'apply'">
          <label for="lab-username" class="input-label">用户名</label>
          <input id="lab-username" v-model.trim="form.username" class="input" autocomplete="username" minlength="3" maxlength="32" pattern="[A-Za-z0-9_][A-Za-z0-9_.\-]{2,31}" required>
          <p v-if="fields.username" class="field-error">{{ fields.username }}</p>
        </div>
        <div v-if="mode === 'status'">
          <label for="lab-request" class="input-label">申请编号</label>
          <input id="lab-request" v-model.trim="form.request_id" class="input" maxlength="100" required>
        </div>
        <div>
          <label for="lab-email" class="input-label">邮箱</label>
          <input id="lab-email" v-model.trim="form.email" class="input" type="email" autocomplete="email" maxlength="180" required>
          <p v-if="fields.email" class="field-error">{{ fields.email }}</p>
        </div>
        <div>
          <label for="lab-password" class="input-label">{{ mode === 'apply' ? '密码（6至128个字符）' : '申请时设置的密码' }}</label>
          <input id="lab-password" v-model="form.password" class="input" type="password" :autocomplete="mode === 'apply' ? 'new-password' : 'current-password'" minlength="6" maxlength="128" required>
          <p v-if="fields.password" class="field-error">{{ fields.password }}</p>
        </div>
        <template v-if="mode === 'apply'">
          <div v-if="settings?.invitation_code_enabled">
            <label for="lab-invitation" class="input-label">邀请码</label>
            <input id="lab-invitation" v-model="form.invitation_code" class="input" maxlength="128" required>
          </div>
          <CaptchaChallenge v-if="settings" ref="captcha"
            :turnstile-enabled="settings.turnstile_enabled" :turnstile-site-key="settings.turnstile_site_key || ''"
            :tencent-enabled="settings.tencent_captcha_enabled === true" :tencent-app-id="settings.tencent_captcha_app_id || ''"
            :aliyun-enabled="settings.aliyun_captcha_enabled === true" :aliyun-scene-id="settings.aliyun_captcha_scene_id || ''"
            :aliyun-prefix="settings.aliyun_captcha_prefix || ''"
            @verify="(token, randstr) => { proof = token; randomString = randstr }"
            @expire="proof = ''" @error="error = '人机验证暂不可用。'" />
          <div v-if="settings?.email_verify_enabled">
            <label for="lab-email-code" class="input-label">邮箱验证码</label>
            <div class="verification-row">
              <input id="lab-email-code" v-model="form.verify_code" class="input" inputmode="numeric" autocomplete="one-time-code" maxlength="16" required>
              <button type="button" class="btn btn-secondary" :disabled="busy || countdown > 0" @click="sendCode">{{ countdown > 0 ? `${countdown}s` : '发送验证码' }}</button>
            </div>
          </div>
          <LoginAgreementPrompt v-if="settings?.login_agreement_enabled"
            mode="checkbox" :accepted="accepted" :documents="settings.login_agreement_documents || []"
            :visible="false" @accept="accepted = true" @reject="accepted = false" />
        </template>
        <button class="btn btn-primary w-full" type="submit" :disabled="busy || !settings || (mode === 'apply' && (!settings.registration_enabled || !!result))">
          {{ busy ? '处理中…' : mode === 'apply' ? '提交入组申请' : '查询进度' }}
        </button>
      </form>
      <RouterLink to="/login" class="block text-center text-sm text-primary-600">返回登录</RouterLink>
    </div>
  </AuthLayout>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, reactive, ref } from 'vue'
import { AuthLayout } from '@/components/layout'
import { apiClient } from '@/api/client'
import { getPublicSettings } from '@/api/auth'
import type { PublicSettings } from '@/types'
import CaptchaChallenge from '@/components/CaptchaChallenge.vue'
import LoginAgreementPrompt from '@/components/auth/LoginAgreementPrompt.vue'

const mode = ref('apply'), error = ref(''), busy = ref(false), accepted = ref(false)
const settings = ref<PublicSettings | null>(null)
const form = reactive({ request_id: crypto.randomUUID(), username: '', email: '', password: '', invitation_code: '', verify_code: '' })
const fields = ref<Record<string, string>>({})
const result = ref<{ request_id: string; decision: string; ready: boolean } | null>(null)
const statusLabels: Record<string, string> = { pending: '待审批', approved: '已批准', rejected: '未通过', revoked: '准入已撤销' }
const captcha = ref<InstanceType<typeof CaptchaChallenge> | null>(null)
const proof = ref(''), randomString = ref(''), countdown = ref(0)
let timer: ReturnType<typeof setInterval> | undefined
function captchaBody() {
  return settings.value?.tencent_captcha_enabled
    ? { tencent_captcha_ticket: proof.value, tencent_captcha_randstr: randomString.value }
    : { turnstile_token: proof.value }
}
async function acquireProof() {
  if (settings.value?.tencent_captcha_enabled || settings.value?.aliyun_captcha_enabled) {
    const value = await captcha.value?.verifyAction()
    if (!value) throw new Error('请完成人机验证。')
    proof.value = value.token; randomString.value = value.randstr
  }
}
function showError(value: unknown) {
  const e = value as { message?: string; field_errors?: Record<string, string>; response?: { data?: { message?: string; field_errors?: Record<string, string> } } }
  error.value = e.response?.data?.message || e.message || '请求未完成，请稍后重试。'
  fields.value = e.field_errors || e.response?.data?.field_errors || {}
}
async function sendCode() {
  if (busy.value || countdown.value) return
  busy.value = true; error.value = ''
  try {
    await acquireProof()
    const { data } = await apiClient.post('/auth/send-verify-code', { email: form.email, ...captchaBody() })
    countdown.value = Math.max(1, Number(data.countdown) || 60)
  } catch (e) { showError(e) }
  finally { busy.value = false; proof.value = ''; captcha.value?.reset() }
}
async function submit() {
  if (busy.value || !settings.value) return
  if (mode.value === 'apply' && settings.value.login_agreement_enabled && !accepted.value) {
    error.value = '请先阅读并同意平台条款。'; return
  }
  busy.value = true; error.value = ''; fields.value = {}
  try {
    if (mode.value === 'apply') {
      if (!settings.value.email_verify_enabled) await acquireProof()
      const { data } = await apiClient.post('/auth/register', { ...form, ...captchaBody() })
      result.value = data.application
      localStorage.setItem('samlab_application_reference', JSON.stringify({ request_id: form.request_id, email: form.email }))
      mode.value = 'status'
    } else {
      const { data } = await apiClient.post('/auth/lab-application/status', { request_id: form.request_id, email: form.email, password: form.password })
      result.value = data
    }
  } catch (e) { showError(e) }
  finally { busy.value = false; proof.value = ''; captcha.value?.reset() }
}
onMounted(async () => {
  try {
    settings.value = await getPublicSettings()
    const previous = localStorage.getItem('samlab_application_reference')
    if (previous) {
      const parsed = JSON.parse(previous)
      if (typeof parsed.request_id === 'string' && typeof parsed.email === 'string') {
        form.request_id = parsed.request_id; form.email = parsed.email; mode.value = 'status'
      }
    }
  } catch { error.value = '暂时无法读取注册策略。' }
  timer = setInterval(() => { if (countdown.value > 0) countdown.value-- }, 1000)
})
onUnmounted(() => { clearInterval(timer); form.password = '' })
</script>

<style scoped>
.lab-register { display: grid; gap: 20px; min-width: 0; letter-spacing: 0; }
.mode-switch { display: flex; flex-wrap: wrap; gap: 20px; border-bottom: 1px solid #d1d5db; padding-bottom: 14px; font-size: 14px; }
.application-result { border-left: 3px solid #15803d; padding: 4px 0 4px 14px; font-size: 14px; line-height: 1.8; }
.request-reference { overflow-wrap: anywhere; color: #6b7280; font-size: 12px; }
.verification-row { display: grid; grid-template-columns: minmax(0, 1fr) 130px; gap: 10px; }
.field-error { color: #dc2626; font-size: 13px; margin-top: 5px; }
input, button { max-width: 100%; }
</style>
