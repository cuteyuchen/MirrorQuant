<script setup lang="ts">
import { computed, onMounted, onUnmounted, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  Activity,
  Cpu,
  GitFork,
  Globe,
  LayoutDashboard,
  LogOut,
  Network,
  Settings,
  Users,
  Wallet,
} from "lucide-vue-next";
import { useAuthStore } from "@/stores/auth";
import { useSocketStore } from "@/stores/socket";
import { useI18n } from "@/i18n";
import LangToggle from "@/components/LangToggle.vue";

const route = useRoute();
const router = useRouter();
const authStore = useAuthStore();
const socketStore = useSocketStore();
const { t } = useI18n();

const cachedViews = [
  "DashboardView",
  "PublicDashboardView",
  "LeadsView",
  "AccountsView",
  "BindingsView",
  "ProxiesView",
  "SettingsView",
];

const isPublicOrAuthLayout = computed(() => {
  if (route.path === "/public") return !authStore.authenticated;
  return ["/login", "/setup"].includes(route.path);
});

// Desktop Navigation Items
const navItems = computed(() => [
  { name: t("nav.dashboard"), path: "/dashboard", icon: LayoutDashboard },
  { name: t("nav.publicFeed"), path: "/public", icon: Globe },
  { name: t("nav.leads"), path: "/leads", icon: Users },
  { name: t("nav.accounts"), path: "/accounts", icon: Wallet },
  { name: t("nav.bindings"), path: "/bindings", icon: GitFork },
  { name: t("nav.proxies"), path: "/proxies", icon: Network },
  { name: t("nav.settings"), path: "/settings", icon: Settings },
]);

// Six compact tabs keep the radar accessible on mobile.
const mobileNavItems = computed(() => [
  { name: t("nav.mobile.dashboard"), path: "/dashboard", icon: LayoutDashboard },
  { name: t("nav.mobile.publicFeed"), path: "/public", icon: Globe },
  { name: t("nav.mobile.leads"), path: "/leads", icon: Users },
  { name: t("nav.mobile.accounts"), path: "/accounts", icon: Wallet },
  { name: t("nav.mobile.bindings"), path: "/bindings", icon: GitFork },
  { name: t("nav.mobile.settings"), path: "/settings", icon: Settings },
]);

watch(
  () => authStore.authenticated,
  (isAuth) => {
    if (isAuth) {
      socketStore.connect();
    } else {
      socketStore.disconnect();
    }
  },
  { immediate: true }
);

onMounted(() => {
  if (authStore.authenticated) {
    socketStore.connect();
  }
});

onUnmounted(() => {
  socketStore.disconnect();
});

async function handleLogout() {
  await authStore.logout();
  router.push("/login");
}
</script>

