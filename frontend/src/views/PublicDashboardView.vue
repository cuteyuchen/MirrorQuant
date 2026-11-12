<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { ArrowDownRight, ArrowUpRight, Cpu, Globe, Lock, RefreshCw, ShieldCheck } from "lucide-vue-next";
import { api } from "@/api/client";

const loading = ref(true);
const enabled = ref(true);
const leads = ref<any[]>([]);
const lastUpdatedAt = ref<string>("");
let timer: any = null;

async function fetchPublic() {
  try {
    const res = await api.get<{ enabled: boolean; leads: any[] }>("/api/dashboard/public");
    enabled.value = res.enabled;
    leads.value = res.leads;
    lastUpdatedAt.value = new Date().toLocaleTimeString();
  } finally {
    loading.value = false;
  }
}

function formatUsd(val: number | null | undefined) {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(val || 0);
}

function formatPrice(val: number | null | undefined) {
  if (val === null || val === undefined) return "--";
  const abs = Math.abs(val);
  if (abs >= 1000) return val.toFixed(1);
  if (abs >= 100) return val.toFixed(2);
  if (abs >= 1) return val.toFixed(4);
  return val.toFixed(6);
}

function verdictBadgeClass(level: string | undefined) {
  if (level === "good") return "bg-emerald-500/15 text-emerald-400 border-emerald-500/30";
  if (level === "mid") return "bg-cyan-500/15 text-cyan-300 border-cyan-500/30";
  if (level === "warn") return "bg-amber-500/15 text-amber-300 border-amber-500/30";
  if (level === "bad") return "bg-rose-500/15 text-rose-400 border-rose-500/30";
  return "bg-slate-700/40 text-slate-300 border-slate-600/40";
}

function formatHeld(ms: number | null | undefined) {
  if (!ms || ms < 0) return "--";
  const sec = Math.floor(ms / 1000);
  if (sec < 3600) return `${Math.floor(sec / 60)}m`;
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (h < 24) return `${h}h ${m}m`;
  const d = Math.floor(h / 24);
  return `${d}d ${h % 24}h`;
}

onMounted(() => {
  fetchPublic();
  timer = setInterval(fetchPublic, 5000);
});

onUnmounted(() => {
  if (timer) clearInterval(timer);
});
</script>

