<template>
  <LabRegistrationView v-if="enabled === true" />
  <RegisterView v-else-if="enabled === false" />
  <AuthLayout v-else>
    <p role="status" class="text-sm text-gray-600">{{ error || '正在读取注册策略…' }}</p>
    <button v-if="error" class="btn btn-primary mt-4" @click="load">重试</button>
  </AuthLayout>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { apiClient } from '@/api/client'
import { AuthLayout } from '@/components/layout'
import RegisterView from './RegisterView.vue'
import LabRegistrationView from './LabRegistrationView.vue'
const enabled = ref<boolean | null>(null)
const error = ref('')
async function load() {
  error.value = ''
  try { enabled.value = (await apiClient.get('/auth/lab-admission/config')).data.enabled === true }
  catch { error.value = '暂时无法读取注册策略，请稍后重试。' }
}
onMounted(load)
</script>
