<script setup lang="ts">
import { onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from "vue";
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
  Zap,
} from "lucide-vue-next";
import { api } from "@/api/client";
import { useSocketStore } from "@/stores/socket";
import { useDataCacheStore } from "@/stores/dataCache";
import { useI18n } from "@/i18n";
import PositionCalculatorCard from "@/components/PositionCalculatorCard.vue";

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

function formatUsd(val: number) {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(val || 0);
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
        <h2 class="text-xl sm:text-2xl font-bold text-white tracking-tight">{{ t("dash.title") }}</h2>
        <p class="text-xs sm:text-sm text-slate-400 mt-0.5">
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

    <!-- KPI Cards -->
    <div class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4">
      <div class="p-5 rounded-2xl bg-surface border border-slate-800/80 shadow-lg">
        <div class="flex items-center justify-between text-slate-400 text-xs font-medium mb-2">
          <span>{{ t("dash.followerEquity") }}</span>
          <Wallet class="w-4 h-4 text-accent" />
        </div>
        <div class="text-2xl font-bold font-mono text-white">{{ formatUsd(data.kpis.totalEquity) }}</div>
        <div class="text-xs text-slate-400 mt-1.5 font-mono">
          {{ t("dash.avail") }}: <span class="text-slate-200">{{ formatUsd(data.kpis.availEquity) }}</span>
        </div>
      </div>

      <div class="p-5 rounded-2xl bg-surface border border-slate-800/80 shadow-lg">
        <div class="flex items-center justify-between text-slate-400 text-xs font-medium mb-2">
          <span>{{ t("dash.exposure") }}</span>
          <TrendingUp class="w-4 h-4 text-brand" />
        </div>
        <div class="text-2xl font-bold font-mono text-white">{{ formatUsd(data.kpis.totalNotional) }}</div>
        <div class="text-xs text-slate-400 mt-1.5 font-mono">
          {{ t("dash.effectiveLev") }}: <span class="text-accent">{{ data.kpis.actualLeverage }}x</span>
        </div>
      </div>

      <div class="p-5 rounded-2xl bg-surface border border-slate-800/80 shadow-lg">
        <div class="flex items-center justify-between text-slate-400 text-xs font-medium mb-2">
          <span>{{ t("dash.unrealizedPnl") }}</span>
          <Activity class="w-4 h-4 text-emerald-400" />
        </div>
        <div
          class="text-2xl font-bold font-mono"
          :class="data.kpis.totalUnrealizedPnl >= 0 ? 'text-emerald-400' : 'text-rose-400'"
        >
          {{ data.kpis.totalUnrealizedPnl >= 0 ? "+" : "" }}{{ formatUsd(data.kpis.totalUnrealizedPnl) }}
        </div>
        <div class="text-xs text-slate-400 mt-1.5 font-mono">
          {{ t("dash.hwmDrawdown") }}:
          <span :class="data.kpis.drawdownPct > 15 ? 'text-rose-400' : 'text-slate-200'">
            {{ data.kpis.drawdownPct }}%
          </span>
        </div>
      </div>

      <div class="p-5 rounded-2xl bg-surface border border-slate-800/80 shadow-lg">
        <div class="flex items-center justify-between text-slate-400 text-xs font-medium mb-2">
          <span>{{ t("dash.topology") }}</span>
          <Zap class="w-4 h-4 text-amber-400" />
        </div>
        <div class="text-2xl font-bold font-mono text-white">
          {{ data.kpis.activeBindings }} <span class="text-sm font-normal text-slate-400">{{ t("dash.bindingsUnit") }}</span>
        </div>
        <div class="text-xs text-slate-400 mt-1.5 font-mono">
          {{ t("dash.leadsLabel") }}: {{ data.kpis.activeLeads }}/{{ data.kpis.totalLeads }} · {{ t("dash.acctsLabel") }}: {{ data.kpis.activeAccounts }}/{{ data.kpis.totalAccounts }}
        </div>
      </div>
    </div>

    <!-- Main Grid: Positions + Executions -->
    <div class="grid grid-cols-1 lg:grid-cols-12 gap-6">
      <div class="lg:col-span-7 min-w-0 space-y-6">
        <div class="rounded-2xl bg-surface border border-slate-800/80 overflow-hidden">
          <div class="px-5 py-4 border-b border-slate-800/80 flex flex-wrap items-center justify-between gap-3">
            <h3 class="text-base font-semibold text-white">{{ t("dash.monitoredLeadPos") }}</h3>
            <div class="flex flex-wrap items-center gap-2">
              <span class="text-xs font-mono text-slate-300">{{ t("dash.activeCount", { n: data.leadPositions.length }) }}</span>
              <button
                v-if="data.leadPositions.length > 0"
                type="button"
                @click="showLeadCalculator = !showLeadCalculator"
                class="min-h-10 px-3 py-2 rounded-lg text-xs font-mono font-semibold flex items-center gap-1.5 border transition-all cursor-pointer"
                :class="
                  showLeadCalculator
                    ? 'bg-cyan-500/20 text-cyan-300 border-cyan-500/50'
                    : 'bg-surface2/80 hover:bg-slate-700 text-slate-300 border-slate-700'
                "
              >
                <Calculator class="w-3.5 h-3.5 text-cyan-400" />
                <span>{{ showLeadCalculator ? (isZh ? '收起测算' : 'Hide Calc') : (isZh ? '开仓测算' : 'Calculator') }}</span>
              </button>
            </div>
          </div>
          <div v-if="showLeadCalculator" class="p-4 border-b border-slate-800/80 bg-slate-900/60">
            <PositionCalculatorCard
              :open-positions="data.leadPositions"
              :compact="true"
            />
          </div>
          <div v-if="data.leadPositions.length === 0" class="p-8 text-center text-sm text-slate-300">
            {{ t("dash.noLeadPos") }}
          </div>
          <div v-else class="overflow-x-auto">
            <table class="w-full min-w-[800px] text-left border-collapse text-sm">
              <thead>
                <tr class="border-b border-slate-800 text-xs font-mono font-semibold uppercase text-slate-300 [&>th]:font-semibold">
                  <th class="py-3 px-4">{{ t("dash.colLeadSymbol") }}</th>
                  <th class="py-3 px-4">{{ t("dash.colSide") }}</th>
                  <th class="py-3 px-4 text-right">{{ t("dash.colEntryMark") }}</th>
                  <th class="py-3 px-4 text-right">{{ t("dash.colNotional") }}</th>
                  <th class="py-3 px-4 text-right">{{ t("dash.colEstUpnl") }}</th>
                  <th class="py-3 px-4">{{ t("dash.colPaBias") }}</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-slate-800/60">
                <tr v-for="(pos, idx) in data.leadPositions" :key="idx" class="hover:bg-slate-800/30">
                  <td class="py-3 px-4">
                    <div class="text-base font-bold text-white">{{ pos.symbol }}</div>
                    <div class="text-xs text-slate-300 mt-1">{{ pos.leadName }}</div>
                  </td>
                  <td class="py-3 px-4">
                    <span
                      class="inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-bold font-mono whitespace-nowrap"
                      :class="pos.positionSide === 'LONG' ? 'bg-emerald-500/15 text-emerald-400' : 'bg-rose-500/15 text-rose-400'"
                    >
                      <ArrowUpRight v-if="pos.positionSide === 'LONG'" class="w-3 h-3" />
                      <ArrowDownRight v-else class="w-3 h-3" />
                      {{ pos.positionSide }}
                    </span>
                  </td>
                  <td class="py-3 px-4 text-right text-sm font-bold font-mono whitespace-nowrap">
                    <div class="text-slate-100">{{ pos.avgEntry }}</div>
                    <div class="text-sm text-slate-300 mt-1">{{ pos.currentPrice }}</div>
                  </td>
                  <td class="py-3 px-4 text-right text-sm font-bold font-mono text-slate-100 whitespace-nowrap">
                    {{ formatUsd(pos.markValue) }}
                  </td>
                  <td
                    class="py-3 px-4 text-right text-sm font-bold font-mono whitespace-nowrap"
                    :class="pos.uPnL >= 0 ? 'text-emerald-400' : 'text-rose-400'"
                  >
                    {{ pos.uPnL >= 0 ? "+" : "" }}{{ formatUsd(pos.uPnL) }}
                    <div class="text-xs font-mono font-normal mt-1">{{ (pos.uPnLRoePct ?? pos.uPnLPct ?? 0).toFixed(2) }}%</div>
                  </td>
                  <td class="py-3 px-4">
                    <span v-if="pos.analysis?.ok" class="text-xs font-mono text-accent">
                      {{ isZh && pos.analysis.verdictZh ? pos.analysis.verdictZh : pos.analysis.verdict }} ·
                      {{ isZh && pos.analysis.trend1hZh ? pos.analysis.trend1hZh : pos.analysis.trend1h }}
                    </span>
                    <span v-else class="text-xs text-slate-300">—</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div class="rounded-2xl bg-surface border border-slate-800/80 overflow-hidden">
          <div class="px-5 py-4 border-b border-slate-800/80 flex flex-wrap items-center justify-between gap-3">
            <h3 class="text-base font-semibold text-white">{{ t("dash.followerPosTitle") }}</h3>
            <span class="text-xs font-mono text-slate-300">{{ t("dash.openCount", { n: data.followerPositions.length }) }}</span>
          </div>
          <div v-if="data.followerPositions.length === 0" class="p-8 text-center text-sm text-slate-300">
            {{ t("dash.noFollowerPos") }}
          </div>
          <div v-else class="overflow-x-auto">
            <table class="w-full min-w-[700px] text-left border-collapse text-sm">
              <thead>
                <tr class="border-b border-slate-800 text-xs font-mono font-semibold uppercase text-slate-300 [&>th]:font-semibold">
                  <th class="py-3 px-4">{{ t("dash.colAcctContract") }}</th>
                  <th class="py-3 px-4">{{ t("dash.colSide") }}</th>
                  <th class="py-3 px-4 text-right">{{ t("dash.colSizeAvgPx") }}</th>
                  <th class="py-3 px-4 text-right">{{ t("dash.colNotional") }}</th>
                  <th class="py-3 px-4 text-right">{{ t("dash.colUpnl") }}</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-slate-800/60">
                <tr v-for="(pos, idx) in data.followerPositions" :key="idx" class="hover:bg-slate-800/30">
                  <td class="py-3 px-4">
                    <div class="text-base font-bold text-white">{{ pos.instId }}</div>
                    <div class="text-xs text-slate-300 uppercase mt-1">
                      {{ pos.accountName }} · {{ pos.exchange }} ({{ pos.environment }})
                    </div>
                  </td>
                  <td class="py-3 px-4">
                    <span
                      class="inline-flex px-2.5 py-1 rounded-md text-xs font-bold font-mono uppercase whitespace-nowrap"
                      :class="pos.posSide === 'long' ? 'bg-emerald-500/15 text-emerald-400' : 'bg-rose-500/15 text-rose-400'"
                    >
                      {{ pos.posSide }} {{ pos.lever }}x
                    </span>
                  </td>
                  <td class="py-3 px-4 text-right text-sm font-bold font-mono whitespace-nowrap">
                    <div class="text-slate-100">{{ pos.pos }}</div>
                    <div class="text-sm text-slate-300 mt-1">{{ pos.avgPx }}</div>
                  </td>
                  <td class="py-3 px-4 text-right text-sm font-bold font-mono text-slate-100 whitespace-nowrap">{{ formatUsd(pos.notionalUsd) }}</td>
                  <td
                    class="py-3 px-4 text-right text-sm font-bold font-mono whitespace-nowrap"
                    :class="pos.upl >= 0 ? 'text-emerald-400' : 'text-rose-400'"
                  >
                    {{ pos.upl >= 0 ? "+" : "" }}{{ formatUsd(pos.upl) }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <div class="lg:col-span-5 min-w-0">
        <div class="rounded-2xl bg-surface border border-slate-800/80 overflow-hidden h-full flex flex-col">
          <div class="px-5 py-4 border-b border-slate-800/80 flex flex-wrap items-center justify-between gap-3">
            <h3 class="text-base font-semibold text-white">{{ t("dash.execFeedTitle") }}</h3>
            <span class="text-xs font-mono text-accent">{{ t("dash.liveStream") }}</span>
          </div>
          <div v-if="data.recentExecutions.length === 0" class="p-8 text-center text-sm text-slate-300 flex-1">
            {{ t("dash.waitingEvents") }}
          </div>
          <div v-else class="divide-y divide-slate-800/60 max-h-[640px] overflow-y-auto">
            <div v-for="ex in data.recentExecutions" :key="ex.id" class="p-4 hover:bg-slate-800/30 transition-colors">
              <div class="flex flex-wrap items-center justify-between gap-2 mb-2">
                <div class="flex flex-wrap items-center gap-2 min-w-0">
                  <span class="text-sm font-bold text-white break-all">{{ ex.symbol }}</span>
                  <span class="text-xs font-mono text-slate-300">{{ ex.action }}</span>
                </div>
                <span class="max-w-full px-2.5 py-1 rounded-full text-xs font-mono font-semibold border break-all" :class="statusBadge(ex.status)">
                  {{ ex.status }}
                </span>
              </div>
              <div class="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 text-xs text-slate-300 font-mono">
                <span class="min-w-0 break-words">{{ ex.leadName }} → {{ ex.accountName }}</span>
                <span v-if="ex.myNotional > 0" class="text-sm font-bold text-slate-100 break-words">
                  ${{ ex.myNotional }} @ {{ ex.myPrice }}
                </span>
              </div>
              <p v-if="ex.details" class="text-sm text-slate-300 mt-2 break-words leading-relaxed">{{ ex.details }}</p>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
