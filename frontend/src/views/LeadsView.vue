<script setup lang="ts">
import { onMounted, ref } from "vue";
import {
  Activity,
  Download,
  Eye,
  Plus,
  RefreshCw,
  Trash2,
  TrendingUp,
  X,
} from "lucide-vue-next";
import { api } from "@/api/client";

const leads = ref<any[]>([]);
const proxies = ref<any[]>([]);
const loading = ref(true);
const syncingId = ref<number | null>(null);

const showModal = ref(false);
const form = ref({
  name: "",
  platform: "binance_bapi",
  portfolioId: "",
  description: "",
  pollIntervalSec: 15,
  proxyId: null as number | null,
  isPublicDashboard: false,
});
const formError = ref("");

// Detail / Snapshot Modal
const detailLead = ref<any>(null);
const detailSnapshot = ref<any>(null);
const detailTab = ref<"positions" | "cycles" | "orders" | "pa">("positions");

async function loadData() {
  loading.value = true;
  try {
    const [lRes, pRes] = await Promise.all([
      api.get<{ items: any[] }>("/api/leads"),
      api.get<{ items: any[] }>("/api/proxies"),
    ]);
    leads.value = lRes.items;
    proxies.value = pRes.items;
  } finally {
    loading.value = false;
  }
}

async function createLead() {
  formError.value = "";
  try {
    await api.post("/api/leads", form.value);
    showModal.value = false;
    form.value = {
      name: "",
      platform: "binance_bapi",
      portfolioId: "",
      description: "",
      pollIntervalSec: 15,
      proxyId: null,
      isPublicDashboard: false,
    };
    await loadData();
  } catch (e: any) {
    formError.value = e.message;
  }
}

async function syncLead(id: number) {
  syncingId.value = id;
  try {
    await api.post(`/api/leads/${id}/sync`);
    await loadData();
  } catch (e) {
    console.error("Sync failed", e);
  } finally {
    syncingId.value = null;
  }
}

async function deleteLead(id: number) {
  if (!confirm("Delete this lead source and all its bindings?")) return;
  await api.delete(`/api/leads/${id}`);
  await loadData();
}

async function openDetail(lead: any) {
  detailLead.value = lead;
  detailTab.value = "positions";
  try {
    const res = await api.get<{ snapshot: any }>(`/api/leads/${lead.id}/snapshot`);
    detailSnapshot.value = res.snapshot;
  } catch {
    detailSnapshot.value = null;
  }
}

function downloadCsv(leadId: number, type: "orders" | "cycles") {
  window.open(`/api/leads/${leadId}/export-csv?export_type=${type}`, "_blank");
}

function formatUsd(val: number) {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(val || 0);
}

onMounted(loadData);
</script>

