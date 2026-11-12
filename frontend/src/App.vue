<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  Activity,
  BarChart3,
  Cpu,
  Globe,
  KeyRound,
  Layers,
  LogOut,
  Menu,
  Network,
  Settings,
  ShieldAlert,
  Users,
  X,
} from "lucide-vue-next";
import { useAuthStore } from "@/stores/auth";
import { useSocketStore } from "@/stores/socket";

const route = useRoute();
const router = useRouter();
const authStore = useAuthStore();
const socketStore = useSocketStore();
const mobileMenuOpen = ref(false);

const isPublicOrAuthLayout = computed(() => {
  return ["/login", "/setup", "/public"].includes(route.path);
});

const navItems = [
  { name: "Dashboard", path: "/dashboard", icon: BarChart3 },
  { name: "Leads & PA", path: "/leads", icon: Users },
  { name: "Accounts", path: "/accounts", icon: KeyRound },
  { name: "Copy Matrix", path: "/bindings", icon: Layers },
  { name: "Proxy Nodes", path: "/proxies", icon: Network },
  { name: "Settings & Audit", path: "/settings", icon: Settings },
];

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
    <!-- Mobile Topbar -->
    <header
      class="md:hidden flex items-center justify-between px-4 py-3 bg-surface border-b border-slate-800 sticky top-0 z-40"
    >
      <div class="flex items-center gap-2.5">
        <div class="w-8 h-8 rounded-lg bg-accent/10 border border-accent/30 flex items-center justify-center">
          <Cpu class="w-4 h-4 text-accent" />
        </div>
        <span class="font-bold tracking-tight text-slate-100">MirrorQuant</span>
      </div>
      <div class="flex items-center gap-3">
        <span
          class="inline-flex items-center gap-1.5 text-xs px-2 py-0.5 rounded-full border"
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
          {{ socketStore.connected ? "LIVE" : "OFF" }}
        </span>
        <button
          @click="mobileMenuOpen = !mobileMenuOpen"
          class="p-2 rounded-lg bg-surface2 text-slate-300 hover:text-white"
          aria-label="Toggle Menu"
        >
          <X v-if="mobileMenuOpen" class="w-5 h-5" />
          <Menu v-else class="w-5 h-5" />
        </button>
      </div>
    </header>

    <!-- Mobile Drawer Overlay -->
    <div
      v-if="mobileMenuOpen"
      @click="mobileMenuOpen = false"
      class="fixed inset-0 bg-black/60 backdrop-blur-sm z-40 md:hidden"
    ></div>

    <!-- Sidebar -->
    <aside
      class="fixed md:sticky top-0 left-0 z-50 h-screen w-64 bg-surface border-r border-slate-800/80 flex flex-col justify-between transition-transform duration-200 ease-in-out"
      :class="mobileMenuOpen ? 'translate-x-0' : '-translate-x-full md:translate-x-0'"
    >
      <div>
        <!-- Brand Header -->
        <div class="hidden md:flex items-center justify-between px-5 py-5 border-b border-slate-800/80">
          <div class="flex items-center gap-3">
            <div
              class="w-9 h-9 rounded-xl bg-gradient-to-br from-accent/20 to-brand/20 border border-accent/30 flex items-center justify-center shadow-lg shadow-accent/10"
            >
              <Cpu class="w-5 h-5 text-accent" />
            </div>
            <div>
              <h1 class="font-bold text-base tracking-tight text-white">MirrorQuant</h1>
              <p class="text-[11px] text-slate-400 font-mono">v1.0 · QUANT TERMINAL</p>
            </div>
          </div>
        </div>

        <!-- Navigation -->
        <nav class="p-3 space-y-1 mt-2">
          <router-link
            v-for="item in navItems"
            :key="item.path"
            :to="item.path"
            @click="mobileMenuOpen = false"
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

          <div class="pt-4 mt-4 border-t border-slate-800/80">
            <router-link
              to="/public"
              @click="mobileMenuOpen = false"
              class="flex items-center gap-3 px-3.5 py-2.5 rounded-xl text-sm font-medium text-slate-400 hover:text-cyan-300 hover:bg-slate-800/50 transition-all"
            >
              <Globe class="w-4 h-4" />
              <span>Public Feed</span>
            </router-link>
          </div>
        </nav>
      </div>

      <!-- Footer User / Stream Info -->
      <div class="p-4 border-t border-slate-800/80 space-y-3">
        <div class="flex items-center justify-between px-2 py-1.5 rounded-lg bg-background/60 border border-slate-800">
          <div class="flex items-center gap-2 text-xs text-slate-400">
            <Activity class="w-3.5 h-3.5 text-accent" />
            <span>Stream WS</span>
          </div>
          <span
            class="inline-flex items-center gap-1.5 text-[11px] font-mono"
            :class="socketStore.connected ? 'text-emerald-400' : 'text-rose-400'"
          >
            <span
              class="w-1.5 h-1.5 rounded-full"
              :class="socketStore.connected ? 'bg-emerald-400 animate-pulse' : 'bg-rose-400'"
            ></span>
            {{ socketStore.connected ? "CONNECTED" : "RECONNECTING" }}
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
            title="Sign out"
          >
            <LogOut class="w-4 h-4" />
          </button>
        </div>
      </div>
    </aside>

    <!-- Main Content -->
    <main class="flex-1 min-w-0 p-4 md:p-6 lg:p-8 max-w-[1600px] mx-auto w-full">
      <router-view />
    </main>
  </div>
</template>
