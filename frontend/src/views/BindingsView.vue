<script setup lang="ts">
import { onActivated, onMounted, ref } from "vue";
import { AlertTriangle, ArrowRight, Layers, Plus, Power, RotateCcw, Settings2, Trash2, X } from "lucide-vue-next";
import { api } from "@/api/client";
import { useDataCacheStore } from "@/stores/dataCache";
import { useI18n } from "@/i18n";

defineOptions({ name: "BindingsView" });

const cache = useDataCacheStore();
const { t } = useI18n();

const bindings = ref<any[]>(cache.bindings?.data.items || []);
const leads = ref<any[]>(cache.leads?.data || []);
const accounts = ref<any[]>(cache.accounts?.data || []);
const globalKillSwitch = ref(cache.bindings?.data.globalKillSwitch || false);
const loading = ref(!cache.bindings);

const showModal = ref(false);
const editingId = ref<number | null>(null);

function defaultForm() {
  const rd = cache.settings?.data?.risk_defaults || {};
  return {
    name: "",
    leadId: null as number | null,
    accountId: null as number | null,
    riskScale: 1.0,
    maxTotalLeverage: rd.max_total_leverage ?? 8.0,
    maxSingleLeverage: rd.max_single_leverage ?? 4.0,
    maxInstrumentLeverage: rd.max_instrument_leverage ?? 20,
    maxDrawdownPct: rd.max_drawdown_pct ?? 30.0,
    dailyMaxLossPct: rd.daily_max_loss_pct ?? 10.0,
    maxSlippagePct: rd.max_slippage_pct ?? 1.0,
    maxSpreadPct: rd.max_spread_pct ?? 0.3,
    iocOffsetPct: rd.ioc_offset_pct ?? 0.5,
    orderMode: rd.order_mode ?? "ioc_then_market",
    tpSlMode: rd.tp_sl_mode ?? "follow_lead",
    enableStopLoss: rd.enable_stop_loss ?? false,
    stopLossPct: rd.stop_loss_pct ?? 5.0,
    enableTakeProfit: rd.enable_take_profit ?? false,
    takeProfitPct: rd.take_profit_pct ?? 10.0,
    enableTrailingStop: rd.enable_trailing_stop ?? false,
    trailingCallbackPct: rd.trailing_callback_pct ?? 2.0,
    hardStopLossPct: rd.hard_stop_loss_pct ?? 15.0,
  };
}

const form = ref(defaultForm());
const formError = ref("");

async function loadData(force = false) {
  if (!cache.bindings) loading.value = true;
  try {
    const [bRes, lItems, aItems] = await Promise.all([
      cache.fetchBindings(force, 12000),
      cache.fetchLeads(force, 12000),
      cache.fetchAccounts(force, 15000),
      cache.fetchSettings(false, 20000),
    ]);
    bindings.value = bRes.items;
    globalKillSwitch.value = bRes.globalKillSwitch;
    leads.value = lItems;
    accounts.value = aItems;
  } finally {
    loading.value = false;
  }
}

function openCreate() {
  editingId.value = null;
  form.value = defaultForm();
  formError.value = "";
  showModal.value = true;
}

function openEdit(b: any) {
  editingId.value = b.id;
  formError.value = "";
  form.value = {
    name: b.name,
    leadId: b.leadId,
    accountId: b.accountId,
    riskScale: b.riskScale,
    maxTotalLeverage: b.maxTotalLeverage,
    maxSingleLeverage: b.maxSingleLeverage,
    maxInstrumentLeverage: b.maxInstrumentLeverage,
    maxDrawdownPct: b.maxDrawdownPct,
    dailyMaxLossPct: b.dailyMaxLossPct ?? 10.0,
    maxSlippagePct: b.maxSlippagePct,
    maxSpreadPct: b.maxSpreadPct ?? 0.3,
    iocOffsetPct: b.iocOffsetPct,
    orderMode: b.orderMode,
    tpSlMode: b.tpSlMode ?? "follow_lead",
    enableStopLoss: !!b.enableStopLoss,
    stopLossPct: b.stopLossPct ?? 5.0,
    enableTakeProfit: !!b.enableTakeProfit,
    takeProfitPct: b.takeProfitPct ?? 10.0,
    enableTrailingStop: !!b.enableTrailingStop,
    trailingCallbackPct: b.trailingCallbackPct ?? 2.0,
    hardStopLossPct: b.hardStopLossPct ?? 15.0,
  };
  showModal.value = true;
}

