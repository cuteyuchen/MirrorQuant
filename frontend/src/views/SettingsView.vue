<script setup lang="ts">
import { computed, onActivated, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import {
  AlertTriangle,
  ArrowRight,
  Bell,
  CheckCircle2,
  Cpu,
  KeyRound,
  Lock,
  Network,
  Plus,
  QrCode,
  RefreshCw,
  Save,
  Send,
  Shield,
  ShieldAlert,
  ShieldCheck,
  Sliders,
  Unlock,
  Wifi,
  X,
} from "lucide-vue-next";
import { api } from "@/api/client";
import { useDataCacheStore } from "@/stores/dataCache";
import { useAuthStore } from "@/stores/auth";
import { useI18n } from "@/i18n";
import { formatUsd } from "@/utils/format";

defineOptions({ name: "SettingsView" });

const cache = useDataCacheStore();
const authStore = useAuthStore();
const { t, isZh } = useI18n();

type TabKey = "accounts" | "trading" | "resilience" | "network" | "notifications" | "security";
const activeTab = ref<TabKey>("accounts");

const settingsData = ref<any>(cache.settings?.data ? structuredClone(cache.settings.data) : null);
const accounts = ref<any[]>(cache.accounts?.data || []);
const proxies = ref<any[]>(cache.proxies?.data || []);
const auditLogs = ref<any[]>(cache.auditLogs?.data || []);
const securityGuards = ref<any[]>(cache.securityGuards?.data || []);

const saving = ref(false);
const applyingDefaults = ref(false);
const savedMsg = ref("");
const applyMsg = ref("");
const testMsg = ref("");

// Password change
const oldPassword = ref("");
const newPassword = ref("");
const pwMsg = ref("");
const pwError = ref("");
const changingPw = ref(false);

// TOTP modal/flow
const securityRouter = useRouter();
const totpPassword = ref("");
const totpSetupData = ref<{ secret: string; otpauthUri: string; qrSvgDataUri: string } | null>(null);
const totpCodeInput = ref("");
const totpError = ref("");
const totpSuccess = ref("");
const showTotpModal = ref(false);
const totpDisableCode = ref("");
const showDisableModal = ref(false);

// Quick Add Account Modal inside Settings
const showAddAcctModal = ref(false);
const acctForm = ref({
  name: "",
  exchange: "okx",
  environment: "demo",
  apiKey: "",
  secretKey: "",
  passphrase: "",
  proxyId: null as number | null,
  marginMode: "cross",
  positionMode: "hedge",
});
const acctError = ref("");
const testingAcctId = ref<number | null>(null);
const acctTestResult = ref<Record<number, any>>({});

async function loadData(force = false) {
  const [sRes, aItems, pItems, lItems, gItems] = await Promise.all([
    cache.fetchSettings(force, 20000),
    cache.fetchAccounts(force, 15000),
    cache.fetchProxies(force, 20000),
    cache.fetchAuditLogs(force, 20000),
    cache.fetchSecurityGuards(force, 15000),
  ]);
  settingsData.value = JSON.parse(JSON.stringify(sRes));
  accounts.value = aItems;
  proxies.value = pItems;
  auditLogs.value = lItems;
  securityGuards.value = gItems;
}

async function saveSettings() {
  if (!settingsData.value) return;
  saving.value = true;
  savedMsg.value = "";
  try {
    await api.put("/api/settings", {
      global_kill_switch: settingsData.value.global_kill_switch,
      public_monitoring_enabled: settingsData.value.public_monitoring_enabled,
      notifications: settingsData.value.notifications,
      risk_defaults: settingsData.value.risk_defaults,
      resilience: settingsData.value.resilience,
    });
    savedMsg.value = t("set.savedMsg");
    setTimeout(() => (savedMsg.value = ""), 3500);
    await loadData(true);
  } finally {
    saving.value = false;
  }
}

async function applyDefaultsToBindings() {
  if (!confirm(t("set.applyDefaultsBtn") + "?")) return;
  applyingDefaults.value = true;
  applyMsg.value = "";
  try {
    const res = await api.post<{ ok: boolean; updatedCount: number }>("/api/settings/apply-defaults-to-bindings");
    applyMsg.value = t("set.applyDefaultsSuccess", { n: res.updatedCount });
    setTimeout(() => (applyMsg.value = ""), 4000);
    await cache.fetchBindings(true);
  } finally {
    applyingDefaults.value = false;
  }
}

async function testNotify() {
  testMsg.value = t("set.sending");
  try {
    const res = await api.post<any>("/api/settings/test-notify");
    testMsg.value = JSON.stringify(res.results);
  } catch (e: any) {
    testMsg.value = "Error: " + e.message;
  }
}

async function startTotpSetup() {
  totpError.value = "";
  totpCodeInput.value = "";
  try {
    const res = await api.post<any>("/api/auth/2fa/setup", { currentPassword: totpPassword.value });
    totpPassword.value = "";
    totpSetupData.value = res;
    showTotpModal.value = true;
  } catch (e: any) {
    alert("TOTP Setup failed: " + e.message);
  }
}

async function verifyAndEnableTotp() {
  if (!totpCodeInput.value || !totpSetupData.value) return;
  totpError.value = "";
  try {
    await api.post("/api/auth/2fa/enable", {
      code: totpCodeInput.value.trim(),
    });
    showTotpModal.value = false;
    totpSetupData.value = null;
    totpSuccess.value = "TOTP 2FA enabled successfully!";
    setTimeout(() => (totpSuccess.value = ""), 4000);
    await authStore.checkStatus();
    await securityRouter.push("/login");
  } catch (e: any) {
    totpError.value = e.message;
  }
}

async function confirmDisableTotp() {
  if (!totpDisableCode.value) return;
  totpError.value = "";
  try {
    await api.post("/api/auth/2fa/disable", { code: totpDisableCode.value.trim(), currentPassword: totpPassword.value });
    totpPassword.value = "";
    showDisableModal.value = false;
    totpDisableCode.value = "";
    await authStore.checkStatus();
    await securityRouter.push("/login");
  } catch (e: any) {
    totpError.value = e.message;
  }
}

async function changePassword() {
  if (!oldPassword.value || !newPassword.value) return;
  changingPw.value = true;
  pwMsg.value = "";
  pwError.value = "";
  try {
    await api.post("/api/auth/change-password", {
      currentPassword: oldPassword.value,
      newPassword: newPassword.value,
    });
    pwMsg.value = t("set.pwChangedSuccess");
    oldPassword.value = "";
    newPassword.value = "";
    setTimeout(() => (pwMsg.value = ""), 3500);
    await authStore.checkStatus();
    await securityRouter.push("/login");
  } catch (e: any) {
    pwError.value = e.message;
  } finally {
    changingPw.value = false;
  }
}

async function unlockGuard(guardId: number) {
  try {
    await api.post(`/api/settings/security-guards/${guardId}/unlock`);
    await loadData(true);
  } catch (e: any) {
    alert("Unlock failed: " + e.message);
  }
}

async function testAccountConnectivity(id: number) {
  testingAcctId.value = id;
  try {
    const res = await api.post<any>(`/api/accounts/${id}/test`);
    acctTestResult.value[id] = res;
    await loadData(true);
  } catch (e: any) {
    acctTestResult.value[id] = { ok: false, error: e.message };
  } finally {
    testingAcctId.value = null;
  }
}

async function quickCreateAccount() {
  acctError.value = "";
  try {
    await api.post("/api/accounts", acctForm.value);
    showAddAcctModal.value = false;
    acctForm.value = {
      name: "",
      exchange: "okx",
      environment: "demo",
      apiKey: "",
      secretKey: "",
      passphrase: "",
      proxyId: null,
      marginMode: "cross",
      positionMode: "hedge",
    };
    await loadData(true);
  } catch (e: any) {
    acctError.value = e.message;
  }
}


onMounted(() => loadData(false));
onActivated(() => {
  if (cache.settings && !settingsData.value) {
    settingsData.value = JSON.parse(JSON.stringify(cache.settings.data));
  }
  if (cache.accounts) accounts.value = cache.accounts.data;
  if (cache.proxies) proxies.value = cache.proxies.data;
  if (cache.auditLogs) auditLogs.value = cache.auditLogs.data;
  if (cache.securityGuards) securityGuards.value = cache.securityGuards.data;
  loadData(false);
});
</script>

<template>
  <div class="space-y-6" v-if="settingsData">
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
      <div>
        <h2 class="text-xl sm:text-2xl font-bold text-white tracking-tight">{{ t("set.title") }}</h2>
        <p class="text-xs sm:text-sm text-slate-400 mt-0.5">{{ t("set.subtitle") }}</p>
      </div>
      <div class="flex items-center gap-2.5">
        <button
          v-if="activeTab === 'trading'"
          @click="applyDefaultsToBindings"
          :disabled="applyingDefaults"
          class="px-3.5 py-2.5 rounded-xl bg-surface2 hover:bg-slate-700 text-accent font-semibold text-xs sm:text-sm flex items-center gap-2 border border-accent/30 transition-all"
        >
          <Sliders class="w-4 h-4" />
          <span>{{ applyingDefaults ? t("set.applying") : t("set.applyDefaultsBtn") }}</span>
        </button>
        <button
          @click="saveSettings"
          :disabled="saving"
          class="px-4 py-2.5 rounded-xl bg-gradient-to-r from-accent to-brand text-slate-950 font-semibold text-xs sm:text-sm flex items-center gap-2 hover:opacity-90 transition-all shadow-lg shadow-accent/20"
        >
          <Save class="w-4 h-4" />
          <span>{{ saving ? t("set.saving") : t("set.saveBtn") }}</span>
        </button>
      </div>
    </div>

    <!-- Feedback messages -->
    <div v-if="savedMsg" class="p-3 rounded-xl bg-emerald-500/15 border border-emerald-500/30 text-emerald-300 text-xs flex items-center gap-2">
      <CheckCircle2 class="w-4 h-4" />
      <span>{{ savedMsg }}</span>
    </div>
    <div v-if="applyMsg" class="p-3 rounded-xl bg-cyan-500/15 border border-cyan-500/30 text-cyan-300 text-xs flex items-center gap-2">
      <CheckCircle2 class="w-4 h-4" />
      <span>{{ applyMsg }}</span>
    </div>
    <div v-if="totpSuccess" class="p-3 rounded-xl bg-emerald-500/15 border border-emerald-500/30 text-emerald-300 text-xs flex items-center gap-2">
      <CheckCircle2 class="w-4 h-4" />
      <span>{{ totpSuccess }}</span>
    </div>

    <!-- Navigation Tabs -->
    <div class="flex flex-wrap items-center gap-2 border-b border-slate-800/80 pb-2">
      <button
        @click="activeTab = 'accounts'"
        class="px-3.5 py-2 rounded-xl text-xs sm:text-sm font-semibold flex items-center gap-2 transition-all"
        :class="activeTab === 'accounts' ? 'bg-accent/15 text-accent border border-accent/30' : 'text-slate-400 hover:text-white hover:bg-surface2'"
      >
        <KeyRound class="w-4 h-4" />
        <span>{{ t("set.tabAccounts") }}</span>
        <span class="px-1.5 py-0.2 rounded-md bg-background text-[10px] font-mono text-slate-300">{{ accounts.length }}</span>
      </button>

      <button
        @click="activeTab = 'trading'"
        class="px-3.5 py-2 rounded-xl text-xs sm:text-sm font-semibold flex items-center gap-2 transition-all"
        :class="activeTab === 'trading' ? 'bg-accent/15 text-accent border border-accent/30' : 'text-slate-400 hover:text-white hover:bg-surface2'"
      >
        <Sliders class="w-4 h-4" />
        <span>{{ t("set.tabTrading") }}</span>
      </button>

      <button
        @click="activeTab = 'resilience'"
        class="px-3.5 py-2 rounded-xl text-xs sm:text-sm font-semibold flex items-center gap-2 transition-all"
        :class="activeTab === 'resilience' ? 'bg-accent/15 text-accent border border-accent/30' : 'text-slate-400 hover:text-white hover:bg-surface2'"
      >
        <ShieldCheck class="w-4 h-4" />
        <span>{{ t("set.tabResilience") }}</span>
      </button>

      <button
        @click="activeTab = 'network'"
        class="px-3.5 py-2 rounded-xl text-xs sm:text-sm font-semibold flex items-center gap-2 transition-all"
        :class="activeTab === 'network' ? 'bg-accent/15 text-accent border border-accent/30' : 'text-slate-400 hover:text-white hover:bg-surface2'"
      >
        <Network class="w-4 h-4" />
        <span>{{ t("set.tabNetwork") }}</span>
        <span class="px-1.5 py-0.2 rounded-md bg-background text-[10px] font-mono text-slate-300">{{ proxies.length }}</span>
      </button>

      <button
        @click="activeTab = 'notifications'"
        class="px-3.5 py-2 rounded-xl text-xs sm:text-sm font-semibold flex items-center gap-2 transition-all"
        :class="activeTab === 'notifications' ? 'bg-accent/15 text-accent border border-accent/30' : 'text-slate-400 hover:text-white hover:bg-surface2'"
      >
        <Bell class="w-4 h-4" />
        <span>{{ t("set.tabNotifications") }}</span>
      </button>

      <button
        @click="activeTab = 'security'"
        class="px-3.5 py-2 rounded-xl text-xs sm:text-sm font-semibold flex items-center gap-2 transition-all"
        :class="activeTab === 'security' ? 'bg-accent/15 text-accent border border-accent/30' : 'text-slate-400 hover:text-white hover:bg-surface2'"
      >
        <ShieldAlert class="w-4 h-4" />
        <span>{{ t("set.tabSecurity") }}</span>
      </button>
    </div>

    <!-- TAB 1: ACCOUNTS -->
    <div v-if="activeTab === 'accounts'" class="space-y-4">
      <div class="p-4 rounded-2xl bg-surface border border-slate-800/80 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <p class="text-xs text-slate-400 leading-relaxed max-w-2xl">
          {{ t("set.exchangeSupportNote") }}
        </p>
        <button
          @click="showAddAcctModal = true"
          class="px-3.5 py-2 rounded-xl bg-accent text-slate-950 font-semibold text-xs flex items-center gap-1.5 shrink-0 hover:opacity-90"
        >
          <Plus class="w-3.5 h-3.5" />
          <span>{{ t("set.quickAddAccount") }}</span>
        </button>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div
          v-for="acct in accounts"
          :key="acct.id"
          class="rounded-2xl bg-surface border border-slate-800/80 p-5 flex flex-col justify-between space-y-3"
        >
          <div>
            <div class="flex items-start justify-between gap-2">
              <div class="flex items-center gap-3">
                <div class="w-10 h-10 rounded-xl bg-surface2 border border-slate-700 flex items-center justify-center font-bold text-xs uppercase text-accent">
                  {{ acct.exchange }}
                </div>
                <div>
                  <div class="flex items-center gap-2">
                    <h4 class="font-bold text-sm text-white">{{ acct.name }}</h4>
                    <span
                      class="px-2 py-0.5 rounded text-[10px] font-mono uppercase border"
                      :class="acct.environment === 'live' ? 'bg-rose-500/15 text-rose-400 border-rose-500/30' : 'bg-cyan-500/15 text-cyan-400 border-cyan-500/30'"
                    >
                      {{ acct.environment }}
                    </span>
                  </div>
                  <p class="text-xs font-mono text-slate-400 mt-0.5 flex items-center gap-1.5">
                    <Shield class="w-3 h-3 text-emerald-400" />
                    <span>{{ acct.apiKeyMasked }}</span>
                    <span v-if="acct.clockOffsetMs" class="text-[10px] text-slate-500">({{ acct.clockOffsetMs }}ms)</span>
                  </p>
                </div>
              </div>
              <span
                class="px-2.5 py-0.5 rounded-full text-[10px] font-mono border"
                :class="acct.status === 'connected' ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30' : 'bg-rose-500/10 text-rose-400 border-rose-500/30'"
              >
                {{ acct.status.toUpperCase() }}
              </span>
            </div>

            <div class="grid grid-cols-3 gap-2 mt-3 p-2.5 rounded-xl bg-background/60 border border-slate-800 text-xs font-mono">
              <div>
                <span class="text-[10px] text-slate-500 block">EQUITY</span>
                <span class="font-bold text-white">{{ formatUsd(acct.usdtEquity) }}</span>
              </div>
              <div>
                <span class="text-[10px] text-slate-500 block">AVAIL</span>
                <span class="text-slate-200">{{ formatUsd(acct.availEquity) }}</span>
              </div>
              <div>
                <span class="text-[10px] text-slate-500 block">UPNL</span>
                <span :class="acct.unrealizedPnl >= 0 ? 'text-emerald-400' : 'text-rose-400'">
                  {{ formatUsd(acct.unrealizedPnl) }}
                </span>
              </div>
            </div>

            <div v-if="acctTestResult[acct.id]" class="mt-2.5 p-2 rounded-lg text-xs font-mono border"
              :class="acctTestResult[acct.id].ok ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-300' : 'bg-rose-500/10 border-rose-500/30 text-rose-300'"
            >
              {{ acctTestResult[acct.id].ok ? 'OK: Connected & Sync' : acctTestResult[acct.id].error }}
            </div>
          </div>

          <div class="flex items-center justify-between pt-2 border-t border-slate-800/80">
            <button
              @click="testAccountConnectivity(acct.id)"
              :disabled="testingAcctId === acct.id"
              class="px-3 py-1.5 rounded-lg bg-accent/15 hover:bg-accent/25 text-accent text-xs font-medium flex items-center gap-1.5"
            >
              <Wifi class="w-3.5 h-3.5" />
              <span>{{ testingAcctId === acct.id ? t("accts.testing") : t("accts.testApi") }}</span>
            </button>
            <router-link to="/accounts" class="text-xs text-slate-400 hover:text-white flex items-center gap-1">
              <span>{{ t("accts.title") }}</span>
              <ArrowRight class="w-3 h-3" />
            </router-link>
          </div>
        </div>
      </div>
    </div>

    <!-- TAB 2: TRADING & TP/SL RULES -->
    <div v-if="activeTab === 'trading'" class="space-y-6">
      <div class="p-4 rounded-2xl bg-surface border border-slate-800/80">
        <p class="text-xs text-slate-300 leading-relaxed">{{ t("set.tradingDefaultsDesc") }}</p>
      </div>

      <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <!-- Execution & Slippage -->
        <div class="rounded-2xl bg-surface border border-slate-800/80 p-5 space-y-4">
          <h3 class="text-sm font-bold text-white flex items-center gap-2">
            <Sliders class="w-4 h-4 text-accent" />
            <span>{{ t("set.secSlippage") }}</span>
          </h3>
          <div class="space-y-3 text-xs">
            <div>
              <label class="block text-slate-400 mb-1">{{ t("set.maxSlip") }}</label>
              <input v-model.number="settingsData.risk_defaults.max_slippage_pct" type="number" step="0.1" min="0.1" max="50" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-white font-mono text-sm" />
            </div>
            <div>
              <label class="block text-slate-400 mb-1">{{ t("set.maxSpread") }}</label>
              <input v-model.number="settingsData.risk_defaults.max_spread_pct" type="number" step="0.05" min="0.05" max="10" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-white font-mono text-sm" />
            </div>
            <div>
              <label class="block text-slate-400 mb-1">{{ t("set.iocOffset") }}</label>
              <input v-model.number="settingsData.risk_defaults.ioc_offset_pct" type="number" step="0.05" min="0" max="10" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-white font-mono text-sm" />
            </div>
            <div>
              <label class="block text-slate-400 mb-1">{{ t("set.execMode") }}</label>
              <select v-model="settingsData.risk_defaults.order_mode" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-white text-xs">
                <option value="ioc_then_market">{{ t("set.modeIocMarket") }}</option>
                <option value="ioc_only">{{ t("set.modeIocOnly") }}</option>
                <option value="market_only">{{ t("set.modeMarketOnly") }}</option>
              </select>
            </div>
          </div>
        </div>

        <!-- Leverage & Breakers -->
        <div class="rounded-2xl bg-surface border border-slate-800/80 p-5 space-y-4">
          <h3 class="text-sm font-bold text-white flex items-center gap-2">
            <AlertTriangle class="w-4 h-4 text-amber-400" />
            <span>{{ t("set.secRiskCaps") }}</span>
          </h3>
          <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs">
            <div>
              <label class="block text-slate-400 mb-1">{{ t("set.maxSingleLev") }}</label>
              <input v-model.number="settingsData.risk_defaults.max_single_leverage" type="number" step="0.5" min="1" max="100" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-white font-mono text-sm" />
            </div>
            <div>
              <label class="block text-slate-400 mb-1">{{ t("set.maxTotalLev") }}</label>
              <input v-model.number="settingsData.risk_defaults.max_total_leverage" type="number" step="0.5" min="1" max="100" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-white font-mono text-sm" />
            </div>
            <div>
              <label class="block text-slate-400 mb-1">{{ t("set.maxInstLev") }}</label>
              <input v-model.number="settingsData.risk_defaults.max_instrument_leverage" type="number" step="1" min="1" max="125" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-white font-mono text-sm" />
            </div>
            <div>
              <label class="block text-slate-400 mb-1">{{ t("set.mddBreaker") }}</label>
              <input v-model.number="settingsData.risk_defaults.max_drawdown_pct" type="number" step="1" min="1" max="100" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-white font-mono text-sm" />
            </div>
            <div class="sm:col-span-2">
              <label class="block text-slate-400 mb-1">{{ t("set.dailyLossBreaker") }}</label>
              <input v-model.number="settingsData.risk_defaults.daily_max_loss_pct" type="number" step="0.5" min="1" max="100" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-white font-mono text-sm" />
            </div>
          </div>
        </div>
      </div>

      <!-- TP/SL Section -->
      <div class="rounded-2xl bg-surface border border-slate-800/80 p-5 space-y-4">
        <h3 class="text-sm font-bold text-white flex items-center gap-2">
          <ShieldCheck class="w-4 h-4 text-emerald-400" />
          <span>{{ t("set.secTpSl") }}</span>
        </h3>
        <div>
          <label class="block text-xs text-slate-400 mb-1.5">{{ t("set.tpslMode") }}</label>
          <select v-model="settingsData.risk_defaults.tp_sl_mode" class="w-full px-3.5 py-2.5 rounded-xl bg-background border border-slate-700 text-white text-xs sm:text-sm">
            <option value="follow_lead">{{ t("set.tpslFollow") }}</option>
            <option value="independent">{{ t("set.tpslIndependent") }}</option>
            <option value="hybrid_guard">{{ t("set.tpslHybrid") }}</option>
          </select>
        </div>

        <div v-if="settingsData.risk_defaults.tp_sl_mode === 'hybrid_guard'" class="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs p-3.5 rounded-xl bg-background/60 border border-slate-800">
          <div>
            <label class="block text-slate-400 mb-1">{{ t("set.hardSlPct") }}</label>
            <input v-model.number="settingsData.risk_defaults.hard_stop_loss_pct" type="number" step="0.5" min="1" max="95" class="w-full px-3 py-2 rounded-lg bg-surface border border-slate-700 text-white font-mono" />
          </div>
          <div>
            <label class="flex items-center gap-2 text-slate-300 mb-1 cursor-pointer">
              <input v-model="settingsData.risk_defaults.enable_take_profit" type="checkbox" class="rounded bg-surface border-slate-600 text-accent" />
              <span>{{ t("set.enableTp") }}</span>
            </label>
            <input v-model.number="settingsData.risk_defaults.take_profit_pct" :disabled="!settingsData.risk_defaults.enable_take_profit" type="number" step="0.5" min="0.5" max="500" class="w-full px-3 py-2 rounded-lg bg-surface border border-slate-700 text-white font-mono disabled:opacity-40" />
          </div>
        </div>

        <div v-if="settingsData.risk_defaults.tp_sl_mode === 'independent'" class="grid grid-cols-1 sm:grid-cols-3 gap-3 text-xs p-3.5 rounded-xl bg-background/60 border border-slate-800">
          <div>
            <label class="flex items-center gap-2 text-slate-300 mb-1 cursor-pointer">
              <input v-model="settingsData.risk_defaults.enable_stop_loss" type="checkbox" class="rounded bg-surface border-slate-600 text-accent" />
              <span>{{ t("set.enableSl") }}</span>
            </label>
            <input v-model.number="settingsData.risk_defaults.stop_loss_pct" :disabled="!settingsData.risk_defaults.enable_stop_loss" type="number" step="0.5" min="0.5" max="95" class="w-full px-3 py-2 rounded-lg bg-surface border border-slate-700 text-white font-mono disabled:opacity-40" />
          </div>
          <div>
            <label class="flex items-center gap-2 text-slate-300 mb-1 cursor-pointer">
              <input v-model="settingsData.risk_defaults.enable_take_profit" type="checkbox" class="rounded bg-surface border-slate-600 text-accent" />
              <span>{{ t("set.enableTp") }}</span>
            </label>
            <input v-model.number="settingsData.risk_defaults.take_profit_pct" :disabled="!settingsData.risk_defaults.enable_take_profit" type="number" step="0.5" min="0.5" max="500" class="w-full px-3 py-2 rounded-lg bg-surface border border-slate-700 text-white font-mono disabled:opacity-40" />
          </div>
          <div>
            <label class="flex items-center gap-2 text-slate-300 mb-1 cursor-pointer">
              <input v-model="settingsData.risk_defaults.enable_trailing_stop" type="checkbox" class="rounded bg-surface border-slate-600 text-accent" />
              <span>{{ t("set.enableTs") }}</span>
            </label>
            <input v-model.number="settingsData.risk_defaults.trailing_callback_pct" :disabled="!settingsData.risk_defaults.enable_trailing_stop" type="number" step="0.2" min="0.2" max="50" class="w-full px-3 py-2 rounded-lg bg-surface border border-slate-700 text-white font-mono disabled:opacity-40" />
          </div>
        </div>
      </div>
    </div>

    <!-- TAB 3: RESILIENCE & EDGE CASES -->
    <div v-if="activeTab === 'resilience'" class="space-y-6">
      <div class="p-4 rounded-2xl bg-surface border border-slate-800/80">
        <p class="text-xs text-slate-300 leading-relaxed">{{ t("set.resilienceDesc") }}</p>
      </div>

      <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div class="rounded-2xl bg-surface border border-slate-800/80 p-5 space-y-4">
          <h3 class="text-sm font-bold text-white flex items-center gap-2">
            <Cpu class="w-4 h-4 text-cyan-400" />
            <span>断网漏单追单防针 (Catch-up Guard)</span>
          </h3>
          <div class="space-y-3 text-xs">
            <div>
              <label class="block text-slate-300 font-semibold mb-0.5">{{ t("set.missedOrderSec") }}</label>
              <p class="text-[11px] text-slate-500 mb-1.5">{{ t("set.missedOrderSecHint") }}</p>
              <input v-model.number="settingsData.resilience.missed_order_max_age_sec" type="number" step="1" min="10" max="3600" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-white font-mono text-sm" />
            </div>
            <div>
              <label class="block text-slate-300 font-semibold mb-0.5">{{ t("set.catchupDevPct") }}</label>
              <p class="text-[11px] text-slate-500 mb-1.5">{{ t("set.catchupDevHint") }}</p>
              <!-- The API allows any positive float, including thresholds below 0.1%. -->
              <input v-model.number="settingsData.resilience.catchup_max_price_dev_pct" type="number" step="any" :min="Number.MIN_VALUE" max="10" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-white font-mono text-sm" />
            </div>
            <div>
              <label class="block text-slate-300 font-semibold mb-0.5">{{ t("set.clockSyncSec") }}</label>
              <p class="text-[11px] text-slate-500 mb-1.5">{{ t("set.clockSyncHint") }}</p>
              <input v-model.number="settingsData.resilience.clock_sync_interval_sec" type="number" step="30" min="30" max="3600" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-white font-mono text-sm" />
            </div>
          </div>
        </div>

        <div class="rounded-2xl bg-surface border border-slate-800/80 p-5 space-y-4">
          <h3 class="text-sm font-bold text-white flex items-center gap-2">
            <ShieldCheck class="w-4 h-4 text-emerald-400" />
            <span>仓位对账与翻转优化 (Reconciliation & Flip)</span>
          </h3>
          <div class="space-y-3 text-xs">
            <div>
              <label class="block text-slate-300 font-semibold mb-0.5">{{ t("set.reconcileSec") }}</label>
              <p class="text-[11px] text-slate-500 mb-1.5">{{ t("set.reconcileSecHint") }}</p>
              <input v-model.number="settingsData.resilience.reconcile_interval_sec" type="number" step="10" min="15" max="600" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-white font-mono text-sm" />
            </div>
            <div>
              <label class="block text-slate-300 font-semibold mb-1">{{ t("set.orphanPolicy") }}</label>
              <select v-model="settingsData.resilience.auto_close_orphan_positions" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-white text-xs">
                <option value="alert_only">{{ t("set.orphanPolicyAlert") }}</option>
                <option value="auto_close">{{ t("set.orphanPolicyClose") }}</option>
              </select>
            </div>
            <div>
              <label class="block text-slate-300 font-semibold mb-1">{{ t("set.marginPolicy") }}</label>
              <select v-model="settingsData.resilience.insufficient_margin_policy" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-white text-xs">
                <option value="auto_scale_down">{{ t("set.marginPolicyScale") }}</option>
                <option value="skip_alert">{{ t("set.marginPolicySkip") }}</option>
              </select>
            </div>
            <div class="pt-2 border-t border-slate-800">
              <label class="flex items-center justify-between cursor-pointer gap-2">
                <div>
                  <span class="text-xs font-semibold text-white block">{{ t("set.flipCloseFirst") }}</span>
                  <span class="text-[11px] text-slate-400">{{ t("set.flipCloseFirstDesc") }}</span>
                </div>
                <input v-model="settingsData.resilience.flip_close_first" type="checkbox" class="rounded bg-background border-slate-600 text-accent w-4 h-4 shrink-0" />
              </label>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- TAB 4: NETWORK & PROXIES -->
    <div v-if="activeTab === 'network'" class="space-y-4">
      <div class="p-4 rounded-2xl bg-surface border border-slate-800/80 flex items-center justify-between">
        <div>
          <h4 class="text-sm font-bold text-white">{{ t("proxy.title") }}</h4>
          <p class="text-xs text-slate-400 mt-0.5">{{ t("proxy.subtitle") }}</p>
        </div>
        <router-link to="/proxies" class="px-3 py-1.5 rounded-xl bg-surface2 text-accent text-xs font-semibold flex items-center gap-1.5 hover:bg-slate-700">
          <span>{{ t("proxy.title") }} →</span>
        </router-link>
      </div>

      <div class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-3">
        <div v-for="p in proxies" :key="p.id" class="p-4 rounded-xl bg-surface border border-slate-800 text-xs space-y-1.5">
          <div class="flex items-center justify-between">
            <span class="font-bold text-white">{{ p.name }}</span>
            <span class="px-2 py-0.5 rounded text-[10px] font-mono" :class="p.isHealthy ? 'bg-emerald-500/15 text-emerald-400' : 'bg-slate-800 text-slate-400'">
              {{ p.latencyMs ? `${p.latencyMs}ms` : 'Untested' }}
            </span>
          </div>
          <p class="text-slate-400 font-mono truncate text-[11px]">{{ p.proxyUrl }}</p>
          <span class="inline-block px-1.5 py-0.5 rounded bg-surface2 text-slate-300 text-[10px]">{{ p.regionTag || 'DIRECT' }}</span>
        </div>
      </div>
    </div>

    <!-- TAB 5: NOTIFICATIONS -->
    <div v-if="activeTab === 'notifications'" class="space-y-6">
      <div class="rounded-2xl bg-surface border border-slate-800/80 p-5 space-y-4">
        <div class="flex items-center justify-between">
          <h3 class="text-sm font-bold text-white flex items-center gap-2">
            <Bell class="w-4 h-4 text-accent" />
            <span>{{ t("set.notifChannels") }}</span>
          </h3>
          <button
            @click="testNotify"
            class="px-3 py-1 rounded-lg bg-surface2 hover:bg-slate-700 text-xs text-accent flex items-center gap-1.5"
          >
            <Send class="w-3.5 h-3.5" />
            <span>{{ t("set.testAlert") }}</span>
          </button>
        </div>

        <p v-if="testMsg" class="text-[11px] font-mono text-slate-300 bg-background p-2 rounded-lg border border-slate-800 break-all">
          {{ testMsg }}
        </p>

        <div class="space-y-3 text-xs">
          <div class="p-3.5 rounded-xl bg-background/60 border border-slate-800 space-y-2.5">
            <label class="flex items-center gap-2 font-semibold text-slate-200 cursor-pointer">
              <input v-model="settingsData.notifications.telegram_enabled" type="checkbox" class="rounded bg-background border-slate-600 text-accent" />
              <span>{{ t("set.tgLabel") }}</span>
            </label>
            <div v-if="settingsData.notifications.telegram_enabled" class="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
              <input v-model="settingsData.notifications.telegram_bot_token" placeholder="Bot Token (1234:ABC...)" class="px-3 py-2 rounded-lg bg-surface border border-slate-700 text-white font-mono" />
              <input v-model="settingsData.notifications.telegram_chat_id" placeholder="Chat ID" class="px-3 py-2 rounded-lg bg-surface border border-slate-700 text-white font-mono" />
            </div>
          </div>

          <div class="p-3.5 rounded-xl bg-background/60 border border-slate-800 space-y-2.5">
            <label class="flex items-center gap-2 font-semibold text-slate-200 cursor-pointer">
              <input v-model="settingsData.notifications.wecom_enabled" type="checkbox" class="rounded bg-background border-slate-600 text-accent" />
              <span>{{ t("set.wecomLabel") }}</span>
            </label>
            <input
              v-if="settingsData.notifications.wecom_enabled"
              v-model="settingsData.notifications.wecom_webhook_url"
              placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."
              class="w-full px-3 py-2 rounded-lg bg-surface border border-slate-700 text-white font-mono"
            />
          </div>

          <div class="p-3.5 rounded-xl bg-background/60 border border-slate-800 space-y-2.5">
            <label class="flex items-center gap-2 font-semibold text-slate-200 cursor-pointer">
              <input v-model="settingsData.notifications.webhook_enabled" type="checkbox" class="rounded bg-background border-slate-600 text-accent" />
              <span>{{ t("set.webhookLabel") }}</span>
            </label>
            <input
              v-if="settingsData.notifications.webhook_enabled"
              v-model="settingsData.notifications.webhook_url"
              placeholder="https://your-server.com/hook"
              class="w-full px-3 py-2 rounded-lg bg-surface border border-slate-700 text-white font-mono"
            />
          </div>

          <div class="p-3.5 rounded-xl bg-background/60 border border-slate-800 space-y-2.5">
            <label class="flex items-center gap-2 font-semibold text-slate-200 cursor-pointer">
              <input v-model="settingsData.notifications.outbox_enabled" type="checkbox" class="rounded bg-background border-slate-600 text-accent" />
              <span>{{ t("set.outboxLabel") }}</span>
            </label>
            <input
              v-if="settingsData.notifications.outbox_enabled"
              v-model="settingsData.notifications.outbox_path"
              placeholder="/path/to/outbox.jsonl"
              class="w-full px-3 py-2 rounded-lg bg-surface border border-slate-700 text-white font-mono"
            />
          </div>
        </div>
      </div>
    </div>

    <!-- TAB 6: SECURITY & AUDIT -->
    <div v-if="activeTab === 'security'" class="space-y-6">
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <!-- Cryptographic Vault & 2FA -->
        <div class="rounded-2xl bg-surface border border-slate-800/80 p-5 space-y-4">
          <div class="flex items-center justify-between">
            <h3 class="text-sm font-bold text-white flex items-center gap-2">
              <ShieldCheck class="w-4 h-4 text-emerald-400" />
              <span>{{ t("set.vaultStatus") }}</span>
            </h3>
            <span
              class="px-2.5 py-0.5 rounded-full text-[10px] font-mono border"
              :class="authStore.user?.totp_enabled ? 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30' : 'bg-slate-700/40 text-slate-400 border-slate-700'"
            >
              {{ authStore.user?.totp_enabled ? t("set.totpEnabled") : t("set.totpDisabled") }}
            </span>
          </div>

          <div class="grid grid-cols-2 gap-3 text-xs font-mono">
            <div class="p-3 rounded-xl bg-background/60 border border-slate-800">
              <span class="text-slate-500 block text-[10px]">{{ t("set.cipher") }}</span>
              <span class="text-emerald-400 font-semibold">{{ settingsData.security.encryptionAlgorithm }}</span>
            </div>
            <div class="p-3 rounded-xl bg-background/60 border border-slate-800">
              <span class="text-slate-500 block text-[10px]">{{ t("set.pwHash") }}</span>
              <span class="text-emerald-400 font-semibold">{{ settingsData.security.passwordHashing }}</span>
            </div>
            <div class="p-3 rounded-xl bg-background/60 border border-slate-800">
              <span class="text-slate-500 block text-[10px]">{{ t("set.authProto") }}</span>
              <span class="text-slate-200">{{ settingsData.security.authMode }}</span>
            </div>
            <div class="p-3 rounded-xl bg-background/60 border border-slate-800">
              <span class="text-slate-500 block text-[10px]">{{ t("set.masterKeySrc") }}</span>
              <span class="text-accent">{{ settingsData.security.masterKeySource }}</span>
            </div>
          </div>

          <div class="pt-3 border-t border-slate-800/80 flex items-center justify-between">
            <div>
              <span class="text-xs font-semibold text-white block">{{ t("set.totpTitle") }}</span>
              <span class="text-[11px] text-slate-400">RFC 6238 TOTP (Google/Microsoft/1Password)</span>
            </div>
            <button
              v-if="!authStore.user?.totp_enabled"
              @click="startTotpSetup"
              class="px-3.5 py-1.5 rounded-xl bg-accent text-slate-950 font-semibold text-xs flex items-center gap-1.5"
            >
              <QrCode class="w-3.5 h-3.5" />
              <span>{{ t("set.totpEnableBtn") }}</span>
            </button>
            <button
              v-else
              @click="showDisableModal = true"
              class="px-3.5 py-1.5 rounded-xl bg-rose-500/15 text-rose-400 border border-rose-500/30 text-xs font-semibold"
            >
              {{ t("set.totpDisableBtn") }}
            </button>
          </div>

          <div>
            <label class="block text-xs text-slate-400 mb-1">{{ t("set.oldPw") }}</label>
            <input v-model="totpPassword" type="password" autocomplete="current-password"
              class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-white text-sm" />
          </div>

          <div class="pt-3 border-t border-slate-800/80">
            <label class="flex items-center justify-between cursor-pointer gap-3">
              <div>
                <span class="text-xs font-semibold text-white block">{{ t("set.publicToggleTitle") }}</span>
                <span class="text-[11px] text-slate-400">{{ t("set.publicToggleDesc") }}</span>
              </div>
              <input v-model="settingsData.public_monitoring_enabled" type="checkbox" class="rounded bg-background border-slate-600 text-accent w-4 h-4 shrink-0" />
            </label>
          </div>
        </div>

        <!-- Change Password Card -->
        <div class="rounded-2xl bg-surface border border-slate-800/80 p-5 space-y-4">
          <h3 class="text-sm font-bold text-white flex items-center gap-2">
            <Lock class="w-4 h-4 text-brand" />
            <span>{{ t("set.changePwTitle") }}</span>
          </h3>

          <div v-if="pwMsg" class="p-2.5 rounded-lg bg-emerald-500/15 border border-emerald-500/30 text-emerald-300 text-xs">
            {{ pwMsg }}
          </div>
          <div v-if="pwError" class="p-2.5 rounded-lg bg-rose-500/15 border border-rose-500/30 text-rose-300 text-xs">
            {{ pwError }}
          </div>

          <form @submit.prevent="changePassword" class="space-y-3 text-xs">
            <div>
              <label class="block text-slate-400 mb-1">{{ t("set.oldPw") }}</label>
              <input v-model="oldPassword" type="password" required autocomplete="current-password" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-white font-mono text-sm" />
            </div>
            <div>
              <label class="block text-slate-400 mb-1">{{ t("set.newPw") }}</label>
              <input v-model="newPassword" type="password" required minlength="8" autocomplete="new-password" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-white font-mono text-sm" />
            </div>
            <button
              type="submit"
              :disabled="changingPw"
              class="w-full py-2 rounded-xl bg-surface2 hover:bg-slate-700 text-slate-200 font-semibold text-xs transition-all"
            >
              {{ changingPw ? '...' : t("set.changePwBtn") }}
            </button>
          </form>
        </div>
      </div>

      <!-- Login Guards & IP Lockouts -->
      <div class="rounded-2xl bg-surface border border-slate-800/80 overflow-hidden">
        <div class="px-5 py-3.5 border-b border-slate-800/80 flex items-center justify-between">
          <h3 class="text-sm font-bold text-white flex items-center gap-2">
            <ShieldAlert class="w-4 h-4 text-amber-400" />
            <span>{{ t("set.secGuardsTitle") }}</span>
          </h3>
          <span class="text-xs font-mono text-slate-400">{{ securityGuards.length }} entries</span>
        </div>
        <div class="overflow-x-auto max-h-[260px] overflow-y-auto">
          <table class="w-full text-left border-collapse text-xs">
            <thead>
              <tr class="border-b border-slate-800 text-slate-400 font-mono uppercase">
                <th class="py-2 px-4">{{ t("set.colGuardKey") }}</th>
                <th class="py-2 px-4">{{ t("set.colFailCount") }}</th>
                <th class="py-2 px-4">{{ t("set.colLockStatus") }}</th>
                <th class="py-2 px-4">{{ t("set.colLastSeen") }}</th>
                <th class="py-2 px-4 text-right">ACTION</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-800/60 font-mono">
              <tr v-for="g in securityGuards" :key="g.id" class="hover:bg-slate-800/30">
                <td class="py-2 px-4 text-white font-semibold">{{ g.guardKey }}</td>
                <td class="py-2 px-4" :class="g.failCount >= 3 ? 'text-amber-400 font-bold' : 'text-slate-300'">{{ g.failCount }}</td>
                <td class="py-2 px-4">
                  <span v-if="g.lockRemainingSec > 0" class="px-2 py-0.5 rounded text-[10px] bg-rose-500/20 text-rose-400 border border-rose-500/30">
                    {{ t("set.activeLocked", { sec: g.lockRemainingSec }) }}
                  </span>
                  <span v-else class="text-slate-400">{{ t("set.normal") }}</span>
                </td>
                <td class="py-2 px-4 text-slate-400">{{ g.lastFailAt ? new Date(g.lastFailAt).toLocaleString(isZh ? "zh-CN" : "en-US") : '-' }}</td>
                <td class="py-2 px-4 text-right">
                  <button
                    v-if="g.failCount > 0 || g.lockRemainingSec > 0"
                    @click="unlockGuard(g.id)"
                    class="px-2 py-1 rounded bg-accent/15 hover:bg-accent/25 text-accent text-[11px] font-sans inline-flex items-center gap-1"
                  >
                    <Unlock class="w-3 h-3" />
                    <span>{{ t("set.unlock") }}</span>
                  </button>
                </td>
              </tr>
              <tr v-if="securityGuards.length === 0">
                <td colspan="5" class="py-4 text-center text-slate-500">{{ t("set.noGuards") }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Audit Log Table -->
      <div class="rounded-2xl bg-surface border border-slate-800/80 overflow-hidden">
        <div class="px-5 py-4 border-b border-slate-800/80 flex items-center justify-between">
          <h3 class="text-sm font-bold text-white flex items-center gap-2">
            <Lock class="w-4 h-4 text-brand" />
            <span>{{ t("set.auditTitle") }}</span>
          </h3>
          <span class="text-xs font-mono text-slate-400">{{ t("set.latestEvents", { n: auditLogs.length }) }}</span>
        </div>
        <div class="overflow-x-auto max-h-[350px] overflow-y-auto">
          <table class="w-full text-left border-collapse text-xs">
            <thead>
              <tr class="border-b border-slate-800 text-slate-400 font-mono uppercase">
                <th class="py-2.5 px-4">{{ t("set.colTime") }}</th>
                <th class="py-2.5 px-4">{{ t("set.colUser") }}</th>
                <th class="py-2.5 px-4">{{ t("set.colAction") }}</th>
                <th class="py-2.5 px-4">{{ t("set.colDetails") }}</th>
                <th class="py-2.5 px-4">{{ t("set.colIp") }}</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-800/60 font-mono">
              <tr v-for="log in auditLogs" :key="log.id" class="hover:bg-slate-800/30">
                <td class="py-2.5 px-4 text-slate-400 whitespace-nowrap">{{ new Date(log.createdAt).toLocaleString(isZh ? "zh-CN" : "en-US") }}</td>
                <td class="py-2.5 px-4 text-white">{{ log.username }}</td>
                <td class="py-2.5 px-4 text-accent">{{ log.action }}</td>
                <td class="py-2.5 px-4 text-slate-300 font-sans">{{ log.details }}</td>
                <td class="py-2.5 px-4 text-slate-500">{{ log.ipAddress || "-" }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- TOTP Enable Modal -->
    <div v-if="showTotpModal && totpSetupData" class="fixed inset-0 z-50 bg-black/75 backdrop-blur-sm flex items-center justify-center p-4">
      <div class="w-full max-w-md bg-surface border border-slate-800 rounded-2xl p-6 shadow-2xl space-y-4">
        <div class="flex items-center justify-between">
          <h3 class="text-base font-bold text-white flex items-center gap-2">
            <QrCode class="w-5 h-5 text-accent" />
            <span>{{ t("set.totpTitle") }}</span>
          </h3>
          <button @click="showTotpModal = false" class="text-slate-400 hover:text-white"><X class="w-5 h-5" /></button>
        </div>

        <div v-if="totpError" class="p-2.5 rounded-lg bg-rose-500/15 text-rose-300 text-xs">{{ totpError }}</div>

        <div class="space-y-3 text-xs text-slate-300">
          <p>{{ t("set.totpStep1") }}</p>
          <div class="flex justify-center p-4 bg-white rounded-xl">
            <img :src="totpSetupData.qrSvgDataUri" alt="TOTP QR" class="w-44 h-44" />
          </div>
          <p class="font-mono text-[11px] text-center text-slate-400 select-all">{{ t("set.totpSecret", { sec: totpSetupData.secret }) }}</p>

          <p class="pt-2">{{ t("set.totpStep2") }}</p>
          <input
            v-model="totpCodeInput"
            type="text"
            inputmode="numeric"
            maxlength="6"
            placeholder="123456"
            class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-center font-mono tracking-widest text-lg text-white"
          />
        </div>

        <div class="flex justify-end gap-2 pt-2">
          <button type="button" @click="showTotpModal = false" class="px-4 py-2 rounded-xl bg-surface2 text-slate-300 text-xs">{{ t("leads.cancel") }}</button>
          <button type="button" @click="verifyAndEnableTotp" class="px-4 py-2 rounded-xl bg-accent text-slate-950 font-semibold text-xs">{{ t("set.totpVerifyBtn") }}</button>
        </div>
      </div>
    </div>

    <!-- TOTP Disable Modal -->
    <div v-if="showDisableModal" class="fixed inset-0 z-50 bg-black/75 backdrop-blur-sm flex items-center justify-center p-4">
      <div class="w-full max-w-sm bg-surface border border-slate-800 rounded-2xl p-6 shadow-2xl space-y-4">
        <h3 class="text-base font-bold text-white">{{ t("set.totpDisableBtn") }}</h3>
        <div v-if="totpError" class="p-2.5 rounded-lg bg-rose-500/15 text-rose-300 text-xs">{{ totpError }}</div>
        <p class="text-xs text-slate-300">{{ t("set.totpStep2Disable") }}</p>
        <input
          v-model="totpDisableCode"
          type="text"
          inputmode="numeric"
          maxlength="6"
          placeholder="123456"
          class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-center font-mono tracking-widest text-lg text-white"
        />
        <div class="flex justify-end gap-2 pt-2">
          <button type="button" @click="showDisableModal = false" class="px-3.5 py-1.5 rounded-xl bg-surface2 text-slate-300 text-xs">{{ t("leads.cancel") }}</button>
          <button type="button" @click="confirmDisableTotp" class="px-3.5 py-1.5 rounded-xl bg-rose-500 text-white font-semibold text-xs">{{ t("set.totpDisableConfirm") }}</button>
        </div>
      </div>
    </div>

    <!-- Quick Add Account Modal -->
    <div v-if="showAddAcctModal" class="fixed inset-0 z-50 bg-black/75 backdrop-blur-sm flex items-center justify-center p-4">
      <div class="w-full max-w-lg bg-surface border border-slate-800 rounded-2xl p-6 shadow-2xl max-h-[90vh] overflow-y-auto">
        <div class="flex items-center justify-between mb-4">
          <h3 class="text-lg font-bold text-white flex items-center gap-2">
            <KeyRound class="w-5 h-5 text-accent" />
            <span>{{ t("accts.modalTitle") }}</span>
          </h3>
          <button @click="showAddAcctModal = false" class="text-slate-400 hover:text-white"><X class="w-5 h-5" /></button>
        </div>
        <form @submit.prevent="quickCreateAccount" class="space-y-4">
          <div v-if="acctError" class="p-3 rounded-xl bg-rose-500/10 text-rose-400 text-xs">{{ acctError }}</div>
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="block text-xs text-slate-400 mb-1">{{ t("accts.exchange") }}</label>
              <select v-model="acctForm.exchange" class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white">
                <option value="okx">OKX V5</option>
                <option value="binance">Binance USDT-M</option>
                <option value="bybit">Bybit V5</option>
                <option value="bitget">Bitget V2</option>
              </select>
            </div>
            <div>
              <label class="block text-xs text-slate-400 mb-1">{{ t("accts.environment") }}</label>
              <select v-model="acctForm.environment" class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white">
                <option value="demo">{{ t("accts.envDemo") }}</option>
                <option value="live">{{ t("accts.envLive") }}</option>
              </select>
            </div>
          </div>
          <div>
            <label class="block text-xs text-slate-400 mb-1">{{ t("accts.label") }}</label>
            <input v-model="acctForm.name" required :placeholder="t('accts.labelPlaceholder')" class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white" />
          </div>
          <div>
            <label class="block text-xs text-slate-400 mb-1">{{ t("accts.apiKey") }}</label>
            <input v-model="acctForm.apiKey" required class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm font-mono text-white" />
          </div>
          <div>
            <label class="block text-xs text-slate-400 mb-1">{{ t("accts.secretKey") }}</label>
            <input v-model="acctForm.secretKey" type="password" required class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm font-mono text-white" />
          </div>
          <div v-if="acctForm.exchange === 'okx' || acctForm.exchange === 'bitget'">
            <label class="block text-xs text-slate-400 mb-1">{{ t("accts.passphrase") }}</label>
            <input v-model="acctForm.passphrase" type="password" required class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm font-mono text-white" />
          </div>
          <div class="grid grid-cols-3 gap-3">
            <div>
              <label class="block text-xs text-slate-400 mb-1">{{ t("accts.marginMode") }}</label>
              <select v-model="acctForm.marginMode" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-xs text-white">
                <option value="cross">{{ t("accts.cross") }}</option>
                <option value="isolated">{{ t("accts.isolated") }}</option>
              </select>
            </div>
            <div>
              <label class="block text-xs text-slate-400 mb-1">{{ t("accts.posMode") }}</label>
              <select v-model="acctForm.positionMode" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-xs text-white">
                <option value="hedge">{{ t("accts.hedge") }}</option>
                <option value="one_way">{{ t("accts.oneWay") }}</option>
              </select>
            </div>
            <div>
              <label class="block text-xs text-slate-400 mb-1">{{ t("accts.proxy") }}</label>
              <select v-model="acctForm.proxyId" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-xs text-white">
                <option :value="null">{{ t("accts.direct") }}</option>
                <option v-for="p in proxies" :key="p.id" :value="p.id">{{ p.name }}</option>
              </select>
            </div>
          </div>
          <div class="flex justify-end gap-3 pt-2">
            <button type="button" @click="showAddAcctModal = false" class="px-4 py-2 rounded-xl bg-surface2 text-slate-300 text-xs font-medium">{{ t("leads.cancel") }}</button>
            <button type="submit" class="px-4 py-2 rounded-xl bg-accent text-slate-950 font-semibold text-xs">{{ t("accts.saveBtn") }}</button>
          </div>
        </form>
      </div>
    </div>
  </div>
</template>
