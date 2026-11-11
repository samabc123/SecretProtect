<template>
  <el-container class="app-shell">
    <el-aside width="248px" class="app-sidebar" :class="{ 'is-collapsed': sidebarCollapsed }">
      <div class="brand-block">
        <SecretRadarLogo :size="36" :animated="false" variant="dark" />
        <div v-if="!sidebarCollapsed" class="brand-copy"><strong>SecretRadar</strong><span>实验室安全管理</span></div>
      </div>
      <AppNavigation :active="route.path" :items="visibleNavigation" :collapsed="sidebarCollapsed" />
      <div class="sidebar-foot">
        <div v-if="!sidebarCollapsed" class="sidebar-policy"><el-icon><Lock /></el-icon><span>默认脱敏 · 本地留痕</span></div>
        <el-tooltip :content="sidebarCollapsed ? '展开侧栏' : '收起侧栏'" placement="right">
          <el-button class="sidebar-toggle" text :icon="sidebarCollapsed ? DArrowRight : DArrowLeft" :aria-label="sidebarCollapsed ? '展开侧栏' : '收起侧栏'" @click="toggleSidebar"><span v-if="!sidebarCollapsed">收起侧栏</span></el-button>
        </el-tooltip>
      </div>
    </el-aside>
    <el-container class="app-workspace">
      <el-header class="app-header">
        <div class="topbar-heading">
          <el-tooltip content="打开导航" placement="bottom">
            <el-button class="mobile-nav-trigger" :icon="Expand" text circle aria-label="打开导航" :aria-expanded="navigationOpen" @click="navigationOpen = true" />
          </el-tooltip>
          <div class="topbar-title"><h1>{{ pageTitle }}</h1><span class="topbar-context">Samlab 安全工作台</span></div>
        </div>
        <div class="header-actions">
          <div class="topbar-meta">
            <span class="topbar-time">北京时间 {{ beijingTime }}</span>
          </div>
          <el-dropdown @command="onCommand">
            <el-button text class="user-trigger" aria-label="账号菜单"><el-avatar :size="32" shape="square">{{ userStore.user?.username?.slice(0, 1)?.toUpperCase() }}</el-avatar><span class="user-account"><span class="user-name">{{ userStore.user?.username }}</span><span class="user-role">{{ userStore.user?.is_superadmin ? '超级管理员' : '管理员' }}</span></span><el-icon><ArrowDown /></el-icon></el-button>
            <template #dropdown><el-dropdown-menu><el-dropdown-item command="settings">账号设置</el-dropdown-item><el-dropdown-item command="logout">退出登录</el-dropdown-item></el-dropdown-menu></template>
          </el-dropdown>
        </div>
      </el-header>
      <el-main class="app-main"><slot /></el-main>
    </el-container>
    <el-drawer v-model="navigationOpen" title="功能导航" direction="ltr" size="min(300px, calc(100vw - 32px))" class="mobile-navigation">
      <div class="brand-block">
        <SecretRadarLogo :size="36" :animated="false" variant="dark" />
        <div class="brand-copy"><strong>SecretRadar</strong><span>实验室安全管理</span></div>
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
import { ArrowDown, DArrowLeft, DArrowRight, Expand, Lock } from "@element-plus/icons-vue";
import { useCompactLayout } from "../composables/useCompactLayout";

const route = useRoute();
const router = useRouter();
const userStore = useUserStore();
const navigationOpen = ref(false);
const sidebarCollapsed = ref(false);
try { sidebarCollapsed.value = localStorage.getItem("secretradar:sidebar-collapsed") === "1"; } catch { /* Storage is optional. */ }
function toggleSidebar() {
  sidebarCollapsed.value = !sidebarCollapsed.value;
  try { localStorage.setItem("secretradar:sidebar-collapsed", sidebarCollapsed.value ? "1" : "0"); } catch { /* Keep the in-memory preference. */ }
}
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
