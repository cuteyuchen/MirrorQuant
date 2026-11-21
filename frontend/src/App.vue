<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { Activity, ClipboardList, Ellipsis, GitFork, Globe, LayoutDashboard, LogOut, Network, Settings, UserCog, Users, Wallet } from "lucide-vue-next";
import { useAuthStore } from "@/stores/auth";
import { useBoardStore } from "@/stores/board";
import { useSocketStore } from "@/stores/socket";
import { useDataCacheStore } from "@/stores/dataCache";
import { useI18n } from "@/i18n";
import BaseModal from "@/components/BaseModal.vue";
import ConfirmDialog from "@/components/ConfirmDialog.vue";
import GlobalStatusBar from "@/components/board/GlobalStatusBar.vue";
import LangToggle from "@/components/LangToggle.vue";
import ThemeToggle from "@/components/ThemeToggle.vue";
import ToastHost from "@/components/ToastHost.vue";

const auth = useAuthStore();
const board = useBoardStore();
const socket = useSocketStore();
const cache = useDataCacheStore();
const route = useRoute();
const router = useRouter();
const { t } = useI18n();
const appVersion = __APP_VERSION__;
const moreOpen = ref(false);
const loggingOut = ref(false);
const intentsError = ref(false);
const showShell = computed(() => auth.authenticated && route.meta.fullscreen !== true);
const isAdmin = computed(() => auth.user?.role === "admin");
// 普通用户只看自己的看板、账户、绑定、执行记录与公开监控；
// 带单员、代理、设置与用户管理是管理员专属导航（带单员列表接口本身仍对普通用户开放，只是不在导航里出现）。
const allNavItems = [
  { key: "dashboard", path: "/dashboard", icon: LayoutDashboard },
  { key: "executions", path: "/executions", icon: ClipboardList },
  { key: "leads", path: "/leads", icon: Users },
  { key: "accounts", path: "/accounts", icon: Wallet },
  { key: "bindings", path: "/bindings", icon: GitFork },
  { key: "proxies", path: "/proxies", icon: Network },
  { key: "settings", path: "/settings", icon: Settings },
  { key: "users", path: "/users", icon: UserCog },
  { key: "publicFeed", path: "/public", icon: Globe },
];
const navItems = computed(() => allNavItems.filter(item => isAdmin.value || ["dashboard", "executions", "accounts", "bindings", "publicFeed"].includes(item.key)));
const navGroups = computed(() => [
  { key: "monitoring", items: navItems.value.filter(item => ["dashboard", "executions"].includes(item.key)) },
  { key: "configuration", items: navItems.value.filter(item => ["leads", "accounts", "bindings", "proxies"].includes(item.key)) },
  { key: "system", items: navItems.value.filter(item => ["settings", "users", "publicFeed"].includes(item.key)) },
]);
const mobileItems = computed(() => navItems.value.filter(item => ["dashboard", "executions", "leads", "bindings"].includes(item.key)));
const moreItems = computed(() => navItems.value.filter(item => !mobileItems.value.includes(item)));
const pendingCount = computed(() => intentsError.value ? null : cache.intents?.data.length ?? null);
// 侧栏底部的用户头像只取用户名首字符，不依赖外部头像服务。
const userInitial = computed(() => (auth.user?.username?.trim() || "?").slice(0, 1).toUpperCase());
function isActive(path: string) { return route.path === path || route.path.startsWith(`${path}/`); }
let pollTimer: ReturnType<typeof setInterval> | undefined;
let eventTimer: ReturnType<typeof setTimeout> | undefined;
function stop() { clearInterval(pollTimer); clearTimeout(eventTimer); pollTimer = undefined; eventTimer = undefined; }
function refreshGlobal(force = false) {
  void board.refresh(force);
  void cache.fetchIntents(force).then(() => { intentsError.value = false; }).catch(() => { intentsError.value = true; });
}
// 认证与错误页不启动私有轮询；待处理角标与执行页共享最新缓存。
watch(showShell, active => {
  stop();
  if (active) {
    socket.connect(); refreshGlobal();
    pollTimer = setInterval(() => { refreshGlobal(true); }, 6000);
  } else { socket.disconnect(); board.clear(); }
}, { immediate: true });
// 有界事件数组满 50 条后仍由单调递增序号触发刷新。
watch(() => socket.eventSequence, () => {
  if (!showShell.value) return;
  clearTimeout(eventTimer);
  eventTimer = setTimeout(() => { refreshGlobal(true); }, 200);
});
watch(() => route.fullPath, () => { moreOpen.value = false; });
onUnmounted(() => { stop(); socket.disconnect(); });
async function logout() {
  if (loggingOut.value) return;
  loggingOut.value = true;
  try { await auth.logout(); }
  finally { loggingOut.value = false; await router.replace("/login"); }
}
</script>
<template>
  <div class="min-h-dvh">
    <a v-if="showShell" href="#main-content" class="sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-50 focus:rounded-lg focus:bg-accent focus:p-3 focus:font-semibold focus:text-onAccent">{{ t("nav.skipToContent") }}</a>
    <div class="flex min-w-0">
      <!-- 桌面侧栏：整屏高度吸附，品牌在上、导航居中滚动、用户与主题 / 语言 / 登出固定在底部。 -->
      <aside v-if="showShell" class="sticky top-0 hidden h-dvh w-56 shrink-0 flex-col border-r border-border/60 bg-surface/80 backdrop-blur md:flex xl:w-60">
        <RouterLink to="/dashboard" class="flex items-center gap-3 px-4 py-4">
          <span class="brand-mark"><Activity :size="19" aria-hidden="true" /></span>
          <span class="min-w-0">
            <span class="block text-base font-bold leading-tight tracking-tight">{{ t("app.name") }}</span>
            <span class="block text-xs text-faint">{{ t("app.brandSub") }} · v{{ appVersion }}</span>
          </span>
        </RouterLink>
        <nav :aria-label="t('nav.main')" class="flex-1 space-y-5 overflow-y-auto px-3 py-2">
          <div v-for="group in navGroups" :key="group.key">
            <p class="eyebrow mb-1.5 px-3">{{ t(`nav.group.${group.key}`) }}</p>
            <div class="space-y-1">
              <RouterLink v-for="item in group.items" :key="item.path" :to="item.path" class="flex min-h-10 items-center gap-3 rounded-xl border px-3 py-2 text-sm font-medium transition"
                :class="isActive(item.path) ? 'border-accent/25 bg-accent/10 text-accent shadow-sm' : 'border-transparent text-muted hover:bg-surface2 hover:text-foreground'" :aria-current="isActive(item.path) ? 'page' : undefined">
                <component :is="item.icon" :size="18" class="shrink-0" aria-hidden="true" /><span class="min-w-0 flex-1 truncate">{{ t(`nav.${item.key}`) }}</span>
                <span v-if="item.key === 'executions' && pendingCount != null && pendingCount > 0" class="rounded-full bg-warning/15 px-1.5 text-xs font-semibold tabular-nums text-warning" :aria-label="t('nav.pendingCount', { n: pendingCount })">{{ pendingCount }}</span>
              </RouterLink>
            </div>
          </div>
        </nav>
        <div class="border-t border-border/60 p-3">
          <div class="flex min-w-0 items-center gap-2.5 px-1">
            <span class="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-border/70 bg-surface2 text-sm font-bold" aria-hidden="true">{{ userInitial }}</span>
            <span class="min-w-0 flex-1">
              <span class="block truncate text-sm font-semibold">{{ auth.user?.username ?? t("common.unknown") }}</span>
              <span v-if="auth.user?.role" class="block truncate text-xs text-faint">{{ auth.user.role }}</span>
            </span>
          </div>
          <div class="mt-2 flex items-center justify-between gap-1">
            <div class="flex items-center gap-1"><ThemeToggle /><LangToggle /></div>
            <button type="button" class="icon-button" :disabled="loggingOut" :title="t('nav.signOut')" :aria-label="t('nav.signOut')" @click="logout"><LogOut :size="18" aria-hidden="true" /></button>
          </div>
        </div>
      </aside>
      <div class="flex min-w-0 flex-1 flex-col">
        <!-- 移动顶栏：只放品牌与全局开关，主导航在底栏。 -->
        <header v-if="showShell" class="flex items-center justify-between gap-2 border-b border-border/60 bg-surface/80 px-3 py-1.5 md:hidden">
          <RouterLink to="/dashboard" class="flex min-w-0 items-center gap-2.5">
            <span class="brand-mark"><Activity :size="18" aria-hidden="true" /></span>
            <span class="truncate text-base font-bold tracking-tight">{{ t("app.name") }} <span class="text-xs font-normal text-faint">v{{ appVersion }}</span></span>
          </RouterLink>
          <div class="flex shrink-0 items-center">
            <ThemeToggle /><LangToggle />
            <button type="button" class="icon-button" :disabled="loggingOut" :aria-label="t('nav.signOut')" :title="t('nav.signOut')" @click="logout"><LogOut :size="18" aria-hidden="true" /></button>
          </div>
        </header>
        <GlobalStatusBar v-if="showShell" />
        <main id="main-content" tabindex="-1" class="min-w-0 flex-1 focus:outline-none" :class="showShell ? 'mx-auto w-full max-w-[1680px] px-3 py-4 pb-28 md:px-6 md:py-5 md:pb-10' : ''">
          <RouterView v-slot="{ Component }">
            <KeepAlive v-if="showShell" :key="auth.user?.id"><component :is="Component" /></KeepAlive>
            <component :is="Component" v-else />
          </RouterView>
        </main>
      </div>
    </div>
    <!-- 移动底栏：四个最常用入口 + “更多”，当前页用顶部指示条标出。 -->
    <nav v-if="showShell" :aria-label="t('nav.main')" class="pb-safe fixed inset-x-0 bottom-0 z-30 grid grid-cols-5 border-t border-border/60 bg-surface/90 backdrop-blur-md md:hidden">
      <RouterLink v-for="item in mobileItems" :key="item.path" :to="item.path" class="relative flex min-h-16 min-w-0 flex-col items-center justify-center gap-1 px-1 py-2 text-xs font-medium transition"
        :class="isActive(item.path) ? 'text-accent' : 'text-muted'" :aria-current="isActive(item.path) ? 'page' : undefined">
        <span v-if="isActive(item.path)" class="absolute inset-x-5 top-0 h-0.5 rounded-full bg-accent" aria-hidden="true" />
        <span class="relative"><component :is="item.icon" :size="21" aria-hidden="true" /><span v-if="item.key === 'executions' && pendingCount != null && pendingCount > 0" class="absolute -right-3 -top-2 min-w-4 rounded-full bg-warning px-1 text-center text-xs font-bold tabular-nums text-onAccent" :aria-label="t('nav.pendingCount', { n: pendingCount })">{{ pendingCount }}</span></span>
        <span class="max-w-full truncate">{{ t(`nav.${item.key}`) }}</span>
      </RouterLink>
      <button type="button" class="relative flex min-h-16 flex-col items-center justify-center gap-1 text-xs font-medium transition" :class="moreItems.some(item => isActive(item.path)) ? 'text-accent' : 'text-muted'" :aria-expanded="moreOpen" @click="moreOpen = true">
        <span v-if="moreItems.some(item => isActive(item.path))" class="absolute inset-x-5 top-0 h-0.5 rounded-full bg-accent" aria-hidden="true" />
        <Ellipsis :size="21" aria-hidden="true" />{{ t("nav.more") }}
      </button>
    </nav>
    <BaseModal :open="moreOpen" :title="t('nav.more')" @close="moreOpen = false">
      <nav class="grid gap-1">
        <RouterLink v-for="item in moreItems" :key="item.path" :to="item.path" class="flex min-h-12 items-center gap-3 rounded-xl border px-3 text-sm font-medium transition"
          :class="isActive(item.path) ? 'border-accent/25 bg-accent/10 text-accent' : 'border-transparent text-foreground hover:bg-surface2'" @click="moreOpen = false">
          <component :is="item.icon" :size="20" aria-hidden="true" />{{ t(`nav.${item.key}`) }}
        </RouterLink>
      </nav>
    </BaseModal>
    <ConfirmDialog /><ToastHost />
  </div>
</template>
