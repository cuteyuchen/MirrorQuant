import { createRouter, createWebHistory, type RouteRecordRaw } from "vue-router";
import { useAuthStore } from "@/stores/auth";
import { guardRoute } from "./guard";

// 视图按需加载，深度分析不进入看板首包。
export const routes: RouteRecordRaw[] = [
  { path: "/", redirect: "/dashboard" },
  { path: "/dashboard", component: () => import("@/views/DashboardView.vue"), meta: { requiresAuth: true } },
  { path: "/leads", component: () => import("@/views/LeadsView.vue"), meta: { requiresAuth: true } },
  { path: "/leads/:id", component: () => import("@/views/LeadDetailView.vue"), meta: { requiresAuth: true } },
  { path: "/accounts", component: () => import("@/views/AccountsView.vue"), meta: { requiresAuth: true } },
  { path: "/bindings", component: () => import("@/views/BindingsView.vue"), meta: { requiresAuth: true } },
  { path: "/executions", component: () => import("@/views/ExecutionsView.vue"), meta: { requiresAuth: true } },
  // 旧入口保持兼容，待处理订单统一归入执行记录页签。
  { path: "/intents", redirect: { path: "/executions", query: { tab: "intents" } } },
  { path: "/proxies", component: () => import("@/views/ProxiesView.vue"), meta: { requiresAuth: true } },
  { path: "/settings", component: () => import("@/views/SettingsView.vue"), meta: { requiresAuth: true } },
  { path: "/public", component: () => import("@/views/PublicDashboardView.vue") },
  { path: "/login", component: () => import("@/views/LoginView.vue"), meta: { fullscreen: true } },
  { path: "/setup", component: () => import("@/views/SetupWizard.vue"), meta: { fullscreen: true } },
  { path: "/connection-error", component: () => import("@/views/ConnectionErrorView.vue"), meta: { fullscreen: true } },
  { path: "/:pathMatch(.*)*", component: () => import("@/views/NotFoundView.vue"), meta: { notFound: true, fullscreen: true } },
];
export const router = createRouter({ history: createWebHistory(import.meta.env.BASE_URL), routes });
router.beforeEach(to => guardRoute(to, useAuthStore()));
