<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from "vue";
import {
  Activity,
  AlertOctagon,
  ArrowDownRight,
  ArrowUpRight,
  RefreshCw,
  ShieldAlert,
  TrendingUp,
  Wallet,
  Zap,
} from "lucide-vue-next";
import { api } from "@/api/client";
import { useSocketStore } from "@/stores/socket";

const socketStore = useSocketStore();
const loading = ref(true);
const data = ref<any>({
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
});

let pollTimer: any = null;

async function fetchOverview() {
  try {
    const res = await api.get<any>("/api/dashboard/overview");
    data.value = res;
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
  } catch (e) {
    console.error("Kill switch toggle failed", e);
  }
}

watch(
  () => socketStore.recentEvents.length,
  () => {
    fetchOverview();
  }
);

onMounted(() => {
  fetchOverview();
  pollTimer = setInterval(fetchOverview, 6000);
});

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer);
});

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
        <h2 class="text-xl sm:text-2xl font-bold text-white tracking-tight">Terminal Overview</h2>
        <p class="text-xs sm:text-sm text-slate-400 mt-0.5">
          Real-time portfolio telemetry, PA signals, and copy execution matrix
        </p>
      </div>
      <div class="flex items-center gap-2.5">
        <button
          @click="fetchOverview"
          class="px-3 py-2 rounded-xl bg-surface border border-slate-700 hover:border-slate-500 text-xs font-medium text-slate-300 flex items-center gap-1.5 transition-colors"
        >
          <RefreshCw class="w-3.5 h-3.5" :class="{ 'animate-spin': loading }" />
          <span>Refresh</span>
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
          <span>{{ data.kpis.globalKillSwitch ? "KILL SWITCH ACTIVE (HALTED)" : "Emergency Kill Switch" }}</span>
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
          <p class="text-sm font-semibold text-rose-200">Global Kill Switch Engaged</p>
          <p class="text-xs text-rose-300/80">
            All new copy-trade open orders are blocked. Reduce-only and full close sync orders remain active for safety.
          </p>
        </div>
      </div>
      <button
        @click="toggleKillSwitch"
        class="px-3 py-1.5 rounded-lg bg-rose-500 text-white text-xs font-semibold hover:bg-rose-600 shrink-0"
      >
        Resume Trading
      </button>
    </div>

    <!-- KPI Cards -->
    <div class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4">
      <!-- Total Equity -->
      <div class="p-5 rounded-2xl bg-surface border border-slate-800/80 shadow-lg">
        <div class="flex items-center justify-between text-slate-400 text-xs font-medium mb-2">
          <span>FOLLOWER EQUITY</span>
          <Wallet class="w-4 h-4 text-accent" />
        </div>
        <div class="text-2xl font-bold font-mono text-white">{{ formatUsd(data.kpis.totalEquity) }}</div>
        <div class="text-xs text-slate-400 mt-1.5 font-mono">
          Avail: <span class="text-slate-200">{{ formatUsd(data.kpis.availEquity) }}</span>
        </div>
      </div>

      <!-- Notional & Leverage -->
      <div class="p-5 rounded-2xl bg-surface border border-slate-800/80 shadow-lg">
        <div class="flex items-center justify-between text-slate-400 text-xs font-medium mb-2">
          <span>PORTFOLIO EXPOSURE</span>
          <TrendingUp class="w-4 h-4 text-brand" />
        </div>
        <div class="text-2xl font-bold font-mono text-white">{{ formatUsd(data.kpis.totalNotional) }}</div>
        <div class="text-xs text-slate-400 mt-1.5 font-mono">
          Effective Lev: <span class="text-accent">{{ data.kpis.actualLeverage }}x</span>
        </div>
      </div>

      <!-- uPnL & Drawdown -->
      <div class="p-5 rounded-2xl bg-surface border border-slate-800/80 shadow-lg">
        <div class="flex items-center justify-between text-slate-400 text-xs font-medium mb-2">
          <span>UNREALIZED PNL</span>
          <Activity class="w-4 h-4 text-emerald-400" />
        </div>
        <div
          class="text-2xl font-bold font-mono"
          :class="data.kpis.totalUnrealizedPnl >= 0 ? 'text-emerald-400' : 'text-rose-400'"
        >
          {{ data.kpis.totalUnrealizedPnl >= 0 ? "+" : "" }}{{ formatUsd(data.kpis.totalUnrealizedPnl) }}
        </div>
        <div class="text-xs text-slate-400 mt-1.5 font-mono">
          HWM Drawdown:
          <span :class="data.kpis.drawdownPct > 15 ? 'text-rose-400' : 'text-slate-200'">
            {{ data.kpis.drawdownPct }}%
          </span>
        </div>
      </div>

      <!-- Active Nodes -->
      <div class="p-5 rounded-2xl bg-surface border border-slate-800/80 shadow-lg">
        <div class="flex items-center justify-between text-slate-400 text-xs font-medium mb-2">
          <span>TOPOLOGY STATUS</span>
          <Zap class="w-4 h-4 text-amber-400" />
        </div>
        <div class="text-2xl font-bold font-mono text-white">
          {{ data.kpis.activeBindings }} <span class="text-sm font-normal text-slate-400">Bindings</span>
        </div>
        <div class="text-xs text-slate-400 mt-1.5 font-mono">
          Leads: {{ data.kpis.activeLeads }}/{{ data.kpis.totalLeads }} · Accts: {{ data.kpis.activeAccounts }}/{{ data.kpis.totalAccounts }}
        </div>
      </div>
    </div>

    <!-- Main Grid: Positions + Executions -->
    <div class="grid grid-cols-1 lg:grid-cols-12 gap-6">
      <!-- Left: Lead & Follower Positions -->
      <div class="lg:col-span-7 space-y-6">
        <!-- Lead Open Positions -->
        <div class="rounded-2xl bg-surface border border-slate-800/80 overflow-hidden">
          <div class="px-5 py-4 border-b border-slate-800/80 flex items-center justify-between">
            <h3 class="text-sm font-semibold text-white">Monitored Lead Positions (Reconstructed & PA)</h3>
            <span class="text-xs font-mono text-slate-400">{{ data.leadPositions.length }} Active</span>
          </div>
          <div v-if="data.leadPositions.length === 0" class="p-8 text-center text-xs text-slate-500">
            No open lead positions detected yet. Add and sync a Lead source in the Leads view.
          </div>
          <div v-else class="overflow-x-auto">
            <table class="w-full text-left border-collapse text-xs">
              <thead>
                <tr class="border-b border-slate-800 text-slate-400 uppercase font-mono">
                  <th class="py-3 px-4">Lead / Symbol</th>
                  <th class="py-3 px-4">Side</th>
                  <th class="py-3 px-4 text-right">Entry / Mark</th>
                  <th class="py-3 px-4 text-right">Notional</th>
                  <th class="py-3 px-4 text-right">Est. uPnL</th>
                  <th class="py-3 px-4">PA Bias</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-slate-800/60">
                <tr v-for="(pos, idx) in data.leadPositions" :key="idx" class="hover:bg-slate-800/30">
                  <td class="py-3 px-4">
                    <div class="font-semibold text-white">{{ pos.symbol }}</div>
                    <div class="text-[11px] text-slate-400">{{ pos.leadName }}</div>
                  </td>
                  <td class="py-3 px-4">
                    <span
                      class="inline-flex items-center gap-1 px-2 py-0.5 rounded-md font-mono font-semibold text-[11px]"
                      :class="
                        pos.positionSide === 'LONG'
                          ? 'bg-emerald-500/15 text-emerald-400'
                          : 'bg-rose-500/15 text-rose-400'
                      "
                    >
                      <ArrowUpRight v-if="pos.positionSide === 'LONG'" class="w-3 h-3" />
                      <ArrowDownRight v-else class="w-3 h-3" />
                      {{ pos.positionSide }}
                    </span>
                  </td>
                  <td class="py-3 px-4 text-right font-mono">
                    <div class="text-slate-200">{{ pos.avgEntry }}</div>
                    <div class="text-[11px] text-slate-400">{{ pos.currentPrice }}</div>
                  </td>
                  <td class="py-3 px-4 text-right font-mono text-slate-200">
                    {{ formatUsd(pos.markValue) }}
                  </td>
                  <td
                    class="py-3 px-4 text-right font-mono font-semibold"
                    :class="pos.uPnL >= 0 ? 'text-emerald-400' : 'text-rose-400'"
                  >
                    {{ pos.uPnL >= 0 ? "+" : "" }}{{ formatUsd(pos.uPnL) }}
                    <div class="text-[10px] font-normal opacity-80">{{ pos.roePct }}%</div>
                  </td>
                  <td class="py-3 px-4">
                    <span v-if="pos.analysis?.ok" class="text-[11px] font-mono text-accent">
                      {{ pos.analysis.summary?.trendLabel || pos.analysis.tf1h?.trend }}
                    </span>
                    <span v-else class="text-[11px] text-slate-500">N/A</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- Follower Open Positions -->
        <div class="rounded-2xl bg-surface border border-slate-800/80 overflow-hidden">
          <div class="px-5 py-4 border-b border-slate-800/80 flex items-center justify-between">
            <h3 class="text-sm font-semibold text-white">Follower Exchange Positions</h3>
            <span class="text-xs font-mono text-slate-400">{{ data.followerPositions.length }} Open</span>
          </div>
          <div v-if="data.followerPositions.length === 0" class="p-8 text-center text-xs text-slate-500">
            No active follower positions across connected exchange accounts.
          </div>
          <div v-else class="overflow-x-auto">
            <table class="w-full text-left border-collapse text-xs">
              <thead>
                <tr class="border-b border-slate-800 text-slate-400 uppercase font-mono">
                  <th class="py-3 px-4">Account / Contract</th>
                  <th class="py-3 px-4">Side</th>
                  <th class="py-3 px-4 text-right">Size / AvgPx</th>
                  <th class="py-3 px-4 text-right">Notional</th>
                  <th class="py-3 px-4 text-right">uPnL</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-slate-800/60">
                <tr v-for="(pos, idx) in data.followerPositions" :key="idx" class="hover:bg-slate-800/30">
                  <td class="py-3 px-4">
                    <div class="font-semibold text-white">{{ pos.instId }}</div>
                    <div class="text-[11px] text-slate-400 uppercase">
                      {{ pos.accountName }} · {{ pos.exchange }} ({{ pos.environment }})
                    </div>
                  </td>
                  <td class="py-3 px-4">
                    <span
                      class="px-2 py-0.5 rounded-md font-mono font-semibold text-[11px] uppercase"
                      :class="
                        pos.posSide === 'long'
                          ? 'bg-emerald-500/15 text-emerald-400'
                          : 'bg-rose-500/15 text-rose-400'
                      "
                    >
                      {{ pos.posSide }} {{ pos.lever }}x
                    </span>
                  </td>
                  <td class="py-3 px-4 text-right font-mono">
                    <div class="text-slate-200">{{ pos.pos }}</div>
                    <div class="text-[11px] text-slate-400">{{ pos.avgPx }}</div>
                  </td>
                  <td class="py-3 px-4 text-right font-mono text-slate-200">{{ formatUsd(pos.notionalUsd) }}</td>
                  <td
                    class="py-3 px-4 text-right font-mono font-semibold"
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

      <!-- Right: Live Execution Feed -->
      <div class="lg:col-span-5">
        <div class="rounded-2xl bg-surface border border-slate-800/80 overflow-hidden h-full flex flex-col">
          <div class="px-5 py-4 border-b border-slate-800/80 flex items-center justify-between">
            <h3 class="text-sm font-semibold text-white">Copy Execution & Risk Feed</h3>
            <span class="text-xs font-mono text-accent">LIVE STREAM</span>
          </div>
          <div v-if="data.recentExecutions.length === 0" class="p-8 text-center text-xs text-slate-500 flex-1">
            Waiting for lead trade events...
          </div>
          <div v-else class="divide-y divide-slate-800/60 max-h-[640px] overflow-y-auto">
            <div v-for="ex in data.recentExecutions" :key="ex.id" class="p-4 hover:bg-slate-800/30 transition-colors">
              <div class="flex items-center justify-between gap-2 mb-1.5">
                <div class="flex items-center gap-2 min-w-0">
                  <span class="font-bold text-xs text-white truncate">{{ ex.symbol }}</span>
                  <span class="text-[11px] font-mono text-slate-400">{{ ex.action }}</span>
                </div>
                <span
                  class="px-2 py-0.5 rounded-full text-[10px] font-mono border shrink-0"
                  :class="statusBadge(ex.status)"
                >
                  {{ ex.status }}
                </span>
              </div>
              <div class="flex items-center justify-between text-[11px] text-slate-400 font-mono">
                <span>{{ ex.leadName }} → {{ ex.accountName }}</span>
                <span v-if="ex.myNotional > 0" class="text-slate-200">
                  ${{ ex.myNotional }} @ {{ ex.myPrice }}
                </span>
              </div>
              <p v-if="ex.details" class="text-[11px] text-slate-500 mt-1.5 break-words">{{ ex.details }}</p>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
