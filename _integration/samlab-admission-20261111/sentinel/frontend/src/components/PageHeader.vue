<template>
  <div class="page-header">
    <div class="page-heading"><h2>{{ title }}</h2><p v-if="description">{{ description }}</p></div>
    <div class="page-actions"><slot /></div>
  </div>
</template>
<script setup>
defineProps({ title: String, description: String });
</script>