async function saveBinding() {
  formError.value = "";
  if (!editingId.value && (!form.value.leadId || !form.value.accountId)) {
    formError.value = t("bind.errSelectBoth");
    return;
  }
  try {
    if (editingId.value) {
      await api.put(`/api/bindings/${editingId.value}`, {
        name: form.value.name,
        risk_scale: form.value.riskScale,
        max_total_leverage: form.value.maxTotalLeverage,
        max_single_leverage: form.value.maxSingleLeverage,
        max_instrument_leverage: form.value.maxInstrumentLeverage,
        max_drawdown_pct: form.value.maxDrawdownPct,
        daily_max_loss_pct: form.value.dailyMaxLossPct,
        max_slippage_pct: form.value.maxSlippagePct,
        max_spread_pct: form.value.maxSpreadPct,
        ioc_offset_pct: form.value.iocOffsetPct,
        order_mode: form.value.orderMode,
        tp_sl_mode: form.value.tpSlMode,
        enable_stop_loss: form.value.enableStopLoss,
        stop_loss_pct: form.value.stopLossPct,
        enable_take_profit: form.value.enableTakeProfit,
        take_profit_pct: form.value.takeProfitPct,
        enable_trailing_stop: form.value.enableTrailingStop,
        trailing_callback_pct: form.value.trailingCallbackPct,
        hard_stop_loss_pct: form.value.hardStopLossPct,
      });
    } else {
      await api.post("/api/bindings", form.value);
    }
    showModal.value = false;
    await loadData(true);
  } catch (e: any) {
    formError.value = e.message;
  }
}

async function toggleBinding(id: number) {
  await api.post(`/api/bindings/${id}/toggle`);
  await loadData(true);
}

async function resetBreaker(id: number) {
  await api.post(`/api/bindings/${id}/reset-breaker`);
  await loadData(true);
}

async function resetDebt(id: number) {
  await api.post(`/api/bindings/${id}/reset-debt`);
  await loadData(true);
}

async function deleteBinding(id: number) {
  if (!confirm(t("bind.deleteConfirm"))) return;
  await api.delete(`/api/bindings/${id}`);
  await loadData(true);
}

function tpSlLabel(mode: string): string {
  if (mode === "independent") return t("bind.tpslIndependent");
  if (mode === "hybrid_guard") return t("bind.tpslHybrid");
  return t("bind.tpslFollowLead");
}

onMounted(() => loadData(false));
onActivated(() => {
  if (cache.bindings) {
    bindings.value = cache.bindings.data.items;
    globalKillSwitch.value = cache.bindings.data.globalKillSwitch;
  }
  if (cache.leads) leads.value = cache.leads.data;
  if (cache.accounts) accounts.value = cache.accounts.data;
  loadData(false);
});
</script>