<template>
  <div class="min-h-screen bg-background text-slate-100 p-4 md:p-8 max-w-6xl mx-auto space-y-6 overflow-x-hidden">
    <!-- Top Bar -->
    <header class="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-5 border-b border-slate-800">
      <div class="flex items-center gap-3 min-w-0">
        <div class="w-10 h-10 rounded-xl bg-accent/10 border border-accent/30 flex items-center justify-center shrink-0">
          <Cpu class="w-5 h-5 text-accent" />
        </div>
        <div class="min-w-0">
          <h1 class="text-lg sm:text-xl font-bold text-white flex flex-wrap items-center gap-2">
            <span>MirrorQuant Public Radar</span>
            <span class="px-2 py-0.5 rounded-full bg-emerald-500/15 text-emerald-400 border border-emerald-500/30 text-[10px] font-mono">
              READ-ONLY · 5S LIVE
            </span>
          </h1>
          <p class="text-xs text-slate-400 truncate">
            Public lead portfolio telemetry &amp; Price-Action / SMC entry analysis
            <span v-if="lastUpdatedAt" class="font-mono text-slate-500"> · {{ lastUpdatedAt }}</span>
          </p>
        </div>
      </div>
      <div class="flex items-center gap-2.5 shrink-0">
        <button @click="fetchPublic" class="px-3 py-2 rounded-xl bg-surface border border-slate-700 hover:border-slate-500 text-xs text-slate-300 flex items-center gap-1.5 transition-colors">
          <RefreshCw class="w-3.5 h-3.5" :class="{ 'animate-spin': loading }" />
          <span>Refresh</span>
        </button>
        <router-link to="/login" class="px-3 py-2 rounded-xl bg-surface border border-slate-800 hover:border-accent/40 text-slate-400 hover:text-accent text-xs font-medium flex items-center gap-1.5 transition-colors">
          <Lock class="w-3.5 h-3.5" />
          <span>Console</span>
        </router-link>
      </div>
    </header>

    <div v-if="!enabled" class="p-10 rounded-2xl bg-surface border border-slate-800 text-center">
      <Globe class="w-8 h-8 text-slate-500 mx-auto mb-3" />
      <h2 class="text-base font-bold text-white">Public Feed Disabled</h2>
      <p class="text-xs text-slate-400 mt-1">The administrator has disabled public monitoring visibility.</p>
    </div>

    <div v-else-if="leads.length === 0" class="p-10 rounded-2xl bg-surface border border-slate-800 text-center text-sm text-slate-400">
      No public leads configured yet.
    </div>

    <div v-else class="space-y-6">
      <div v-for="lead in leads" :key="lead.id" class="rounded-2xl bg-surface border border-slate-800/80 p-4 sm:p-5 space-y-5">
        <!-- Lead Header & KPIs -->
        <div class="flex flex-col md:flex-row md:items-center justify-between gap-4">
          <div class="min-w-0">
            <h2 class="text-lg font-bold text-white flex items-center gap-2">
              <span>{{ lead.name }}</span>
              <span class="px-2 py-0.5 rounded-md bg-accent/10 text-accent border border-accent/20 text-[10px] font-mono uppercase">
                {{ lead.platform }}
              </span>
            </h2>
            <p class="text-xs font-mono text-slate-400 truncate mt-0.5">Portfolio ID: {{ lead.portfolioId }}</p>
          </div>

          <div class="grid grid-cols-2 sm:grid-cols-4 gap-2.5 font-mono text-xs">
            <div class="px-3 py-2 rounded-xl bg-background border border-slate-800">
              <span class="text-slate-500 block text-[10px]">MARGIN BALANCE</span>
              <span class="text-white font-bold">{{ formatUsd(lead.marginBalance) }}</span>
            </div>
            <div class="px-3 py-2 rounded-xl bg-background border border-slate-800">
              <span class="text-slate-500 block text-[10px]">POSITIONS / LEV</span>
              <span class="text-accent font-bold">
                {{ (lead.snapshot?.openPositions || []).length }} · {{ (lead.snapshot?.openPositionsSummary?.totalNotionalLeverage ?? 0).toFixed(2) }}x
              </span>
            </div>
            <div class="px-3 py-2 rounded-xl bg-background border border-slate-800">
              <span class="text-slate-500 block text-[10px]">MARK NOTIONAL</span>
              <span class="text-slate-200 font-bold">{{ formatUsd(lead.snapshot?.openPositionsSummary?.totalMarkNotional) }}</span>
            </div>
            <div class="px-3 py-2 rounded-xl bg-background border border-slate-800">
              <span class="text-slate-500 block text-[10px]">EST. UPNL (ROE)</span>
              <span
                class="font-bold"
                :class="(lead.snapshot?.openPositionsSummary?.totalUnrealizedPnl || 0) >= 0 ? 'text-emerald-400' : 'text-rose-400'"
              >
                {{ (lead.snapshot?.openPositionsSummary?.totalUnrealizedPnl || 0) >= 0 ? "+" : "" }}{{ formatUsd(lead.snapshot?.openPositionsSummary?.totalUnrealizedPnl) }}
                ({{ (lead.snapshot?.openPositionsSummary?.totalMarginRoePct || 0).toFixed(2) }}%)
              </span>
            </div>
          </div>
        </div>

        <!-- Open Positions Table -->
        <div v-if="(lead.snapshot?.openPositions || []).length > 0" class="space-y-4">
          <div class="overflow-x-auto rounded-xl border border-slate-800/80">
            <table class="w-full text-left border-collapse text-xs">
              <thead>
                <tr class="border-b border-slate-800 bg-background/60 text-slate-400 font-mono uppercase">
                  <th class="py-2.5 px-3">Contract</th>
                  <th class="py-2.5 px-3">Side</th>
                  <th class="py-2.5 px-3 text-right">Entry / Current</th>
                  <th class="py-2.5 px-3 text-right">Notional (Lev)</th>
                  <th class="py-2.5 px-3 text-right">Est. uPnL</th>
                  <th class="py-2.5 px-3">PA Verdict</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-slate-800/60">
                <tr v-for="(pos, idx) in lead.snapshot.openPositions" :key="idx" class="hover:bg-slate-800/20">
                  <td class="py-2.5 px-3">
                    <div class="font-bold text-white">{{ pos.symbol }}</div>
                    <div class="text-[10px] font-mono text-slate-500">Held {{ formatHeld(pos.heldMs) }} · Qty {{ pos.qty }}</div>
                  </td>
                  <td class="py-2.5 px-3">
                    <span
                      class="inline-flex items-center gap-1 px-2 py-0.5 rounded font-mono font-semibold text-[11px]"
                      :class="pos.positionSide === 'LONG' ? 'bg-emerald-500/15 text-emerald-400' : 'bg-rose-500/15 text-rose-400'"
                    >
                      <ArrowUpRight v-if="pos.positionSide === 'LONG'" class="w-3 h-3" />
                      <ArrowDownRight v-else class="w-3 h-3" />
                      {{ pos.positionSide }}
                    </span>
                  </td>
                  <td class="py-2.5 px-3 text-right font-mono">
                    <div class="text-slate-200">{{ formatPrice(pos.avgEntry) }}</div>
                    <div class="text-[11px] text-accent">{{ formatPrice(pos.currentPrice) }}</div>
                  </td>
                  <td class="py-2.5 px-3 text-right font-mono text-slate-200">
                    <div>{{ formatUsd(pos.markValue) }}</div>
                    <div class="text-[10px] text-slate-400">{{ pos.notionalLeverage ? `${pos.notionalLeverage}x` : "-" }}</div>
                  </td>
                  <td class="py-2.5 px-3 text-right font-mono font-bold" :class="(pos.uPnL || 0) >= 0 ? 'text-emerald-400' : 'text-rose-400'">
                    <div>{{ (pos.uPnL || 0) >= 0 ? "+" : "" }}{{ formatUsd(pos.uPnL) }}</div>
                    <div class="text-[10px] font-normal opacity-85">{{ (pos.uPnLRoePct ?? pos.uPnLPct ?? 0).toFixed(2) }}%</div>
                  </td>
                  <td class="py-2.5 px-3">
                    <span
                      v-if="pos.analysis?.ok"
                      class="inline-block px-2 py-0.5 rounded-md border font-mono text-[11px]"
                      :class="verdictBadgeClass(pos.analysis.verdictLevel)"
                    >
                      {{ pos.analysis.verdict }}
                    </span>
                    <span v-else class="text-[11px] text-slate-500">-</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <!-- PA / SMC Entry Analysis Cards -->
          <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div
              v-for="(pos, idx) in lead.snapshot.openPositions"
              :key="`pa-${idx}`"
              class="p-4 rounded-xl bg-background/70 border border-slate-800 space-y-3"
            >
              <div class="flex items-center justify-between gap-2">
                <div class="flex items-center gap-2">
                  <ShieldCheck class="w-4 h-4 text-accent shrink-0" />
                  <span class="font-bold text-sm text-white">{{ pos.symbol }} {{ pos.positionSide }}</span>
                  <span class="text-[11px] font-mono text-slate-400">PA / SMC Analysis</span>
                </div>
                <span
                  v-if="pos.analysis?.ok"
                  class="px-2 py-0.5 rounded-md border font-mono text-[10px]"
                  :class="verdictBadgeClass(pos.analysis.verdictLevel)"
                >
                  {{ pos.analysis.trendAlignment }}
                </span>
              </div>

              <div v-if="pos.analysis?.ok" class="space-y-2.5 text-xs">
                <div class="grid grid-cols-2 gap-2 font-mono text-[11px]">
                  <div class="p-2 rounded-lg bg-surface border border-slate-800/80">
                    <span class="text-slate-500 block text-[10px]">1H / 4H STRUCTURE</span>
                    <span class="text-slate-200">{{ pos.analysis.trend1h }} / {{ pos.analysis.trend4h }}</span>
                  </div>
                  <div class="p-2 rounded-lg bg-surface border border-slate-800/80">
                    <span class="text-slate-500 block text-[10px]">ENTRY ZONE (72H)</span>
                    <span class="text-accent">{{ pos.analysis.entryZone }} ({{ pos.analysis.entryRangePct }}%)</span>
                  </div>
                  <div class="p-2 rounded-lg bg-surface border border-slate-800/80">
                    <span class="text-slate-500 block text-[10px]">INVALIDATION / T1 / T2</span>
                    <span class="text-slate-200">
                      {{ formatPrice(pos.analysis.invalidationLevel) }} / {{ formatPrice(pos.analysis.target1) }} / {{ formatPrice(pos.analysis.target2) }}
                    </span>
                  </div>
                  <div class="p-2 rounded-lg bg-surface border border-slate-800/80">
                    <span class="text-slate-500 block text-[10px]">SCALING &amp; R:R</span>
                    <span class="text-slate-200">{{ pos.analysis.scalingStyle }} · {{ pos.analysis.rrFromEntry }}x</span>
                  </div>
                </div>
                <p class="text-[11px] text-slate-400 leading-relaxed break-words">
                  {{ pos.analysis.summary }}
                </p>
              </div>
              <div v-else class="text-xs text-slate-500">PA/SMC kline analysis pending next sync cycle.</div>
            </div>
          </div>
        </div>

        <div v-else class="text-xs text-slate-500 py-3">No active open positions reconstructed.</div>
      </div>
    </div>
  </div>
</template>