<template>
  <div v-if="isPublicOrAuthLayout" class="min-h-screen bg-background text-slate-100">
    <router-view />
  </div>

  <div v-else class="min-h-screen bg-background text-slate-100 flex flex-col md:flex-row">
    <!-- Mobile Topbar: Minimal header, clean & simple, NO hamburger menu toggle -->
    <header
      class="md:hidden flex items-center justify-between px-4 py-3 bg-[#0d121c]/95 backdrop-blur border-b border-[#1e293b] sticky top-0 z-40"
    >
      <div class="flex items-center gap-2.5">
        <div class="w-8 h-8 rounded-lg bg-accent/10 border border-accent/30 flex items-center justify-center">
          <Cpu class="w-4 h-4 text-accent" />
        </div>
        <div class="flex items-center gap-2">
          <span class="font-bold tracking-tight text-slate-100 text-sm">MirrorQuant</span>
          <span
            class="inline-flex items-center gap-1 text-[10px] font-mono px-2 py-0.5 rounded-full border"
            :class="
              socketStore.connected
                ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30'
                : 'bg-rose-500/10 text-rose-400 border-rose-500/30'
            "
          >
            <span
              class="w-1.5 h-1.5 rounded-full"
              :class="socketStore.connected ? 'bg-emerald-400 animate-pulse' : 'bg-rose-400'"
            ></span>
            {{ socketStore.connected ? t("nav.live") : t("nav.off") }}
          </span>
        </div>
      </div>
      <div class="flex items-center gap-2">
        <LangToggle />
        <button
          @click="handleLogout"
          class="p-2 rounded-lg text-slate-400 hover:text-rose-400 hover:bg-rose-500/10 transition-colors"
          :title="t('nav.signOut')"
        >
          <LogOut class="w-4 h-4" />
        </button>
      </div>
    </header>

    <!-- Desktop Sidebar (Hidden on mobile) -->
    <aside
      class="hidden md:flex sticky top-0 left-0 z-30 h-screen w-64 bg-surface border-r border-slate-800/80 flex-col justify-between shrink-0"
    >
      <div>
        <!-- Brand Header -->
        <div class="flex items-center justify-between px-4 py-5 border-b border-slate-800/80 gap-2">
          <div class="flex items-center gap-2.5 min-w-0">
            <div
              class="w-9 h-9 rounded-xl bg-gradient-to-br from-accent/20 to-brand/20 border border-accent/30 flex items-center justify-center shadow-lg shadow-accent/10 shrink-0"
            >
              <Cpu class="w-5 h-5 text-accent" />
            </div>
            <div class="min-w-0">
              <h1 class="font-bold text-base tracking-tight text-white">MirrorQuant</h1>
              <p class="text-[10px] text-slate-400 font-mono truncate">{{ t("app.brandSub") }}</p>
            </div>
          </div>
          <LangToggle />
        </div>

        <!-- Navigation -->
        <nav class="p-3 space-y-1 mt-2">
          <router-link
            v-for="item in navItems"
            :key="item.path"
            :to="item.path"
            class="flex items-center gap-3 px-3.5 py-2.5 rounded-xl text-sm font-medium transition-all duration-150"
            :class="
              route.path === item.path
                ? 'bg-accent/10 text-accent border border-accent/25 shadow-sm'
                : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/50 border border-transparent'
            "
          >
            <component :is="item.icon" class="w-4 h-4" />
            <span>{{ item.name }}</span>
          </router-link>

        </nav>
      </div>

      <!-- Footer User / Stream Info -->
      <div class="p-4 border-t border-slate-800/80 space-y-3">
        <div class="flex items-center justify-between px-2 py-1.5 rounded-lg bg-background/60 border border-slate-800">
          <div class="flex items-center gap-2 text-xs text-slate-400">
            <Activity class="w-3.5 h-3.5 text-accent" />
            <span>{{ t("nav.streamWs") }}</span>
          </div>
          <span
            class="inline-flex items-center gap-1.5 text-[11px] font-mono"
            :class="socketStore.connected ? 'text-emerald-400' : 'text-rose-400'"
          >
            <span
              class="w-1.5 h-1.5 rounded-full"
              :class="socketStore.connected ? 'bg-emerald-400 animate-pulse' : 'bg-rose-400'"
            ></span>
            {{ socketStore.connected ? t("nav.connected") : t("nav.reconnecting") }}
          </span>
        </div>

        <div class="flex items-center justify-between px-2">
          <div class="flex items-center gap-2.5 min-w-0">
            <div
              class="w-8 h-8 rounded-full bg-slate-800 border border-slate-700 flex items-center justify-center text-xs font-bold text-accent uppercase shrink-0"
            >
              {{ authStore.user?.username?.slice(0, 2) || "AD" }}
            </div>
            <div class="truncate">
              <p class="text-xs font-semibold text-slate-200 truncate">{{ authStore.user?.username }}</p>
              <p class="text-[10px] text-slate-500 uppercase font-mono">{{ authStore.user?.role }}</p>
            </div>
          </div>
          <button
            @click="handleLogout"
            class="p-2 rounded-lg text-slate-400 hover:text-rose-400 hover:bg-rose-500/10 transition-colors"
            :title="t('nav.signOut')"
          >
            <LogOut class="w-4 h-4" />
          </button>
        </div>
      </div>
    </aside>

    <!-- Main Content with Keep-Alive Cache (Extra pb-24 on mobile prevents bottom tab overlap) -->
    <main class="flex-1 min-w-0 p-4 pb-24 md:pb-6 md:p-6 lg:p-8 max-w-[1600px] mx-auto w-full">
      <router-view v-slot="{ Component }">
        <keep-alive :include="cachedViews">
          <component :is="Component" />
        </keep-alive>
      </router-view>
    </main>

    <!-- Mobile Native-Style Bottom Navigation Bar (Sticky fixed bottom, sleek glassmorphism) -->
    <nav
      class="md:hidden fixed bottom-0 left-0 right-0 z-40 bg-[#0d121c]/95 backdrop-blur border-t border-[#1e293b] pb-safe shadow-[0_-8px_24px_rgba(0,0,0,0.5)]"
    >
      <div class="grid grid-cols-6 items-center justify-around h-14 max-w-md mx-auto px-1">
        <router-link
          v-for="item in mobileNavItems"
          :key="item.path"
          :to="item.path"
          class="flex flex-col items-center justify-center h-full relative group transition-colors duration-150"
          :class="
            route.path === item.path
              ? 'text-[#00f090] font-semibold'
              : 'text-slate-400 hover:text-slate-200'
          "
        >
          <!-- Active Indicator Pill/Bar -->
          <span
            v-if="route.path === item.path"
            class="absolute top-0 left-1/2 -translate-x-1/2 w-8 h-0.5 bg-[#00f090] rounded-full shadow-[0_0_8px_#00f090]"
          ></span>

          <component
            :is="item.icon"
            class="w-5 h-5 transition-transform duration-150 group-active:scale-90"
            :class="route.path === item.path ? 'text-[#00f090]' : 'text-slate-400'"
          />
          <span class="text-[10px] mt-1 tracking-tight leading-none truncate max-w-full px-0.5">
            {{ item.name }}
          </span>
        </router-link>
      </div>
    </nav>
  </div>
</template>
