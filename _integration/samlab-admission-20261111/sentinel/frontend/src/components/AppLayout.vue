<template>
  <el-container class="app-shell">
    <el-aside width="248px" class="app-sidebar">
      <div class="brand-block">
        <SecretRadarLogo :size="40" :animated="true" variant="dark" />
        <div class="brand-copy"><strong>云上凭据泄露自动化检测</strong></div>
      </div>
      <AppNavigation :active="route.path" :items="visibleNavigation" />
      <div class="sidebar-foot">
        <span class="sidebar-foot-label">数据策略</span>
        <strong>默认脱敏 · 本地留痕</strong>
      </div>
    </el-aside>
    <el-container class="app-workspace">
      <el-header class="app-header">
        <div class="topbar-heading">
          <el-tooltip content="打开导航" placement="bottom">
            <el-button class="mobile-nav-trigger" :icon="Expand" text circle aria-label="打开导航" :aria-expanded="navigationOpen" @click="navigationOpen = true" />
          </el-tooltip>
          <div class="topbar-title"><h1>{{ pageTitle }}</h1></div>
        </div>
        <div class="header-actions">
          <div class="topbar-meta">
            <span class="topbar-time">北京时间 {{ beijingTime }}</span>
          </div>
          <el-dropdown @command="onCommand">
            <el-button text class="user-trigger" aria-label="账号菜单"><el-avatar :size="32">{{ userStore.user?.username?.slice(0, 1)?.toUpperCase() }}</el-avatar><span class="user-name">{{ userStore.user?.username }}</span><el-icon><ArrowDown /></el-icon></el-button>
            <template #dropdown><el-dropdown-menu><el-dropdown-item command="settings">账号设置</el-dropdown-item><el-dropdown-item command="logout">退出登录</el-dropdown-item></el-dropdown-menu></template>
          </el-dropdown>
        </div>
      </el-header>
      <el-main class="app-main"><slot /></el-main>
    </el-container>
    <el-drawer v-model="navigationOpen" title="功能导航" direction="ltr" size="min(300px, calc(100vw - 32px))" class="mobile-navigation">
      <div class="brand-block">
        <SecretRadarLogo :size="40" :animated="false" variant="dark" />
        <div class="brand-copy"><strong>云上凭据泄露自动化检测</strong></div>
      </div>
      <AppNavigation :active="route.path" :items="visibleNavigation" @navigate="navigationOpen = false" />
    </el-drawer>
  </el-container>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useUserStore } from "../store/modules/user";
import { navigation } from "../constants";
import SecretRadarLogo from "./SecretRadarLogo.vue";
import AppNavigation from "./AppNavigation.vue";
import { ArrowDown, Expand } from "@element-plus/icons-vue";
import { useCompactLayout } from "../composables/useCompactLayout";

const route = useRoute();
const router = useRouter();
const userStore = useUserStore();
const navigationOpen = ref(false);
const compactNavigation = useCompactLayout("(max-width: 760px)");
watch(compactNavigation, value => { if (!value) navigationOpen.value = false; });
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
