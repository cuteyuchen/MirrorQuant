<script setup lang="ts">
import { computed, nextTick, onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from "vue";
import {
  Activity,
  AlertOctagon,
  ArrowDownRight,
  ArrowUpRight,
  Calculator,
  RefreshCw,
  ShieldAlert,
  TrendingUp,
  Wallet,
  Globe,
} from "lucide-vue-next";
import { api } from "@/api/client";
import { useSocketStore } from "@/stores/socket";
import { useDataCacheStore } from "@/stores/dataCache";
import { useI18n } from "@/i18n";
import PositionCalculatorCard from "@/components/PositionCalculatorCard.vue";
import { formatPrice, formatQty, formatUsd, formatPct } from "@/utils/format";
import { getLeadAlignment } from "@/utils/alignment";

defineOptions({ name: "DashboardView" });

const socketStore = useSocketStore();
const cache = useDataCacheStore();
const { t, isZh } = useI18n();

const showLeadCalculator = ref(false);

const loading = ref(!cache.overview);
const data = ref<any>(
  cache.overview?.data || {
    kpis: {
      totalEquity: 0,
      availEquity: 0,
      totalNotional: 0,
      totalUnrealizedPnl: 0,
      actualLeverage: 0,
      drawdownPct: 0,
      activeLeads: 0,
      totalLeads: 0,
      activeAccounts: 0,
      totalAccounts: 0,
      activeBindings: 0,
      globalKillSwitch: false,
    },
    leadsStatus: [],
    accountsStatus: [],
    bindingsStatus: [],
    leadPositions: [],
    followerPositions: [],
    recentExecutions: [],
  }
);

let pollTimer: any = null;

async function fetchOverview(force = false) {
  if (!cache.overview) loading.value = true;
  try {
    data.value = await cache.fetchOverview(force, 6000);
  } catch (e) {
    console.error("Failed to load overview", e);
  } finally {
    loading.value = false;
  }
}

async function toggleKillSwitch() {
  const nextState = !data.value.kpis.globalKillSwitch;
  try {
    const res = await api.post<{ globalKillSwitch: boolean }>("/api/bindings/kill-switch", {
      enabled: nextState,
    });
    data.value.kpis.globalKillSwitch = res.globalKillSwitch;
    await fetchOverview(true);
  } catch (e) {
    console.error("Kill switch toggle failed", e);
  }
}

watch(
  () => socketStore.recentEvents.length,
  () => {
    fetchOverview(true);
  }
);

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => fetchOverview(true), 6000);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

onMounted(() => {
  fetchOverview(false);
  startPolling();
});

onActivated(() => {
  if (cache.overview) data.value = cache.overview.data;
  fetchOverview(false);
  startPolling();
});

onDeactivated(stopPolling);
onUnmounted(stopPolling);

const calculatorLeadId = ref<number | null>(null);
const calculatorSection = ref<HTMLElement | null>(null);
const leadsStatus = computed<any[]>(() => data.value.leadsStatus || []);
const calculatorLead = computed(() => leadsStatus.value.find(l => l.id === calculatorLeadId.value)
  || leadsStatus.value.find(l => l.isEnabled && l.openPositionsCount > 0) || leadsStatus.value[0]);
const calculatorPositions = computed(() => data.value.leadPositions.filter((p: any) => p.leadId === calculatorLead.value?.id));

