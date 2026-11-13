<template><el-empty v-if="!loading && !error && empty" :description="emptyText" /><el-result v-else-if="error" icon="error" title="加载失败" :sub-title="error"><template #extra><el-button type="primary" @click="$emit('retry')">重试</el-button></template></el-result><div v-else-if="loading" class="loading-state"><el-skeleton :rows="5" animated /></div></template>
<script setup>
defineProps({ loading: Boolean, error: String, empty: Boolean, emptyText: { type: String, default: "暂无数据" } });
defineEmits(["retry"]);
</script>