<template>
  <div class="space-y-6">
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
      <div>
        <h2 class="text-xl sm:text-2xl font-bold text-white tracking-tight">Lead Traders & PA Analytics</h2>
        <p class="text-xs sm:text-sm text-slate-400 mt-0.5">
          Monitor Binance BAPI lead portfolios, reconstruct hidden positions, and replay trade cycles
        </p>
      </div>
      <button
        @click="showModal = true"
        class="px-4 py-2.5 rounded-xl bg-gradient-to-r from-accent to-brand text-slate-950 font-semibold text-xs sm:text-sm flex items-center gap-2 hover:opacity-90 shadow-lg shadow-accent/20"
      >
        <Plus class="w-4 h-4" />
        <span>Add Lead Portfolio</span>
      </button>
    </div>

    <!-- Leads Grid -->
    <div v-if="loading && leads.length === 0" class="text-center py-12 text-slate-400 text-sm">
      Loading lead sources...
    </div>

    <div v-else-if="leads.length === 0" class="p-10 rounded-2xl bg-surface border border-slate-800 text-center">
      <p class="text-slate-300 font-medium mb-1">No lead portfolios configured</p>
      <p class="text-xs text-slate-500 mb-4">Add a Binance copy-trading portfolio ID to begin telemetry collection.</p>
      <button @click="showModal = true" class="px-4 py-2 rounded-xl bg-accent/15 text-accent border border-accent/30 text-xs font-semibold">
        + Add First Lead
      </button>
    </div>

    <div v-else class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-5">
      <div
        v-for="lead in leads"
        :key="lead.id"
        class="rounded-2xl bg-surface border border-slate-800/80 p-5 flex flex-col justify-between space-y-4 hover:border-slate-700 transition-all"
      >
        <div>
          <div class="flex items-start justify-between gap-3">
            <div>
              <div class="flex items-center gap-2">
                <h3 class="font-bold text-base text-white">{{ lead.name }}</h3>
                <span
                  class="px-2 py-0.5 rounded-full text-[10px] font-mono border"
                  :class="
                    lead.status === 'running'
                      ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30'
                      : lead.status === 'error'
                      ? 'bg-rose-500/10 text-rose-400 border-rose-500/30'
                      : 'bg-slate-700/40 text-slate-400 border-slate-700'
                  "
                >
                  {{ lead.status.toUpperCase() }}
                </span>
              </div>
              <p class="text-xs text-slate-400 font-mono mt-0.5">ID: {{ lead.portfolioId }}</p>
            </div>
            <span
              v-if="lead.isPublicDashboard"
              class="px-2 py-0.5 rounded-md bg-cyan-500/10 text-cyan-400 border border-cyan-500/20 text-[10px] font-mono"
            >
              PUBLIC
            </span>
          </div>

          <div class="grid grid-cols-2 gap-3 mt-4 p-3 rounded-xl bg-background/60 border border-slate-800/80 text-xs">
            <div>
              <span class="text-slate-500 block text-[11px]">Margin Balance</span>
              <span class="font-mono font-semibold text-slate-100">{{ formatUsd(lead.marginBalance) }}</span>
            </div>
            <div>
              <span class="text-slate-500 block text-[11px]">Open Positions</span>
              <span class="font-mono font-semibold text-accent">{{ lead.openPositionsCount }} Active</span>
            </div>
            <div>
              <span class="text-slate-500 block text-[11px]">Est. uPnL</span>
              <span
                class="font-mono font-semibold"
                :class="lead.openUnrealizedPnl >= 0 ? 'text-emerald-400' : 'text-rose-400'"
              >
                {{ lead.openUnrealizedPnl >= 0 ? "+" : "" }}{{ formatUsd(lead.openUnrealizedPnl) }}
              </span>
            </div>
            <div>
              <span class="text-slate-500 block text-[11px]">Poll Interval</span>
              <span class="font-mono text-slate-300">{{ lead.pollIntervalSec }}s</span>
            </div>
          </div>

          <p v-if="lead.lastError" class="mt-3 text-[11px] text-rose-400 bg-rose-500/10 p-2 rounded-lg border border-rose-500/20 break-words">
            {{ lead.lastError }}
          </p>
        </div>

        <div class="flex items-center justify-between pt-3 border-t border-slate-800/80">
          <div class="flex items-center gap-2">
            <button
              @click="syncLead(lead.id)"
              :disabled="syncingId === lead.id"
              class="px-3 py-1.5 rounded-lg bg-surface2 hover:bg-slate-700 text-xs font-medium text-slate-200 flex items-center gap-1.5 transition-colors"
            >
              <RefreshCw class="w-3.5 h-3.5" :class="{ 'animate-spin': syncingId === lead.id }" />
              <span>{{ syncingId === lead.id ? "Syncing..." : "Sync Now" }}</span>
            </button>
            <button
              @click="openDetail(lead)"
              class="px-3 py-1.5 rounded-lg bg-accent/15 hover:bg-accent/25 text-accent text-xs font-medium flex items-center gap-1.5 transition-colors"
            >
              <Eye class="w-3.5 h-3.5" />
              <span>Inspect & Cycles</span>
            </button>
          </div>
          <button
            @click="deleteLead(lead.id)"
            class="p-1.5 rounded-lg text-slate-500 hover:text-rose-400 hover:bg-rose-500/10 transition-colors"
          >
            <Trash2 class="w-4 h-4" />
          </button>
        </div>
      </div>
    </div>

    <!-- Create Lead Modal -->
    <div v-if="showModal" class="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4">
      <div class="w-full max-w-lg bg-surface border border-slate-800 rounded-2xl p-6 shadow-2xl">
        <div class="flex items-center justify-between mb-5">
          <h3 class="text-lg font-bold text-white">Add Binance Lead Portfolio</h3>
          <button @click="showModal = false" class="text-slate-400 hover:text-white"><X class="w-5 h-5" /></button>
        </div>
        <form @submit.prevent="createLead" class="space-y-4">
          <div v-if="formError" class="p-3 rounded-xl bg-rose-500/10 text-rose-400 text-xs">{{ formError }}</div>
          <div>
            <label class="block text-xs text-slate-400 mb-1">Display Name</label>
            <input v-model="form.name" required placeholder="e.g. Alpha Quant Lead" class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white" />
          </div>
          <div>
            <label class="block text-xs text-slate-400 mb-1">Binance Portfolio ID</label>
            <input v-model="form.portfolioId" required placeholder="e.g. 1234567890123456789" class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm font-mono text-white" />
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="block text-xs text-slate-400 mb-1">Poll Interval (sec)</label>
              <input v-model.number="form.pollIntervalSec" type="number" min="5" max="3600" class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white" />
            </div>
            <div>
              <label class="block text-xs text-slate-400 mb-1">Proxy Node</label>
              <select v-model="form.proxyId" class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white">
                <option :value="null">Auto / Direct</option>
                <option v-for="p in proxies" :key="p.id" :value="p.id">{{ p.name }} ({{ p.region }})</option>
              </select>
            </div>
          </div>
          <label class="flex items-center gap-2 text-xs text-slate-300 cursor-pointer">
            <input v-model="form.isPublicDashboard" type="checkbox" class="rounded bg-background border-slate-600 text-accent" />
            <span>Expose anonymized snapshot on Public Monitoring Feed (`/public`)</span>
          </label>
          <div class="flex justify-end gap-3 pt-2">
            <button type="button" @click="showModal = false" class="px-4 py-2 rounded-xl bg-surface2 text-slate-300 text-xs font-medium">Cancel</button>
            <button type="submit" class="px-4 py-2 rounded-xl bg-accent text-slate-950 font-semibold text-xs">Save Lead</button>
          </div>
        </form>
      </div>
    </div>

    <!-- Inspect / Trade Cycles Modal -->
    <div v-if="detailLead" class="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-3 sm:p-6">
      <div class="w-full max-w-5xl max-h-[90vh] bg-surface border border-slate-800 rounded-2xl flex flex-col shadow-2xl overflow-hidden">
        <div class="px-6 py-4 border-b border-slate-800 flex items-center justify-between gap-4">
          <div>
            <h3 class="text-lg font-bold text-white">{{ detailLead.name }} · Quantitative Review</h3>
            <p class="text-xs text-slate-400 font-mono">
              Portfolio: {{ detailLead.portfolioId }} · Hidden Positions: {{ detailLead.positionShow === false ? "RECONSTRUCTED" : "PUBLIC" }}
            </p>
          </div>
          <div class="flex items-center gap-2">
            <button
              @click="downloadCsv(detailLead.id, 'cycles')"
              class="px-3 py-1.5 rounded-lg bg-surface2 hover:bg-slate-700 text-xs text-slate-200 flex items-center gap-1.5"
            >
              <Download class="w-3.5 h-3.5 text-accent" />
              <span>Export Cycles CSV</span>
            </button>
            <button
              @click="downloadCsv(detailLead.id, 'orders')"
              class="px-3 py-1.5 rounded-lg bg-surface2 hover:bg-slate-700 text-xs text-slate-200 flex items-center gap-1.5"
            >
              <Download class="w-3.5 h-3.5 text-brand" />
              <span>Export Orders CSV</span>
            </button>
            <button @click="detailLead = null" class="p-1.5 text-slate-400 hover:text-white"><X class="w-5 h-5" /></button>
          </div>
        </div>

        <!-- Tabs -->
        <div class="px-6 pt-3 border-b border-slate-800 flex gap-4 text-xs font-medium">
          <button
            v-for="tab in [
              { id: 'positions', label: 'Open Positions & PA' },
              { id: 'cycles', label: 'Trade Cycles (Replay)' },
              { id: 'orders', label: 'Raw Order Feed' },
            ]"
            :key="tab.id"
            @click="detailTab = tab.id as any"
            class="pb-3 border-b-2 transition-colors"
            :class="detailTab === tab.id ? 'border-accent text-accent' : 'border-transparent text-slate-400 hover:text-slate-200'"
          >
            {{ tab.label }}
          </button>
        </div>

        <div class="p-6 overflow-y-auto flex-1">
          <div v-if="!detailSnapshot" class="text-center py-12 text-slate-500 text-sm">
            No snapshot cached yet. Click "Sync Now" on the card first.
          </div>

          <!-- Tab 1: Open Positions + PA -->
          <div v-else-if="detailTab === 'positions'" class="space-y-4">
            <div v-if="!(detailSnapshot.openPositions || []).length" class="text-center py-8 text-slate-500 text-xs">
              No active open positions reconstructed.
            </div>
            <div
              v-for="(pos, idx) in detailSnapshot.openPositions || []"
              :key="idx"
              class="p-4 rounded-xl bg-background/70 border border-slate-800 space-y-3"
            >
              <div class="flex flex-wrap items-center justify-between gap-2">
                <div class="flex items-center gap-2.5">
                  <span class="font-bold text-sm text-white">{{ pos.symbol }}</span>
                  <span
                    class="px-2 py-0.5 rounded text-[11px] font-mono font-bold"
                    :class="pos.positionSide === 'LONG' ? 'bg-emerald-500/15 text-emerald-400' : 'bg-rose-500/15 text-rose-400'"
                  >
                    {{ pos.positionSide }}
                  </span>
                  <span class="text-xs font-mono text-slate-400">Qty: {{ pos.qty }} · Entry: {{ pos.avgEntry }} · Mark: {{ pos.currentPrice }}</span>
                </div>
                <div class="font-mono text-sm font-bold" :class="pos.uPnL >= 0 ? 'text-emerald-400' : 'text-rose-400'">
                  {{ pos.uPnL >= 0 ? "+" : "" }}{{ formatUsd(pos.uPnL) }} ({{ pos.roePct }}%)
                </div>
              </div>

              <!-- PA & SMC Technical Metrics -->
              <div v-if="pos.analysis?.ok" class="grid grid-cols-1 md:grid-cols-3 gap-3 pt-2 border-t border-slate-800/80 text-xs">
                <div class="p-2.5 rounded-lg bg-surface/80 border border-slate-800">
                  <span class="text-[10px] text-slate-500 uppercase font-mono block">1H / 4H Trend & RSI</span>
                  <span class="text-accent font-mono">{{ pos.analysis.tf1h?.trend }} · RSI14: {{ pos.analysis.tf1h?.rsi14 }}</span>
                </div>
                <div class="p-2.5 rounded-lg bg-surface/80 border border-slate-800">
                  <span class="text-[10px] text-slate-500 uppercase font-mono block">72H Swing Zone</span>
                  <span class="text-slate-200 font-mono">
                    {{ pos.analysis.tf1h?.swingZone }} ({{ pos.analysis.tf1h?.swingPosPct }}%)
                  </span>
                </div>
                <div class="p-2.5 rounded-lg bg-surface/80 border border-slate-800">
                  <span class="text-[10px] text-slate-500 uppercase font-mono block">Invalidation & Targets</span>
                  <span class="text-slate-200 font-mono">
                    SL Ref: {{ pos.analysis.levels?.invalidation }} · T1: {{ pos.analysis.levels?.t1 }}
                  </span>
                </div>
              </div>
            </div>
          </div>

          <!-- Tab 2: Trade Cycles -->
          <div v-else-if="detailTab === 'cycles'" class="overflow-x-auto">
            <table class="w-full text-left border-collapse text-xs">
              <thead>
                <tr class="border-b border-slate-800 text-slate-400 font-mono uppercase">
                  <th class="py-2.5 px-3">Cycle ID</th>
                  <th class="py-2.5 px-3">Status</th>
                  <th class="py-2.5 px-3">Entries / Scale-Ins</th>
                  <th class="py-2.5 px-3">Duration</th>
                  <th class="py-2.5 px-3 text-right">Avg Entry → Exit</th>
                  <th class="py-2.5 px-3 text-right">Realized PnL</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-slate-800/60">
                <tr v-for="c in detailSnapshot.tradeCycles || []" :key="c.cycleId" class="hover:bg-slate-800/30">
                  <td class="py-2.5 px-3 font-mono font-semibold text-white">{{ c.cycleId }}</td>
                  <td class="py-2.5 px-3">
                    <span
                      class="px-2 py-0.5 rounded text-[10px] font-mono"
                      :class="c.status === 'OPEN' ? 'bg-accent/15 text-accent' : 'bg-slate-700/50 text-slate-300'"
                    >
                      {{ c.status }}
                    </span>
                  </td>
                  <td class="py-2.5 px-3 font-mono text-slate-300">
                    {{ c.entryCount }} in (+{{ c.scaleInCount }} adds) / {{ c.exitCount }} out
                  </td>
                  <td class="py-2.5 px-3 font-mono text-slate-400">{{ c.durationFormatted }}</td>
                  <td class="py-2.5 px-3 text-right font-mono text-slate-300">
                    {{ c.avgEntryPrice }} → {{ c.avgExitPrice || "HOLDING" }}
                  </td>
                  <td
                    class="py-2.5 px-3 text-right font-mono font-bold"
                    :class="c.realizedPnl >= 0 ? 'text-emerald-400' : 'text-rose-400'"
                  >
                    {{ c.realizedPnl >= 0 ? "+" : "" }}{{ formatUsd(c.realizedPnl) }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <!-- Tab 3: Raw Orders -->
          <div v-else class="overflow-x-auto">
            <table class="w-full text-left border-collapse text-xs">
              <thead>
                <tr class="border-b border-slate-800 text-slate-400 font-mono uppercase">
                  <th class="py-2.5 px-3">Time</th>
                  <th class="py-2.5 px-3">Symbol</th>
                  <th class="py-2.5 px-3">Action</th>
                  <th class="py-2.5 px-3 text-right">Qty</th>
                  <th class="py-2.5 px-3 text-right">Avg Price</th>
                  <th class="py-2.5 px-3 text-right">PnL</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-slate-800/60">
                <tr v-for="(o, i) in detailSnapshot.orders || []" :key="i" class="hover:bg-slate-800/30">
                  <td class="py-2.5 px-3 font-mono text-slate-400">{{ new Date(o.time).toLocaleString() }}</td>
                  <td class="py-2.5 px-3 font-semibold text-white">{{ o.symbol }}</td>
                  <td class="py-2.5 px-3 font-mono text-accent">{{ o.dir }}</td>
                  <td class="py-2.5 px-3 text-right font-mono text-slate-200">{{ o.qty }}</td>
                  <td class="py-2.5 px-3 text-right font-mono text-slate-200">{{ o.avgPrice }}</td>
                  <td class="py-2.5 px-3 text-right font-mono" :class="(o.pnl || 0) >= 0 ? 'text-emerald-400' : 'text-rose-400'">
                    {{ o.pnl ?? 0 }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
