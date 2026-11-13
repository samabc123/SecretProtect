import { createRouter, createWebHistory } from "vue-router";
import { useUserStore } from "../store/modules/user";

const view = (name) => () => import(`../views/${name}.vue`);

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/login", name: "login", component: view("AuthView"), meta: { public: true, mode: "login" } },
    { path: "/register", redirect: "/login" },
    { path: "/admission", component: view("LabAdmissionView"), meta: { admin: true } },
    { path: "/administrators", component: view("AdminsView"), meta: { admin: true, superadmin: true } },
    { path: "/", name: "overview", component: view("ProtectionView") },
    { path: "/attacks", component: view("ProtectionView") },
    { path: "/decisions", component: view("ProtectionView") },
    { path: "/engine", component: view("ProtectionView") },
    { path: "/security-audit", component: view("ProtectionView") },
    { path: "/monitoring-tasks", component: view("MonitoringTasksView") },
    { path: "/events", component: view("EventsView") },
    { path: "/events/:eventId", component: view("EventDetailView") },
    { path: "/findings", component: view("FindingsView") },
    { path: "/verification", component: view("VerificationView") },
    { path: "/channels", component: view("ChannelsView") },
    { path: "/reports", component: view("ReportsView") },
    { path: "/settings", component: view("SettingsView") },
    { path: "/connector-runs", component: view("ConnectorRunsView") },
    { path: "/:pathMatch(.*)*", redirect: "/" },
  ],
});

router.beforeEach(async (to) => {
  const store = useUserStore();
  if (!to.meta.public && !store.loaded) {
    try { await store.load(); } catch { /* public pages are allowed to continue */ }
  }
  if (!to.meta.public && !store.isLoggedIn) return { name: "login", query: { next: to.fullPath } };
  if (to.meta.admin && store.user?.role !== "admin") return { path: "/" };
  if (to.meta.superadmin && !store.user?.is_superadmin) return { path: "/admission" };
  if (store.user?.needs_password_change && to.path !== "/settings" && to.path !== "/login")
    return { path: "/settings", hash: "#account" };
  if (to.meta.public && store.isLoggedIn) return { path: String(to.query.next || "/") };
  return true;
});

router.onError((error) => {
  const message = String(error?.message || error || "");
  const isChunkFailure = /Failed to fetch dynamically imported module|Importing a module script failed|ChunkLoadError|Loading chunk/i.test(message);
  if (!isChunkFailure) {
    console.error(error);
    return;
  }
  // A new frontend build can invalidate a lazy chunk while an old index page
  // is still open. Reload once so the browser obtains the matching manifest.
  try {
    const key = "secretradar:chunk-reload";
    if (sessionStorage.getItem(key) === "1") {
      sessionStorage.removeItem(key);
      return;
    }
    sessionStorage.setItem(key, "1");
  } catch {
    // Storage can be unavailable in privacy-restricted browsers.
  }
  window.location.reload();
});

router.afterEach(() => {
  try {
    sessionStorage.removeItem("secretradar:chunk-reload");
  } catch {
    // Ignore storage restrictions.
  }
});

export default router;
