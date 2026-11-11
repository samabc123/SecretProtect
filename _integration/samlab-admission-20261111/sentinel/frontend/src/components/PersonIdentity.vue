<template>
  <div class="person-identity">
    <span class="person-avatar" aria-hidden="true">{{ initial }}</span>
    <div class="record-identity"><strong>{{ name || "尚未注册" }}</strong><span v-if="detail">{{ detail }}</span></div>
  </div>
</template>

<script setup>
import { computed } from "vue";
const props = defineProps({ name: String, detail: String });
const initial = computed(() => Array.from(props.name || props.detail || "?")[0].toUpperCase());
</script>

<style scoped>
.person-identity { display: flex; align-items: flex-start; gap: 12px; min-width: 0; }
.person-avatar { display: grid; place-items: center; flex: 0 0 36px; width: 36px; height: 36px; border-radius: 8px; color: #315eb9; background: #edf3ff; font-size: 14px; font-weight: 600; }
.record-identity { flex: 1; }
</style>
