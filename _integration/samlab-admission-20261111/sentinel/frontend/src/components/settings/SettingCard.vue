<template><el-card shadow="never" class="settings-card"><template #header><div><strong>{{ title }}</strong><p v-if="subtitle" class="muted">{{ subtitle }}</p></div></template><div class="settings-grid"><slot /></div><template v-if="$slots.footer" #footer><slot name="footer" /></template></el-card></template>
<script setup>
defineProps({ title: String, subtitle: String });
</script>
