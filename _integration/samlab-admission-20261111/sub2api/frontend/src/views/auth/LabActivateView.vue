<template>
  <AuthLayout>
    <form class="space-y-5" @submit.prevent="activate">
      <h2 class="text-2xl font-bold">激活实验室账号</h2>
      <p v-if="error" role="alert" class="text-sm text-red-600">{{ error }}</p>
      <p v-if="done" role="status">账号已激活。<RouterLink to="/login" class="text-primary-600 underline">前往登录</RouterLink></p>
      <template v-else>
        <label for="activation-password" class="input-label">设置密码（6至128个字符）</label>
        <input id="activation-password" v-model="password" class="input" type="password" autocomplete="new-password" minlength="6" maxlength="128" required>
        <label for="activation-confirm" class="input-label">确认密码</label>
        <input id="activation-confirm" v-model="confirmation" class="input" type="password" autocomplete="new-password" minlength="6" maxlength="128" required>
        <button class="btn btn-primary w-full" :disabled="busy || !token">激活账号</button>
      </template>
    </form>
  </AuthLayout>
</template>
<script setup lang="ts">
import { ref } from 'vue'
import { AuthLayout } from '@/components/layout'
import { apiClient } from '@/api/client'
const token = ref(new URLSearchParams(location.hash.slice(1)).get('token') || '')
history.replaceState(null, '', location.pathname + location.search)
const password = ref(''), confirmation = ref(''), error = ref(''), busy = ref(false), done = ref(false)
async function activate() {
  if (password.value !== confirmation.value) { error.value = '两次密码不一致。'; return }
  busy.value = true; error.value = ''
  try {
    await apiClient.post('/auth/lab-activate', { token: token.value, password: password.value })
    token.value = ''; password.value = ''; confirmation.value = ''; done.value = true
  } catch (e) { error.value = (e as { message?: string }).message || '激活未完成。' }
  finally { busy.value = false }
}
</script>
