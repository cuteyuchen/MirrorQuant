import { ref } from "vue";
import { createRouter, createWebHistory, type RouteRecordRaw } from "vue-router";
import { useAuthStore } from "@/stores/auth";
import { guardRoute } from "./guard";

const loadDashboard = () => import("@/views/DashboardView.vue");
const loadExecutions = () => import("@/views/ExecutionsView.vue");
const loadLeads = () => import("@/views/LeadsView.vue");
const loadAccounts = () => import("@/views/AccountsView.vue");
const loadBindings = () => import("@/views/BindingsView.vue");

// 视图按需加载，深度分析不进入看板首包。
export const routes: RouteRecordRaw[] = [
  { path: "/", redirect: "/dashboard" },
  { path: "/dashboard", component: loadDashboard, meta: { requiresAuth: true } },
  { path: "/leads", component: loadLeads, meta: { requiresAuth: true } },
  { path: "/leads/:id", component: () => import("@/views/LeadDetailView.vue"), meta: { requiresAuth: true } },
  { path: "/accounts", component: loadAccounts, meta: { requiresAuth: true } },
  { path: "/accounts/:id", component: () => import("@/views/AccountDetailView.vue"), meta: { requiresAuth: true } },
  { path: "/bindings", component: loadBindings, meta: { requiresAuth: true } },
  { path: "/executions", component: loadExecutions, meta: { requiresAuth: true } },
  // 旧入口保持兼容，待处理订单统一归入执行记录页签。
  { path: "/intents", redirect: { path: "/executions", query: { tab: "intents" } } },
  { path: "/proxies", component: () => import("@/views/ProxiesView.vue"), meta: { requiresAuth: true } },
  { path: "/settings", component: () => import("@/views/SettingsView.vue"), meta: { requiresAuth: true } },
  { path: "/users", component: () => import("@/views/UsersView.vue"), meta: { requiresAuth: true, requiresAdmin: true } },
  { path: "/public", component: () => import("@/views/PublicDashboardView.vue") },
  { path: "/login", component: () => import("@/views/LoginView.vue"), meta: { fullscreen: true } },
  { path: "/register", component: () => import("@/views/RegisterView.vue"), meta: { fullscreen: true } },
  { path: "/setup", component: () => import("@/views/SetupWizard.vue"), meta: { fullscreen: true } },
  { path: "/connection-error", component: () => import("@/views/ConnectionErrorView.vue"), meta: { fullscreen: true } },
  { path: "/:pathMatch(.*)*", component: () => import("@/views/NotFoundView.vue"), meta: { notFound: true, fullscreen: true } },
];
export const router = createRouter({ history: createWebHistory(import.meta.env.BASE_URL), routes });
export const routeTransitioning = ref(false);
let prefetched = false;
export function prefetchCoreRoutes() {
  if (prefetched || typeof window === "undefined") return;
  prefetched = true;
  const run = () => {
    void Promise.allSettled([loadDashboard(), loadExecutions(), loadLeads(), loadAccounts(), loadBindings()]);
  };
  const ric = (window as Window & { requestIdleCallback?: (cb: () => void, opts?: { timeout: number }) => void }).requestIdleCallback;
  if (typeof ric === "function") ric(run, { timeout: 1500 });
  else setTimeout(run, 300);
}
router.beforeEach(to => {
  routeTransitioning.value = true;
  return guardRoute(to, useAuthStore());
});
router.afterEach(() => { routeTransitioning.value = false; });
router.onError(() => { routeTransitioning.value = false; });
