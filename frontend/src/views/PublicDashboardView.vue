<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { ArrowDownRight, ArrowUpRight, Cpu, Globe, RefreshCw } from "lucide-vue-next";
import { api } from "@/api/client";

const loading = ref(true);
const enabled = ref(true);
const leads = ref<any[]>([]);
let timer: any = null;

async function fetchPublic() {
  try {
    const res = await api.get<{ enabled: boolean; leads: any[] }>("/api/dashboard/public");
    enabled.value = res.enabled;
    leads.value = res.leads;
  } finally {
    loading.value = false;
  }
}

function formatUsd(val: number) {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(val || 0);
}

onMounted(() => {
  fetchPublic();
  timer = setInterval(fetchPublic, 10000);
});

onUnmounted(() => {
  if (timer) clearInterval(timer);
});
</script>

<template>
  <div class="min-h-screen bg-background text-slate-100 p-4 md:p-8 max-w-6xl mx-auto space-y-6">
    <!-- Top Bar -->
    <header class="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-5 border-b border-slate-800">
      <div class="flex items-center gap-3">
        <div class="w-10 h-10 rounded-xl bg-accent/10 border border-accent/30 flex items-center justify-center">
          <Cpu class="w-5 h-5 text-accent" />
        </div>
        <div>
          <h1 class="text-xl font-bold text-white flex items-center gap-2">
            <span>MirrorQuant Public Radar</span>
            <span class="px-2 py-0.5 rounded-full bg-emerald-500/15 text-emerald-400 border border-emerald-500/30 text-[10px] font-mono">
              READ-ONLY
            </span>
          </h1>
          <p class="text-xs text-slate-400">Public lead portfolio telemetry & reconstructed Price-Action analysis</p>
        </div>
      </div>
      <div class="flex items-center gap-2.5">
        <button @click="fetchPublic" class="px-3 py-2 rounded-xl bg-surface border border-slate-700 text-xs text-slate-300 flex items-center gap-1.5">
          <RefreshCw class="w-3.5 h-3.5" :class="{ 'animate-spin': loading }" />
          <span>Refresh</span>
        </button>
        <router-link to="/dashboard" class="px-4 py-2 rounded-xl bg-accent text-slate-950 font-semibold text-xs">
          Admin Terminal →
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
      <div v-for="lead in leads" :key="lead.id" class="rounded-2xl bg-surface border border-slate-800/80 p-5 space-y-4">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 class="text-lg font-bold text-white">{{ lead.name }}</h2>
            <p class="text-xs font-mono text-slate-400">Portfolio ID: {{ lead.portfolioId }}</p>
          </div>
          <div class="flex gap-4 font-mono text-xs">
            <div class="px-3 py-2 rounded-xl bg-background border border-slate-800">
              <span class="text-slate-500 block text-[10px]">MARGIN BALANCE</span>
              <span class="text-white font-bold">{{ formatUsd(lead.marginBalance) }}</span>
            </div>
            <div class="px-3 py-2 rounded-xl bg-background border border-slate-800">
              <span class="text-slate-500 block text-[10px]">OPEN POSITIONS</span>
              <span class="text-accent font-bold">{{ (lead.snapshot?.openPositions || []).length }}</span>
            </div>
          </div>
        </div>

        <div v-if="(lead.snapshot?.openPositions || []).length > 0" class="overflow-x-auto">
          <table class="w-full text-left border-collapse text-xs">
            <thead>
              <tr class="border-b border-slate-800 text-slate-400 font-mono uppercase">
                <th class="py-2.5 px-3">Contract</th>
                <th class="py-2.5 px-3">Side</th>
                <th class="py-2.5 px-3 text-right">Entry / Mark</th>
                <th class="py-2.5 px-3 text-right">Notional</th>
                <th class="py-2.5 px-3 text-right">Est. uPnL</th>
                <th class="py-2.5 px-3">PA Reference</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-800/60">
              <tr v-for="(pos, idx) in lead.snapshot.openPositions" :key="idx">
                <td class="py-2.5 px-3 font-bold text-white">{{ pos.symbol }}</td>
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
                <td class="py-2.5 px-3 text-right font-mono text-slate-200">
                  {{ pos.avgEntry }} / {{ pos.currentPrice }}
                </td>
                <td class="py-2.5 px-3 text-right font-mono text-slate-200">{{ formatUsd(pos.markValue) }}</td>
                <td class="py-2.5 px-3 text-right font-mono font-bold" :class="pos.uPnL >= 0 ? 'text-emerald-400' : 'text-rose-400'">
                  {{ pos.uPnL >= 0 ? "+" : "" }}{{ formatUsd(pos.uPnL) }} ({{ pos.roePct }}%)
                </td>
                <td class="py-2.5 px-3 font-mono text-[11px] text-accent">
                  {{ pos.analysis?.ok ? pos.analysis.tf1h?.trend : "-" }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-else class="text-xs text-slate-500 py-3">No active open positions reconstructed.</div>
      </div>
    </div>
  </div>
</template>