async function openCalculator(leadId?: number) {
  calculatorLeadId.value = leadId ?? calculatorLead.value?.id ?? null;
  showLeadCalculator.value = true;
  await nextTick();
  calculatorSection.value?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function signedUsd(value: number | string | null | undefined) {
  return (Number(value) > 0 ? "+" : "") + formatUsd(value);
}

function syncState(lead: any) {
  if (!lead.isEnabled) return "paused";
  if (lead.status === "error" || lead.lastError) return "error";
  if (!lead.lastSyncAt) return "pending";
  return lead.stale ? "stale" : "healthy";
}

function syncTime(value?: string) {
  if (!value) return "—";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "—";
  return date.toLocaleString(isZh.value ? "zh-CN" : "en-US", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
}

function statusBadge(status: string) {
  if (status === "EXECUTED") return "bg-emerald-500/15 text-emerald-400 border-emerald-500/30";
  if (status.startsWith("BLOCKED")) return "bg-rose-500/15 text-rose-400 border-rose-500/30";
  if (status === "ACCUMULATED_DEBT") return "bg-amber-500/15 text-amber-400 border-amber-500/30";
  if (status === "FAILED") return "bg-rose-600/20 text-rose-300 border-rose-500/40";
  return "bg-slate-700/40 text-slate-300 border-slate-600/40";
}
</script>

<template>
  <div class="space-y-6">
    <!-- Header & Kill Switch -->
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
      <div>
        <h2 class="text-xl font-bold text-white tracking-tight">{{ t("dash.title") }}</h2>
        <p class="text-sm text-slate-400 mt-0.5">
          {{ t("dash.subtitle") }}
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-2.5">
        <button
          @click="fetchOverview(true)"
          class="px-3 py-2 rounded-xl bg-surface border border-slate-700 hover:border-slate-500 text-xs font-medium text-slate-300 flex items-center gap-1.5 transition-colors"
        >
          <RefreshCw class="w-3.5 h-3.5" :class="{ 'animate-spin': loading }" />
          <span>{{ t("dash.refresh") }}</span>
        </button>

        <button
          @click="toggleKillSwitch"
          class="px-3.5 py-2 rounded-xl text-xs font-semibold flex items-center gap-2 border transition-all"
          :class="
            data.kpis.globalKillSwitch
              ? 'bg-rose-500/20 border-rose-500 text-rose-300 shadow-lg shadow-rose-500/20'
              : 'bg-surface border-slate-700 hover:border-rose-500/50 text-slate-200 hover:text-rose-400'
          "
        >
          <AlertOctagon class="w-4 h-4" />
          <span>{{ data.kpis.globalKillSwitch ? t("dash.killActive") : t("dash.killBtn") }}</span>
        </button>
      </div>
    </div>

    <!-- Kill Switch Alert Banner -->
    <div
      v-if="data.kpis.globalKillSwitch"
      class="p-4 rounded-2xl bg-rose-950/60 border border-rose-500/50 flex items-center justify-between gap-4"
    >
      <div class="flex items-center gap-3">
        <ShieldAlert class="w-5 h-5 text-rose-400 shrink-0" />
        <div>
          <p class="text-sm font-semibold text-rose-200">{{ t("dash.killBannerTitle") }}</p>
          <p class="text-xs text-rose-300/80">{{ t("dash.killBannerDesc") }}</p>
        </div>
      </div>
      <button
        @click="toggleKillSwitch"
        class="px-3 py-1.5 rounded-lg bg-rose-500 text-white text-xs font-semibold hover:bg-rose-600 shrink-0"
      >
        {{ t("dash.resumeTrading") }}
      </button>
    </div>

    <div class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4">
      <div class="p-5 rounded-2xl bg-gradient-to-br from-cyan-950/50 to-surface border border-cyan-800/50">
        <div class="flex items-center justify-between text-slate-300 text-sm mb-3">
          <span>{{ t("dash.followerEquity") }}</span><Wallet class="w-4 h-4 text-cyan-300" />
        </div>
        <div class="text-xl font-bold font-mono tabular-nums text-white">{{ formatUsd(data.kpis.totalEquity) }}</div>
        <div class="text-sm text-slate-400 mt-2">{{ t("dash.acctsLabel") }} {{ data.kpis.activeAccounts }}/{{ data.kpis.totalAccounts }}</div>
      </div>
      <div class="p-5 rounded-2xl bg-surface border border-slate-700/80">
        <div class="flex items-center justify-between text-slate-300 text-sm mb-3">
          <span>{{ t("dash.avail") }}</span><Wallet class="w-4 h-4 text-accent" />
        </div>
        <div class="text-xl font-bold font-mono tabular-nums text-white">{{ formatUsd(data.kpis.availEquity) }}</div>
        <div class="text-sm text-slate-400 mt-2">{{ data.kpis.activeBindings }} {{ t("dash.bindingsUnit") }}</div>
      </div>
      <div class="p-5 rounded-2xl bg-surface border" :class="data.kpis.totalUnrealizedPnl >= 0 ? 'border-emerald-800/60' : 'border-rose-800/60'">
        <div class="flex items-center justify-between text-slate-300 text-sm mb-3">
          <span>{{ t("dash.unrealizedPnl") }}</span><Activity class="w-4 h-4" />
        </div>
        <div class="text-xl font-bold font-mono tabular-nums" :class="data.kpis.totalUnrealizedPnl >= 0 ? 'text-emerald-400' : 'text-rose-400'">
          {{ signedUsd(data.kpis.totalUnrealizedPnl) }}
        </div>
        <div class="text-sm text-slate-400 mt-2">{{ t("dash.hwmDrawdown") }}: {{ formatPct(data.kpis.drawdownPct, 2, false) }}</div>
      </div>
      <div class="p-5 rounded-2xl bg-gradient-to-br from-indigo-950/40 to-surface border border-indigo-800/50">
        <div class="flex items-center justify-between text-slate-300 text-sm mb-3">
          <span>{{ t("dash.exposure") }}</span><TrendingUp class="w-4 h-4 text-indigo-300" />
        </div>
        <div class="text-xl font-bold font-mono tabular-nums text-white">{{ formatUsd(data.kpis.totalNotional) }}</div>
        <div class="text-sm text-slate-400 mt-2">{{ t("dash.effectiveLev") }}: <span class="text-indigo-300">{{ formatQty(data.kpis.actualLeverage) }}x</span></div>
      </div>
    </div>

    <section class="rounded-2xl bg-surface border border-cyan-800/60 overflow-hidden shadow-lg shadow-cyan-950/20">
      <div class="px-5 py-4 border-b border-slate-700/70 flex flex-wrap items-center justify-between gap-3">
        <div class="flex flex-wrap items-center gap-3">
          <h3 class="text-lg font-semibold text-white">{{ t("dash.followerPosTitle") }}</h3>
          <span class="px-2.5 py-1 rounded-full bg-cyan-500/10 text-cyan-300 text-sm font-mono">{{ t("dash.openCount", { n: data.followerPositions.length }) }}</span>
        </div>
        <router-link to="/accounts" class="text-sm text-cyan-300 hover:text-cyan-200">{{ t("dash.manageAccounts") }} →</router-link>
      </div>
      <div v-if="data.followerPositions.length === 0" class="p-10 text-center">
        <Wallet class="w-8 h-8 text-slate-400 mx-auto mb-3" />
        <p class="text-base text-slate-200">{{ t("dash.noFollowerPos") }}</p>
        <p class="text-sm text-slate-400 mt-2">{{ t("dash.noFollowerHint") }}</p>
      </div>
      <div v-else>
        <div class="md:hidden p-4 space-y-4">
          <article v-for="pos in data.followerPositions" :key="`${pos.accountId}-${pos.instId}-${pos.posSide}`" class="p-4 rounded-xl bg-background/50 border border-slate-700/70">
            <div class="flex items-center justify-between gap-3">
              <h4 class="text-base font-bold text-white break-all">{{ pos.instId || pos.symbol }}</h4>
              <span class="text-sm font-semibold uppercase whitespace-nowrap" :class="pos.posSide === 'long' ? 'text-emerald-400' : 'text-rose-400'">{{ pos.posSide }} · {{ formatQty(pos.lever) }}x</span>
            </div>
            <p class="text-sm text-slate-400 mt-2">{{ pos.accountName }} · {{ pos.exchange?.toUpperCase() }} · {{ pos.environment === 'live' ? t('dash.liveEnv') : t('dash.demoEnv') }}</p>
            <dl class="grid grid-cols-2 gap-4 mt-4 text-sm">
              <div><dt class="text-slate-400">{{ t("dash.colQty") }}</dt><dd class="text-base font-mono text-white mt-1">{{ formatQty(pos.pos) }}</dd></div>
              <div><dt class="text-slate-400">{{ t("dash.colAvgPrice") }}</dt><dd class="text-base font-mono text-white mt-1">{{ formatPrice(pos.avgPx) }}</dd></div>
              <div>
                <dt class="text-slate-400">{{ t("dash.colCurrentPrice") }}</dt>
                <dd class="text-base font-mono font-bold text-cyan-300 mt-1">{{ formatPrice(pos.curPx || pos.currentPrice || pos.markPx || null) }}</dd>
                <p v-if="pos.priceSource === 'estimated' || pos.priceSource === 'entry'" class="text-sm text-slate-400 mt-1">{{ t(`dash.priceSource.${pos.priceSource}`) }}</p>
              </div>
              <div :class="pos.upl >= 0 ? 'text-emerald-400' : 'text-rose-400'">
                <dt class="text-slate-400">{{ t("dash.colUpnl") }}</dt>
                <dd class="text-base font-mono font-bold mt-1">{{ signedUsd(pos.upl) }}<span class="block text-sm mt-1" :title="t('dash.pnlRatioNote')">({{ formatPct(pos.uplRatio == null ? null : Number(pos.uplRatio) * 100) }})</span></dd>
              </div>
            </dl>
            <div class="flex justify-between gap-3 mt-4 pt-3 border-t border-slate-700/70 text-sm"><span class="text-slate-400">{{ t("dash.colNotional") }}</span><span class="font-mono text-white">{{ formatUsd(pos.notionalUsd) }}</span></div>
            <p class="text-sm text-slate-400 mt-2">{{ t("dash.lastSync") }} {{ syncTime(pos.lastSyncAt) }}</p>
          </article>
        </div>
        <div class="hidden md:block overflow-x-auto">
        <table class="w-full min-w-[1060px] text-sm text-left">
          <thead class="bg-background/50 text-slate-300 border-b border-slate-700/70">
            <tr class="[&>th]:px-5 [&>th]:py-3 [&>th]:font-medium">
              <th>{{ t("dash.colAcctContract") }}</th>
              <th>{{ t("dash.colSideLeverage") }}</th>
              <th class="text-right">{{ t("dash.colQty") }}</th>
              <th class="text-right">{{ t("dash.colAvgPrice") }}</th>
              <th class="text-right">{{ t("dash.colCurrentPrice") }}</th>
              <th class="text-right" :title="t('dash.pnlRatioNote')">{{ t("dash.colUpnl") }}</th>
              <th class="text-right">{{ t("dash.colNotional") }}</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-800/70">
            <tr v-for="pos in data.followerPositions" :key="`${pos.accountId}-${pos.instId}-${pos.posSide}`" class="hover:bg-slate-800/30 [&>td]:px-5 [&>td]:py-4">
              <td>
                <div class="text-base font-bold text-white">{{ pos.instId || pos.symbol }}</div>
                <div class="text-sm text-slate-400 mt-1">{{ pos.accountName }} · {{ pos.exchange?.toUpperCase() }} · {{ pos.environment === 'live' ? t('dash.liveEnv') : t('dash.demoEnv') }}</div>
                <div class="text-sm text-slate-400 mt-1">{{ t("dash.lastSync") }} {{ syncTime(pos.lastSyncAt) }}</div>
              </td>
              <td>
                <span class="inline-flex px-2.5 py-1 rounded-lg font-semibold uppercase whitespace-nowrap" :class="pos.posSide === 'long' ? 'bg-emerald-500/15 text-emerald-400' : 'bg-rose-500/15 text-rose-400'">{{ pos.posSide }}</span>
                <div class="text-sm text-slate-300 font-mono mt-1.5">{{ formatQty(pos.lever) }}x</div>
              </td>
              <td class="text-right font-mono tabular-nums text-slate-100 whitespace-nowrap">{{ formatQty(pos.pos) }}</td>
              <td class="text-right font-mono tabular-nums text-slate-200 whitespace-nowrap">{{ formatPrice(pos.avgPx) }}</td>
              <td class="text-right font-mono tabular-nums whitespace-nowrap">
                <div class="text-base font-bold text-cyan-300">{{ formatPrice(pos.curPx || pos.currentPrice || pos.markPx || null) }}</div>
                <div v-if="pos.priceSource === 'estimated' || pos.priceSource === 'entry'" class="text-sm font-sans text-slate-400 mt-1">{{ t(`dash.priceSource.${pos.priceSource}`) }}</div>
              </td>
              <td class="text-right font-mono tabular-nums font-semibold whitespace-nowrap" :class="pos.upl >= 0 ? 'text-emerald-400' : 'text-rose-400'">
                {{ signedUsd(pos.upl) }}
                <span class="block mt-1 text-sm" :title="t('dash.pnlRatioNote')">({{ formatPct(pos.uplRatio == null ? null : Number(pos.uplRatio) * 100) }})</span>
              </td>
              <td class="text-right font-mono tabular-nums text-slate-100 whitespace-nowrap">{{ formatUsd(pos.notionalUsd) }}</td>
            </tr>
          </tbody>
        </table>
        </div>
      </div>
    </section>

    <section class="space-y-4">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 class="text-lg font-semibold text-white">{{ t("dash.syncAlignmentTitle") }}</h3>
          <p class="text-sm text-slate-400 mt-1">{{ t("dash.alignmentNote") }}</p>
        </div>
        <router-link to="/public" class="inline-flex items-center gap-2 px-3 py-2 rounded-xl border border-slate-700 text-sm text-cyan-300 hover:bg-slate-800">
          <Globe class="w-4 h-4" />{{ t("dash.enterRadar") }}
        </router-link>
      </div>
      <div v-if="!leadsStatus.length" class="p-6 rounded-2xl bg-surface border border-slate-800 text-sm text-slate-300">
        {{ t("dash.noLeadPos") }} <router-link to="/leads" class="text-cyan-300">{{ t("dash.manageLeads") }} →</router-link>
      </div>
      <div v-else class="grid grid-cols-1 xl:grid-cols-2 gap-4">
        <article v-for="lead in leadsStatus" :key="lead.id" class="p-5 rounded-2xl bg-surface border border-slate-700/70">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <h4 class="text-base font-bold text-white">{{ lead.name }}</h4>
            <span class="inline-flex items-center gap-2 text-sm" :class="syncState(lead) === 'healthy' ? 'text-emerald-400' : syncState(lead) === 'error' ? 'text-rose-400' : 'text-amber-300'">
              <span class="w-2 h-2 rounded-full bg-current"></span>{{ t(`dash.sync.${syncState(lead)}`) }}
            </span>
          </div>
          <div class="flex flex-wrap gap-x-6 gap-y-2 mt-3 text-sm text-slate-300">
            <span>{{ t("dash.lastSync") }} <span class="font-mono">{{ syncTime(lead.lastSyncAt) }}</span></span>
            <span>{{ t("dash.leadOpenCount", { n: lead.openPositionsCount }) }}</span>
          </div>
          <p class="mt-3 text-sm font-medium" :class="getLeadAlignment(lead, data) === 'aligned' ? 'text-emerald-400' : getLeadAlignment(lead, data) === 'mismatch' ? 'text-rose-400' : 'text-amber-300'">{{ t(`dash.alignment.${getLeadAlignment(lead, data)}`) }}</p>
          <p v-if="lead.lastError" class="mt-2 text-sm text-rose-300 break-words">{{ lead.lastError }}</p>
          <div class="flex flex-wrap items-center gap-3 mt-4">
            <button type="button" @click="openCalculator(lead.id)" class="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg bg-cyan-500/10 border border-cyan-800/70 text-cyan-300 text-sm hover:bg-cyan-500/20">
              <Calculator class="w-4 h-4" />{{ t("dash.openCalculator") }}
            </button>
            <router-link to="/bindings" class="text-sm text-slate-300 hover:text-white">{{ t("dash.manageBindings") }} →</router-link>
          </div>
        </article>
      </div>
    </section>

    <section ref="calculatorSection" class="rounded-2xl bg-surface border border-slate-700/70 overflow-hidden scroll-mt-20">
      <div class="px-5 py-4 border-b border-slate-800 flex flex-wrap items-center justify-between gap-3">
        <div class="flex flex-wrap items-center gap-3">
          <h3 class="text-lg font-semibold text-white">{{ t("dash.monitoredLeadPos") }}</h3>
          <span class="text-sm font-mono text-slate-400">{{ t("dash.activeCount", { n: data.leadPositions.length }) }}</span>
        </div>
        <button v-if="leadsStatus.length" type="button" @click="showLeadCalculator ? showLeadCalculator = false : openCalculator()" class="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg border border-cyan-800/70 text-cyan-300 text-sm hover:bg-cyan-500/10">
          <Calculator class="w-4 h-4" />{{ t(showLeadCalculator ? 'dash.hideCalculator' : 'dash.openCalculator') }}
        </button>
      </div>
      <div v-if="showLeadCalculator" class="p-4 border-b border-slate-800 bg-background/50">
        <label class="flex flex-wrap items-center gap-3 text-sm text-slate-300 mb-4">
          {{ t("dash.calculatorLead") }}
          <select :value="calculatorLead?.id" @change="calculatorLeadId = Number(($event.target as HTMLSelectElement).value)" class="bg-surface border border-slate-700 rounded-lg px-3 py-2 text-white">
            <option v-for="lead in leadsStatus" :key="lead.id" :value="lead.id">{{ lead.name }}</option>
          </select>
        </label>
        <PositionCalculatorCard :open-positions="calculatorPositions" :lead-margin-balance="calculatorLead?.marginBalance" :display-name="calculatorLead?.name" :compact="true" />
      </div>
      <div v-if="!data.leadPositions.length" class="p-8 text-center text-sm text-slate-300">{{ t("dash.noLeadPos") }}</div>
      <div v-else class="overflow-x-auto">
        <table class="w-full min-w-[900px] text-sm text-left">
          <thead class="bg-background/40 text-slate-300 border-b border-slate-800">
            <tr class="[&>th]:px-5 [&>th]:py-3 [&>th]:font-medium">
              <th>{{ t("dash.colLeadSymbol") }}</th><th>{{ t("dash.colSide") }}</th>
              <th class="text-right">{{ t("dash.colQty") }}</th><th class="text-right">{{ t("dash.colEntryMark") }}</th>
              <th class="text-right">{{ t("dash.colNotional") }}</th><th class="text-right">{{ t("dash.colEstUpnl") }}</th><th>{{ t("dash.colPaBias") }}</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-800/60">
            <tr v-for="(pos, idx) in data.leadPositions" :key="`${pos.leadId}-${pos.symbol}-${pos.positionSide}-${idx}`" class="hover:bg-slate-800/30 [&>td]:px-5 [&>td]:py-4">
              <td><div class="text-base font-bold text-white">{{ pos.symbol }}</div><div class="text-sm text-slate-400 mt-1">{{ pos.leadName }}</div></td>
              <td><span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg font-semibold" :class="pos.positionSide === 'LONG' ? 'bg-emerald-500/15 text-emerald-400' : 'bg-rose-500/15 text-rose-400'">
                <ArrowUpRight v-if="pos.positionSide === 'LONG'" class="w-4 h-4" /><ArrowDownRight v-else class="w-4 h-4" />{{ pos.positionSide }}
              </span></td>
              <td class="text-right font-mono text-slate-200 whitespace-nowrap">{{ formatQty(pos.qty) }}</td>
              <td class="text-right font-mono tabular-nums whitespace-nowrap"><div class="text-slate-100">{{ formatPrice(pos.avgEntry) }}</div><div class="text-cyan-300 mt-1">{{ formatPrice(pos.currentPrice ?? pos.markPrice) }}</div></td>
              <td class="text-right font-mono text-slate-100 whitespace-nowrap">{{ formatUsd(pos.markValue) }}</td>
              <td class="text-right font-mono font-semibold whitespace-nowrap" :class="pos.uPnL >= 0 ? 'text-emerald-400' : 'text-rose-400'">
                {{ signedUsd(pos.uPnL) }}<div class="text-sm mt-1">{{ formatPct(pos.uPnLRoePct ?? pos.uPnLPct) }}</div>
              </td>
              <td><span v-if="pos.analysis?.ok" class="text-sm text-accent">{{ isZh && pos.analysis.verdictZh ? pos.analysis.verdictZh : pos.analysis.verdict }} · {{ isZh && pos.analysis.trend1hZh ? pos.analysis.trend1hZh : pos.analysis.trend1h }}</span><span v-else class="text-slate-400">—</span></td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="rounded-2xl bg-surface border border-slate-700/70 overflow-hidden">
      <div class="px-5 py-4 border-b border-slate-800 flex flex-wrap items-center justify-between gap-3">
        <h3 class="text-lg font-semibold text-white">{{ t("dash.execFeedTitle") }}</h3>
        <span class="text-sm text-accent inline-flex items-center gap-2"><Activity class="w-4 h-4" />{{ t("dash.liveStream") }}</span>
      </div>
      <div v-if="!data.recentExecutions.length" class="p-8 text-center text-sm text-slate-300">{{ t("dash.waitingEvents") }}</div>
      <div v-else class="divide-y divide-slate-800/60 max-h-[480px] overflow-y-auto">
        <div v-for="ex in data.recentExecutions" :key="ex.id" class="px-5 py-4 hover:bg-slate-800/30">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div class="flex flex-wrap items-center gap-3"><span class="text-base font-bold text-white">{{ ex.symbol }}</span><span class="text-sm text-slate-300">{{ ex.action }}</span><span class="px-2.5 py-1 rounded-lg text-sm border" :class="statusBadge(ex.status)">{{ ex.status }}</span></div>
            <span class="text-sm font-mono text-slate-400">{{ syncTime(ex.createdAt) }}</span>
          </div>
          <div class="flex flex-wrap items-center justify-between gap-3 text-sm mt-2">
            <span class="text-slate-300">{{ ex.leadName }} → {{ ex.accountName }}</span>
            <span v-if="ex.myNotional > 0" class="font-mono text-slate-100">{{ formatQty(ex.myQty) }} @ {{ formatPrice(ex.myPrice) }} · {{ formatUsd(ex.myNotional) }}</span>
          </div>
          <p v-if="ex.details" class="text-sm text-slate-400 mt-2 break-words leading-relaxed">{{ ex.details }}</p>
        </div>
      </div>
    </section>
  </div>
</template>
