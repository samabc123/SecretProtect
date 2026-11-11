<template>
  <el-menu :default-active="active" router class="app-menu" aria-label="功能导航" @select="$emit('navigate')">
    <template v-for="(item, index) in items" :key="item.path">
      <div v-if="index === 7" class="menu-divider">项目管理</div>
      <el-menu-item :index="item.path">
        <el-icon><component :is="icons[item.icon]" /></el-icon>
        <span>{{ item.label }}</span>
      </el-menu-item>
    </template>
  </el-menu>
</template>

<script setup>
import { Odometer, List, Warning, Search, CircleCheck, Connection, DataAnalysis, Setting, Timer, UserFilled } from "@element-plus/icons-vue";

defineProps({ active: String, items: { type: Array, required: true } });
defineEmits(["navigate"]);
const icons = { Odometer, List, Warning, Search, CircleCheck, Connection, DataAnalysis, Setting, Timer, UserFilled };
</script>
