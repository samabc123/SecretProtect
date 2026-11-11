<template>
  <el-menu :default-active="active" :collapse="collapsed" :collapse-transition="false" router class="app-menu" aria-label="功能导航" @select="$emit('navigate')">
    <template v-for="item in items" :key="item.path">
      <div v-if="!collapsed && item.group" class="menu-divider">{{ item.group }}</div>
      <el-menu-item :index="item.path" :aria-label="item.label">
        <el-icon><component :is="icons[item.icon]" /></el-icon>
        <template #title><span>{{ item.label }}</span></template>
      </el-menu-item>
    </template>
  </el-menu>
</template>

<script setup>
import { Odometer, List, Warning, Search, CircleCheck, Connection, DataAnalysis, Setting, Timer, UserFilled } from "@element-plus/icons-vue";

defineProps({ active: String, items: { type: Array, required: true }, collapsed: Boolean });
defineEmits(["navigate"]);
const icons = { Odometer, List, Warning, Search, CircleCheck, Connection, DataAnalysis, Setting, Timer, UserFilled };
</script>
