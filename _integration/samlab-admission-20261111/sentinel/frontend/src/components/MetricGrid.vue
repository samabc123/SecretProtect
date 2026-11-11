<template>
  <div class="metric-grid">
    <el-card v-for="(item, index) in items" :key="item.label" shadow="never" class="metric-card" :class="`metric-card--${index % 4}`">
      <div class="metric-topline"><span class="metric-label">{{ item.label }}</span><span class="metric-index">{{ String(index + 1).padStart(2, "0") }}</span></div>
      <strong>{{ item.value ?? "-" }}</strong>
      <small v-if="showHints">{{ item.hint }}</small>
    </el-card>
  </div>
</template>
<script setup>
defineProps({
  items: { type: Array, default: () => [] },
  showHints: { type: Boolean, default: true },
});
</script>
