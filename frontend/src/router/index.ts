import { createRouter, createWebHistory } from "vue-router";
import { useAuthStore } from "@/stores/auth";

import DashboardView from "@/views/DashboardView.vue";
import LeadsView from "@/views/LeadsView.vue";
import AccountsView from "@/views/AccountsView.vue";
import BindingsView from "@/views/BindingsView.vue";
import ProxiesView from "@/views/ProxiesView.vue";
import SettingsView from "@/views/SettingsView.vue";
import LoginView from "@/views/LoginView.vue";
import SetupWizard from "@/views/SetupWizard.vue";
import PublicDashboardView from "@/views/PublicDashboardView.vue";

const routes = [
  {
    path: "/",
    redirect: "/dashboard",
  },
  {
    path: "/dashboard",
    name: "Dashboard",
    component: DashboardView,
    meta: { requiresAuth: true },
  },
  {
    path: "/leads",
    name: "Leads",
    component: LeadsView,
    meta: { requiresAuth: true },
  },
  {
    path: "/accounts",
    name: "Accounts",
    component: AccountsView,
    meta: { requiresAuth: true },
  },
  {
    path: "/bindings",
    name: "Bindings",
    component: BindingsView,
    meta: { requiresAuth: true },
  },
  {
    path: "/proxies",
    name: "Proxies",
    component: ProxiesView,
    meta: { requiresAuth: true },
  },
  {
    path: "/settings",
    name: "Settings",
    component: SettingsView,
    meta: { requiresAuth: true },
  },
  {
    path: "/login",
    name: "Login",
    component: LoginView,
  },
  {
    path: "/setup",
    name: "Setup",
    component: SetupWizard,
  },
  {
    path: "/public",
    name: "Public",
    component: PublicDashboardView,
  },
];

export const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
});

router.beforeEach(async (to, _from, next) => {
  const authStore = useAuthStore();
  if (authStore.initialized === null) {
    await authStore.checkStatus();
  }

  if (!authStore.initialized && to.path !== "/setup") {
    return next("/setup");
  }

  if (authStore.initialized && to.path === "/setup") {
    return next(authStore.authenticated ? "/dashboard" : "/login");
  }

  if (to.meta.requiresAuth && !authStore.authenticated) {
    return next("/login");
  }

  if ((to.path === "/login") && authStore.authenticated) {
    return next("/dashboard");
  }

  next();
});
