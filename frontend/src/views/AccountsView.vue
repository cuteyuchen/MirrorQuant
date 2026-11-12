<script setup lang="ts">
import { onActivated, onMounted, ref } from "vue";
import { CheckCircle2, KeyRound, Plus, RefreshCw, Shield, Trash2, Wifi, X } from "lucide-vue-next";
import { api } from "@/api/client";
import { useDataCacheStore } from "@/stores/dataCache";
import { useI18n } from "@/i18n";

defineOptions({ name: "AccountsView" });

const cache = useDataCacheStore();
const { t } = useI18n();

const accounts = ref<any[]>(cache.accounts?.data || []);
const proxies = ref<any[]>(cache.proxies?.data || []);
const loading = ref(!cache.accounts);
const testingId = ref<number | null>(null);
const syncingId = ref<number | null>(null);
const testResult = ref<Record<number, any>>({});

const showModal = ref(false);
const form = ref({
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
const formError = ref("");

async function loadData(force = false) {
  if (!cache.accounts) loading.value = true;
  try {
    const [aItems, pItems] = await Promise.all([
      cache.fetchAccounts(force, 15000),
      cache.fetchProxies(force, 20000),
    ]);
    accounts.value = aItems;
    proxies.value = pItems;
  } finally {
    loading.value = false;
  }
}

async function createAccount() {
  formError.value = "";
  try {
    await api.post("/api/accounts", form.value);
    showModal.value = false;
    form.value = {
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
    formError.value = e.message;
  }
}

async function testConnectivity(id: number) {
  testingId.value = id;
  try {
    const res = await api.post<any>(`/api/accounts/${id}/test`);
    testResult.value[id] = res;
    await loadData(true);
  } catch (e: any) {
    testResult.value[id] = { ok: false, error: e.message };
  } finally {
    testingId.value = null;
  }
}

async function syncAccount(id: number) {
  syncingId.value = id;
  try {
    await api.post(`/api/accounts/${id}/sync`);
    await loadData(true);
  } finally {
    syncingId.value = null;
  }
}

async function deleteAccount(id: number) {
  if (!confirm(t("accts.deleteConfirm"))) return;
  await api.delete(`/api/accounts/${id}`);
  await loadData(true);
}

function formatUsd(val: number) {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
  }).format(val || 0);
}

onMounted(() => loadData(false));
onActivated(() => {
  if (cache.accounts) accounts.value = cache.accounts.data;
  if (cache.proxies) proxies.value = cache.proxies.data;
  loadData(false);
});
</script>

<template>
  <div class="space-y-6">
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
      <div>
        <h2 class="text-xl sm:text-2xl font-bold text-white tracking-tight">{{ t("accts.title") }}</h2>
        <p class="text-xs sm:text-sm text-slate-400 mt-0.5">{{ t("accts.subtitle") }}</p>
      </div>
      <button
        @click="showModal = true"
        class="px-4 py-2.5 rounded-xl bg-gradient-to-r from-accent to-brand text-slate-950 font-semibold text-xs sm:text-sm flex items-center gap-2 hover:opacity-90 shadow-lg shadow-accent/20"
      >
        <Plus class="w-4 h-4" />
        <span>{{ t("accts.addBtn") }}</span>
      </button>
    </div>

    <div v-if="loading && accounts.length === 0" class="text-center py-12 text-slate-400 text-sm">
      {{ t("accts.loading") }}
    </div>

    <div v-else-if="accounts.length === 0" class="p-10 rounded-2xl bg-surface border border-slate-800 text-center">
      <p class="text-slate-300 font-medium mb-1">{{ t("accts.emptyTitle") }}</p>
      <p class="text-xs text-slate-500 mb-4">{{ t("accts.emptyDesc") }}</p>
      <button @click="showModal = true" class="px-4 py-2 rounded-xl bg-accent/15 text-accent border border-accent/30 text-xs font-semibold">
        {{ t("accts.addFirst") }}
      </button>
    </div>

    <div v-else class="grid grid-cols-1 md:grid-cols-2 gap-5">
      <div
        v-for="acct in accounts"
        :key="acct.id"
        class="rounded-2xl bg-surface border border-slate-800/80 p-5 flex flex-col justify-between space-y-4"
      >
        <div>
          <div class="flex items-start justify-between gap-3">
            <div class="flex items-center gap-3">
              <div class="w-10 h-10 rounded-xl bg-surface2 border border-slate-700 flex items-center justify-center font-bold text-xs uppercase text-accent">
                {{ acct.exchange }}
              </div>
              <div>
                <div class="flex items-center gap-2">
                  <h3 class="font-bold text-base text-white">{{ acct.name }}</h3>
                  <span
                    class="px-2 py-0.5 rounded-md text-[10px] font-mono uppercase border"
                    :class="
                      acct.environment === 'live'
                        ? 'bg-rose-500/15 text-rose-400 border-rose-500/30'
                        : 'bg-cyan-500/15 text-cyan-400 border-cyan-500/30'
                    "
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
              :class="
                acct.status === 'connected'
                  ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30'
                  : acct.status === 'error'
                  ? 'bg-rose-500/10 text-rose-400 border-rose-500/30'
                  : 'bg-slate-700/40 text-slate-400 border-slate-700'
              "
            >
              {{ acct.status.toUpperCase() }}
            </span>
          </div>

          <div class="grid grid-cols-3 gap-3 mt-4 p-3 rounded-xl bg-background/60 border border-slate-800/80 text-xs font-mono">
            <div>
              <span class="text-[10px] text-slate-500 block">{{ t("accts.usdtEquity") }}</span>
              <span class="font-bold text-white">{{ formatUsd(acct.usdtEquity) }}</span>
            </div>
            <div>
              <span class="text-[10px] text-slate-500 block">{{ t("accts.available") }}</span>
              <span class="text-slate-200">{{ formatUsd(acct.availEquity) }}</span>
            </div>
            <div>
              <span class="text-[10px] text-slate-500 block">{{ t("accts.unrealizedPnl") }}</span>
              <span :class="acct.unrealizedPnl >= 0 ? 'text-emerald-400' : 'text-rose-400'">
                {{ acct.unrealizedPnl >= 0 ? "+" : "" }}{{ formatUsd(acct.unrealizedPnl) }}
              </span>
            </div>
          </div>

          <div v-if="testResult[acct.id]" class="mt-3 p-2.5 rounded-lg text-xs font-mono border"
            :class="testResult[acct.id].ok ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-300' : 'bg-rose-500/10 border-rose-500/30 text-rose-300'"
          >
            <div v-if="testResult[acct.id].ok" class="flex items-center gap-1.5">
              <CheckCircle2 class="w-3.5 h-3.5" />
              <span>{{ t("accts.connectedMsg", { mode: testResult[acct.id].posMode, eq: formatUsd(testResult[acct.id].usdtEquity) }) }}</span>
            </div>
            <div v-else>Error: {{ testResult[acct.id].error }}</div>
          </div>

          <p v-if="acct.lastError && !testResult[acct.id]" class="mt-2 text-[11px] text-rose-400 break-words">
            {{ acct.lastError }}
          </p>
        </div>

        <div class="flex items-center justify-between pt-3 border-t border-slate-800/80">
          <div class="flex items-center gap-2">
            <button
              @click="testConnectivity(acct.id)"
              :disabled="testingId === acct.id"
              class="px-3 py-1.5 rounded-lg bg-accent/15 hover:bg-accent/25 text-accent text-xs font-medium flex items-center gap-1.5"
            >
              <Wifi class="w-3.5 h-3.5" />
              <span>{{ testingId === acct.id ? t("accts.testing") : t("accts.testApi") }}</span>
            </button>
            <button
              @click="syncAccount(acct.id)"
              :disabled="syncingId === acct.id"
              class="px-3 py-1.5 rounded-lg bg-surface2 hover:bg-slate-700 text-slate-200 text-xs font-medium flex items-center gap-1.5"
            >
              <RefreshCw class="w-3.5 h-3.5" :class="{ 'animate-spin': syncingId === acct.id }" />
              <span>{{ t("accts.sync") }}</span>
            </button>
          </div>
          <button
            @click="deleteAccount(acct.id)"
            class="p-1.5 rounded-lg text-slate-500 hover:text-rose-400 hover:bg-rose-500/10"
          >
            <Trash2 class="w-4 h-4" />
          </button>
        </div>
      </div>
    </div>

    <!-- Add Account Modal -->
    <div v-if="showModal" class="fixed inset-0 z-50 bg-black/75 backdrop-blur-sm flex items-center justify-center p-4">
      <div class="w-full max-w-lg bg-surface border border-slate-800 rounded-2xl p-6 shadow-2xl">
        <div class="flex items-center justify-between mb-5">
          <h3 class="text-lg font-bold text-white flex items-center gap-2">
            <KeyRound class="w-5 h-5 text-accent" />
            <span>{{ t("accts.modalTitle") }}</span>
          </h3>
          <button @click="showModal = false" class="text-slate-400 hover:text-white"><X class="w-5 h-5" /></button>
        </div>
        <form @submit.prevent="createAccount" class="space-y-4">
          <div v-if="formError" class="p-3 rounded-xl bg-rose-500/10 text-rose-400 text-xs">{{ formError }}</div>
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="block text-xs text-slate-400 mb-1">{{ t("accts.exchange") }}</label>
              <select v-model="form.exchange" class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white">
                <option value="okx">OKX V5</option>
                <option value="binance">Binance USDT-M</option>
                <option value="bybit">Bybit V5</option>
                <option value="bitget">Bitget V2</option>
              </select>
            </div>
            <div>
              <label class="block text-xs text-slate-400 mb-1">{{ t("accts.environment") }}</label>
              <select v-model="form.environment" class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white">
                <option value="demo">{{ t("accts.envDemo") }}</option>
                <option value="live">{{ t("accts.envLive") }}</option>
              </select>
            </div>
          </div>
          <div>
            <label class="block text-xs text-slate-400 mb-1">{{ t("accts.label") }}</label>
            <input v-model="form.name" required :placeholder="t('accts.labelPlaceholder')" class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white" />
          </div>
          <div>
            <label class="block text-xs text-slate-400 mb-1">{{ t("accts.apiKey") }}</label>
            <input v-model="form.apiKey" required class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm font-mono text-white" />
          </div>
          <div>
            <label class="block text-xs text-slate-400 mb-1">{{ t("accts.secretKey") }}</label>
            <input v-model="form.secretKey" type="password" required class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm font-mono text-white" />
          </div>
          <div v-if="form.exchange === 'okx' || form.exchange === 'bitget'">
            <label class="block text-xs text-slate-400 mb-1">{{ t("accts.passphrase") }}</label>
            <input v-model="form.passphrase" type="password" required class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm font-mono text-white" />
          </div>
          <div class="grid grid-cols-3 gap-3">
            <div>
              <label class="block text-xs text-slate-400 mb-1">{{ t("accts.marginMode") }}</label>
              <select v-model="form.marginMode" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-xs text-white">
                <option value="cross">{{ t("accts.cross") }}</option>
                <option value="isolated">{{ t("accts.isolated") }}</option>
              </select>
            </div>
            <div>
              <label class="block text-xs text-slate-400 mb-1">{{ t("accts.posMode") }}</label>
              <select v-model="form.positionMode" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-xs text-white">
                <option value="hedge">{{ t("accts.hedge") }}</option>
                <option value="one_way">{{ t("accts.oneWay") }}</option>
              </select>
            </div>
            <div>
              <label class="block text-xs text-slate-400 mb-1">{{ t("accts.proxy") }}</label>
              <select v-model="form.proxyId" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-xs text-white">
                <option :value="null">{{ t("accts.direct") }}</option>
                <option v-for="p in proxies" :key="p.id" :value="p.id">{{ p.name }}</option>
              </select>
            </div>
          </div>
          <div class="flex justify-end gap-3 pt-2">
            <button type="button" @click="showModal = false" class="px-4 py-2 rounded-xl bg-surface2 text-slate-300 text-xs font-medium">{{ t("leads.cancel") }}</button>
            <button type="submit" class="px-4 py-2 rounded-xl bg-accent text-slate-950 font-semibold text-xs">{{ t("accts.saveBtn") }}</button>
          </div>
        </form>
      </div>
    </div>
  </div>
</template>
