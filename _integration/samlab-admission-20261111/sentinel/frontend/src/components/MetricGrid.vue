<template>
  <dl class="metric-grid">
    <div v-for="(item, index) in items" :key="item.label" class="metric-card" :class="`metric-card--${index % 4}`">
      <dt class="metric-label">{{ item.label }}</dt>
      <dd>{{ item.value ?? "-" }}</dd>
      <small v-if="showHints">{{ item.hint }}</small>
    </div>
  </dl>
</template>
<script setup>
defineProps({
  items: { type: Array, default: () => [] },
  showHints: { type: Boolean, default: true },
});
</script>
