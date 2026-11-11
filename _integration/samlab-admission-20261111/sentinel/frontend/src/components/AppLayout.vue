<template>
  <el-container class="app-shell">
    <el-aside width="248px" class="app-sidebar">
      <div class="brand-block">
        <SecretRadarLogo :size="40" :animated="true" variant="dark" />
        <div class="brand-copy"><strong>云上凭据泄露自动化检测</strong></div>
      </div>
      <el-menu :default-active="route.path" router class="app-menu">
        <el-menu-item v-for="item in visibleNavigation.slice(0, 7)" :key="item.path" :index="item.path">
          <el-icon><component :is="icons[item.icon]" /></el-icon><span>{{ item.label }}</span>
        </el-menu-item>
        <div class="menu-divider"><span v-text="'项目管理'" /></div>
        <el-menu-item v-for="item in visibleNavigation.slice(7)" :key="item.path" :index="item.path">
          <el-icon><component :is="icons[item.icon]" /></el-icon><span>{{ item.label }}</span>
        </el-menu-item>
      </el-menu>
      <div class="sidebar-foot">
        <span class="sidebar-foot-label">数据策略</span>
        <strong>默认脱敏 · 本地留痕</strong>
      </div>
    </el-aside>
    <el-container>
      <el-header class="app-header">
        <div class="topbar-title"><span class="topbar-kicker">SECURITY OPERATIONS / EVIDENCE DESK</span><h1>{{ pageTitle }}</h1></div>
        <div class="header-actions">
          <div class="topbar-meta">
            <span class="topbar-time">北京时间 {{ beijingTime }}</span>
          </div>
          <el-dropdown @command="onCommand">
            <el-button text class="user-trigger"><el-avatar :size="32">{{ userStore.user?.username?.slice(0, 1)?.toUpperCase() }}</el-avatar><span class="user-name">{{ userStore.user?.username }}</span><el-icon><ArrowDown /></el-icon></el-button>
            <template #dropdown><el-dropdown-menu><el-dropdown-item command="settings">账号设置</el-dropdown-item><el-dropdown-item command="logout">退出登录</el-dropdown-item></el-dropdown-menu></template>
          </el-dropdown>
        </div>
      </el-header>
      <el-main class="app-main"><slot /></el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useUserStore } from "../store/modules/user";
import { navigation } from "../constants";
import SecretRadarLogo from "./SecretRadarLogo.vue";
import { ArrowDown, Odometer, List, Warning, Search, CircleCheck, Connection, DataAnalysis, Setting, Timer, UserFilled } from "@element-plus/icons-vue";

const route = useRoute();
const router = useRouter();
const userStore = useUserStore();
const icons = { Odometer, List, Warning, Search, CircleCheck, Connection, DataAnalysis, Setting, Timer, UserFilled };
const visibleNavigation = computed(() => navigation.filter(item =>
  (!item.admin || userStore.user?.role === "admin") && (!item.superadmin || userStore.user?.is_superadmin)));
const pageTitle = computed(() => navigation.find((item) => item.path === route.path)?.label || (route.params.eventId ? "事件详情" : "云上凭据泄露自动化检测"));
const beijingTime = ref("");
let clockTimer;

function updateBeijingTime() {
  beijingTime.value = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    hourCycle: "h23",
  }).format(new Date()).replace(/\//g, "-");
}

onMounted(() => {
  updateBeijingTime();
  clockTimer = window.setInterval(updateBeijingTime, 1000);
});

onUnmounted(() => {
  window.clearInterval(clockTimer);
});

function onCommand(command) {
  if (command === "logout") userStore.logout().then(() => window.location.assign("/login"));
  else router.push("/settings#account");
}
</script>
