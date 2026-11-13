<template>
  <div class="person-identity" :class="{ 'email-identity': emailFirst }">
    <span class="person-avatar" aria-hidden="true">{{ initial }}</span>
    <div class="record-identity"><strong>{{ emailFirst ? detail : name || "尚未注册" }}</strong><span v-if="detail && !emailFirst">{{ detail }}</span></div>
  </div>
</template>

<script setup>
import { computed } from "vue";
const props = defineProps({ name: String, detail: String, emailFirst: Boolean });
const initial = computed(() => Array.from((props.emailFirst ? props.detail : props.name) || props.detail || "?")[0].toUpperCase());
</script>

<style scoped>
.person-identity { display: flex; align-items: flex-start; gap: 12px; min-width: 0; }
.person-avatar { display: grid; place-items: center; flex: 0 0 36px; width: 36px; height: 36px; border-radius: 8px; color: #315eb9; background: #edf3ff; font-size: 14px; font-weight: 600; }
.record-identity { flex: 1; }
.email-identity { align-items: center; gap: 10px; }
.email-identity .person-avatar { flex-basis: 32px; width: 32px; height: 32px; border-radius: 50%; color: #087f78; background: #d9f7ee; font-weight: 500; }
.email-identity .record-identity strong { font-size: 13px; font-weight: 500; line-height: 1.6; }
</style>