<template>
  <div class="space-y-6">
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
      <div>
        <h2 class="text-xl sm:text-2xl font-bold text-white tracking-tight">{{ t("bind.title") }}</h2>
        <p class="text-xs sm:text-sm text-slate-400 mt-0.5">{{ t("bind.subtitle") }}</p>
      </div>
      <button
        @click="openCreate"
        class="px-4 py-2.5 rounded-xl bg-gradient-to-r from-accent to-brand text-slate-950 font-semibold text-xs sm:text-sm flex items-center gap-2 hover:opacity-90 shadow-lg shadow-accent/20"
      >
        <Plus class="w-4 h-4" />
        <span>{{ t("bind.newBtn") }}</span>
      </button>
    </div>

    <div v-if="loading && bindings.length === 0" class="text-center py-12 text-slate-400 text-sm">
      {{ t("bind.loading") }}
    </div>

    <div v-else-if="bindings.length === 0" class="p-10 rounded-2xl bg-surface border border-slate-800 text-center">
      <p class="text-slate-300 font-medium mb-1">{{ t("bind.emptyTitle") }}</p>
      <p class="text-xs text-slate-500 mb-4">{{ t("bind.emptyDesc") }}</p>
      <button @click="openCreate" class="px-4 py-2 rounded-xl bg-accent/15 text-accent border border-accent/30 text-xs font-semibold">
        {{ t("bind.createFirst") }}
      </button>
    </div>

    <div v-else class="space-y-4">
      <div
        v-for="b in bindings"
        :key="b.id"
        class="rounded-2xl bg-surface border p-5 transition-all"
        :class="b.circuitBreakerTripped ? 'border-rose-500/50 bg-rose-950/10' : 'border-slate-800/80'"
      >
        <div class="flex flex-col lg:flex-row lg:items-center justify-between gap-4">
          <div class="space-y-1.5">
            <div class="flex flex-wrap items-center gap-2.5">
              <Layers class="w-4 h-4 text-accent" />
              <h3 class="font-bold text-base text-white">{{ b.name }}</h3>
              <span
                class="px-2 py-0.5 rounded-full text-[10px] font-mono border"
                :class="
                  b.isEnabled
                    ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30'
                    : 'bg-slate-700/40 text-slate-400 border-slate-700'
                "
              >
                {{ b.isEnabled ? t("bind.active") : t("bind.paused") }}
              </span>
              <span v-if="b.baselineSeeded" class="px-2 py-0.5 rounded bg-slate-800 text-slate-400 text-[10px] font-mono">
                {{ t("bind.baselineSeeded") }}
              </span>
              <span class="px-2 py-0.5 rounded bg-accent/10 text-accent border border-accent/25 text-[10px] font-mono">
                {{ tpSlLabel(b.tpSlMode || 'follow_lead') }}
              </span>
            </div>

            <div class="flex flex-wrap items-center gap-2 text-xs font-mono text-slate-300">
              <span class="px-2.5 py-1 rounded-lg bg-background border border-slate-800">{{ b.leadName }}</span>
              <ArrowRight class="w-3.5 h-3.5 text-accent" />
              <span class="px-2.5 py-1 rounded-lg bg-background border border-slate-800">
                {{ b.accountName }} ({{ b.accountExchange?.toUpperCase() }} {{ b.accountEnvironment }})
              </span>
            </div>
          </div>

          <div class="flex flex-wrap items-center gap-2">
            <button
              @click="openEdit(b)"
              class="px-3 py-1.5 rounded-lg bg-surface2 hover:bg-slate-700 text-xs text-slate-200 flex items-center gap-1.5"
            >
              <Settings2 class="w-3.5 h-3.5 text-accent" />
              <span>{{ t("bind.editBtn") }}</span>
            </button>

            <button
              @click="toggleBinding(b.id)"
              class="px-3 py-1.5 rounded-lg text-xs font-medium flex items-center gap-1.5 border transition-colors"
              :class="
                b.isEnabled
                  ? 'bg-amber-500/10 border-amber-500/30 text-amber-300 hover:bg-amber-500/20'
                  : 'bg-emerald-500/10 border-emerald-500/30 text-emerald-300 hover:bg-emerald-500/20'
              "
            >
              <Power class="w-3.5 h-3.5" />
              <span>{{ b.isEnabled ? t("bind.pause") : t("bind.enable") }}</span>
            </button>

            <button
              v-if="b.pendingDebtUsd > 0"
              @click="resetDebt(b.id)"
              class="px-3 py-1.5 rounded-lg bg-surface2 hover:bg-slate-700 text-xs text-amber-300 flex items-center gap-1.5"
            >
              <RotateCcw class="w-3.5 h-3.5" />
              <span>{{ t("bind.clearDebt", { amt: b.pendingDebtUsd }) }}</span>
            </button>

            <button
              v-if="b.circuitBreakerTripped"
              @click="resetBreaker(b.id)"
              class="px-3 py-1.5 rounded-lg bg-rose-500 text-white text-xs font-semibold flex items-center gap-1.5 hover:bg-rose-600"
            >
              <AlertTriangle class="w-3.5 h-3.5" />
              <span>{{ t("bind.resetBreaker") }}</span>
            </button>

            <button @click="deleteBinding(b.id)" class="p-2 rounded-lg text-slate-500 hover:text-rose-400 hover:bg-rose-500/10">
              <Trash2 class="w-4 h-4" />
            </button>
          </div>
        </div>

        <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-7 gap-3 mt-4 p-3 rounded-xl bg-background/60 border border-slate-800/80 text-xs font-mono">
          <div>
            <span class="text-[10px] text-slate-500 block">{{ t("bind.riskScale") }}</span>
            <span class="text-accent font-bold">{{ b.riskScale }}x</span>
          </div>
          <div>
            <span class="text-[10px] text-slate-500 block">{{ t("bind.maxLev") }}</span>
            <span class="text-slate-200">{{ b.maxSingleLeverage }}x / {{ b.maxTotalLeverage }}x</span>
          </div>
          <div>
            <span class="text-[10px] text-slate-500 block">{{ t("bind.drawdownBreaker") }}</span>
            <span class="text-slate-200">{{ b.maxDrawdownPct }}% / {{ b.dailyMaxLossPct ?? 10 }}%</span>
          </div>
          <div>
            <span class="text-[10px] text-slate-500 block">{{ t("bind.slippageGuard") }}</span>
            <span class="text-slate-200">{{ b.maxSlippagePct }}% / {{ b.maxSpreadPct ?? 0.3 }}%</span>
          </div>
          <div>
            <span class="text-[10px] text-slate-500 block">{{ t("bind.orderPolicy") }}</span>
            <span class="text-slate-200 uppercase">{{ b.orderMode }}</span>
          </div>
          <div>
            <span class="text-[10px] text-slate-500 block">{{ t("bind.tpSlShort") }}</span>
            <span class="text-slate-200">
              <template v-if="b.tpSlMode === 'follow_lead'">{{ t("bind.tpslFollowShort") }}</template>
              <template v-else-if="b.tpSlMode === 'hybrid_guard'">SL -{{ b.hardStopLossPct }}%</template>
              <template v-else>{{ b.enableTakeProfit ? `+${b.takeProfitPct}%` : '-' }} / {{ b.enableStopLoss ? `-${b.stopLossPct}%` : '-' }}</template>
            </span>
          </div>
          <div>
            <span class="text-[10px] text-slate-500 block">{{ t("bind.pendingDebt") }}</span>
            <span :class="b.pendingDebtUsd > 0 ? 'text-amber-400' : 'text-slate-400'">${{ b.pendingDebtUsd }}</span>
          </div>
        </div>

        <div v-if="b.circuitBreakerTripped" class="mt-3 p-2.5 rounded-lg bg-rose-500/15 border border-rose-500/30 text-xs text-rose-300 flex items-center gap-2">
          <AlertTriangle class="w-4 h-4 shrink-0" />
          <span>{{ t("bind.breakerBanner", { reason: b.circuitBreakerReason }) }}</span>
        </div>
      </div>
    </div>

    <!-- Modal -->
    <div v-if="showModal" class="fixed inset-0 z-50 bg-black/75 backdrop-blur-sm flex items-center justify-center p-4">
      <div class="w-full max-w-2xl bg-surface border border-slate-800 rounded-2xl p-6 shadow-2xl max-h-[90vh] overflow-y-auto">
        <div class="flex items-center justify-between mb-5">
          <h3 class="text-lg font-bold text-white">{{ editingId ? t("bind.editModalTitle") : t("bind.modalTitle") }}</h3>
          <button @click="showModal = false" class="text-slate-400 hover:text-white"><X class="w-5 h-5" /></button>
        </div>
        <form @submit.prevent="saveBinding" class="space-y-4">
          <div v-if="formError" class="p-3 rounded-xl bg-rose-500/10 text-rose-400 text-xs">{{ formError }}</div>
          <div>
            <label class="block text-xs text-slate-400 mb-1">{{ t("bind.label") }}</label>
            <input v-model="form.name" required :placeholder="t('bind.labelPlaceholder')" class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white" />
          </div>
          <div v-if="!editingId" class="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label class="block text-xs text-slate-400 mb-1">{{ t("bind.leadSource") }}</label>
              <select v-model="form.leadId" required class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white">
                <option :value="null" disabled>{{ t("bind.selectLead") }}</option>
                <option v-for="l in leads" :key="l.id" :value="l.id">{{ l.name }}</option>
              </select>
            </div>
            <div>
              <label class="block text-xs text-slate-400 mb-1">{{ t("bind.followerAccount") }}</label>
              <select v-model="form.accountId" required class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white">
                <option :value="null" disabled>{{ t("bind.selectAccount") }}</option>
                <option v-for="a in accounts" :key="a.id" :value="a.id">{{ a.name }} ({{ a.exchange }} {{ a.environment }})</option>
              </select>
            </div>
          </div>

          <div class="grid grid-cols-2 sm:grid-cols-3 gap-3">
            <div>
              <label class="block text-xs text-slate-400 mb-1">{{ t("bind.riskScaleInput") }}</label>
              <input v-model.number="form.riskScale" type="number" step="0.1" min="0.1" max="20" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white" />
            </div>
            <div>
              <label class="block text-xs text-slate-400 mb-1">{{ t("bind.maxSingleLev") }}</label>
              <input v-model.number="form.maxSingleLeverage" type="number" step="0.5" min="1" max="100" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white" />
            </div>
            <div>
              <label class="block text-xs text-slate-400 mb-1">{{ t("bind.maxTotalLev") }}</label>
              <input v-model.number="form.maxTotalLeverage" type="number" step="0.5" min="1" max="100" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white" />
            </div>
            <div>
              <label class="block text-xs text-slate-400 mb-1">{{ t("bind.maxDrawdown") }}</label>
              <input v-model.number="form.maxDrawdownPct" type="number" step="1" min="1" max="100" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white" />
            </div>
            <div>
              <label class="block text-xs text-slate-400 mb-1">{{ t("bind.dailyMaxLoss") }}</label>
              <input v-model.number="form.dailyMaxLossPct" type="number" step="0.5" min="1" max="100" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white" />
            </div>
            <div>
              <label class="block text-xs text-slate-400 mb-1">{{ t("bind.maxSlippage") }}</label>
              <input v-model.number="form.maxSlippagePct" type="number" step="0.1" min="0.1" max="50" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white" />
            </div>
            <div>
              <label class="block text-xs text-slate-400 mb-1">{{ t("bind.maxSpread") }}</label>
              <input v-model.number="form.maxSpreadPct" type="number" step="0.05" min="0.05" max="10" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white" />
            </div>
            <div>
              <label class="block text-xs text-slate-400 mb-1">{{ t("bind.iocOffset") }}</label>
              <input v-model.number="form.iocOffsetPct" type="number" step="0.05" min="0" max="10" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white" />
            </div>
            <div>
              <label class="block text-xs text-slate-400 mb-1">{{ t("bind.orderMode") }}</label>
              <select v-model="form.orderMode" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-xs text-white">
                <option value="ioc_then_market">{{ t("bind.modeIocMarket") }}</option>
                <option value="ioc_only">{{ t("bind.modeIocOnly") }}</option>
                <option value="market_only">{{ t("bind.modeMarketOnly") }}</option>
              </select>
            </div>
          </div>

          <div class="p-3.5 rounded-xl bg-background/70 border border-slate-800 space-y-3">
            <div>
              <label class="block text-xs font-semibold text-white mb-1">{{ t("bind.tpSlModeLabel") }}</label>
              <select v-model="form.tpSlMode" class="w-full px-3 py-2 rounded-xl bg-surface border border-slate-700 text-xs text-white">
                <option value="follow_lead">{{ t("bind.tpslFollowLeadDesc") }}</option>
                <option value="independent">{{ t("bind.tpslIndependentDesc") }}</option>
                <option value="hybrid_guard">{{ t("bind.tpslHybridDesc") }}</option>
              </select>
            </div>

            <div v-if="form.tpSlMode === 'hybrid_guard'" class="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label class="block text-xs text-slate-400 mb-1">{{ t("bind.hardSlPct") }}</label>
                <input v-model.number="form.hardStopLossPct" type="number" step="0.5" min="1" max="90" class="w-full px-3 py-2 rounded-lg bg-surface border border-slate-700 text-xs text-white" />
              </div>
              <div>
                <label class="flex items-center gap-2 text-xs text-slate-300 mb-1 cursor-pointer">
                  <input v-model="form.enableTakeProfit" type="checkbox" class="rounded bg-surface border-slate-600 text-accent" />
                  <span>{{ t("bind.tpPct") }}</span>
                </label>
                <input v-model.number="form.takeProfitPct" :disabled="!form.enableTakeProfit" type="number" step="0.5" min="0.5" max="500" class="w-full px-3 py-2 rounded-lg bg-surface border border-slate-700 text-xs text-white disabled:opacity-40" />
              </div>
            </div>

            <div v-if="form.tpSlMode === 'independent'" class="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <div>
                <label class="flex items-center gap-2 text-xs text-slate-300 mb-1 cursor-pointer">
                  <input v-model="form.enableStopLoss" type="checkbox" class="rounded bg-surface border-slate-600 text-accent" />
                  <span>{{ t("bind.slPct") }}</span>
                </label>
                <input v-model.number="form.stopLossPct" :disabled="!form.enableStopLoss" type="number" step="0.5" min="0.5" max="95" class="w-full px-3 py-2 rounded-lg bg-surface border border-slate-700 text-xs text-white disabled:opacity-40" />
              </div>
              <div>
                <label class="flex items-center gap-2 text-xs text-slate-300 mb-1 cursor-pointer">
                  <input v-model="form.enableTakeProfit" type="checkbox" class="rounded bg-surface border-slate-600 text-accent" />
                  <span>{{ t("bind.tpPct") }}</span>
                </label>
                <input v-model.number="form.takeProfitPct" :disabled="!form.enableTakeProfit" type="number" step="0.5" min="0.5" max="500" class="w-full px-3 py-2 rounded-lg bg-surface border border-slate-700 text-xs text-white disabled:opacity-40" />
              </div>
              <div>
                <label class="flex items-center gap-2 text-xs text-slate-300 mb-1 cursor-pointer">
                  <input v-model="form.enableTrailingStop" type="checkbox" class="rounded bg-surface border-slate-600 text-accent" />
                  <span>{{ t("bind.trailingPct") }}</span>
                </label>
                <input v-model.number="form.trailingCallbackPct" :disabled="!form.enableTrailingStop" type="number" step="0.2" min="0.2" max="50" class="w-full px-3 py-2 rounded-lg bg-surface border border-slate-700 text-xs text-white disabled:opacity-40" />
              </div>
            </div>
          </div>

          <div class="flex justify-end gap-3 pt-2">
            <button type="button" @click="showModal = false" class="px-4 py-2 rounded-xl bg-surface2 text-slate-300 text-xs font-medium">{{ t("leads.cancel") }}</button>
            <button type="submit" class="px-4 py-2 rounded-xl bg-accent text-slate-950 font-semibold text-xs">{{ editingId ? t("bind.saveEditBtn") : t("bind.createBtn") }}</button>
          </div>
        </form>
      </div>
    </div>
  </div>
</template>
