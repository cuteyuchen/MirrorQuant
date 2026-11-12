<script setup lang="ts">
import { onMounted, ref } from "vue";
import {
  AlertTriangle,
  ArrowRight,
  Layers,
  Plus,
  Power,
  RotateCcw,
  Trash2,
  X,
} from "lucide-vue-next";
import { api } from "@/api/client";

const bindings = ref<any[]>([]);
const leads = ref<any[]>([]);
const accounts = ref<any[]>([]);
const globalKillSwitch = ref(false);
const loading = ref(true);

const showModal = ref(false);
const form = ref({
  name: "",
  leadId: null as number | null,
  accountId: null as number | null,
  riskScale: 1.0,
  maxTotalLeverage: 8.0,
  maxSingleLeverage: 4.0,
  maxInstrumentLeverage: 20,
  maxDrawdownPct: 30.0,
  maxSlippagePct: 1.0,
  iocOffsetPct: 0.5,
  orderMode: "ioc_then_market",
});
const formError = ref("");

async function loadData() {
  loading.value = true;
  try {
    const [bRes, lRes, aRes] = await Promise.all([
      api.get<{ items: any[]; globalKillSwitch: boolean }>("/api/bindings"),
      api.get<{ items: any[] }>("/api/leads"),
      api.get<{ items: any[] }>("/api/accounts"),
    ]);
    bindings.value = bRes.items;
    globalKillSwitch.value = bRes.globalKillSwitch;
    leads.value = lRes.items;
    accounts.value = aRes.items;
  } finally {
    loading.value = false;
  }
}

async function createBinding() {
  formError.value = "";
  if (!form.value.leadId || !form.value.accountId) {
    formError.value = "Please select both a Lead and a Follower Account.";
    return;
  }
  try {
    await api.post("/api/bindings", form.value);
    showModal.value = false;
    form.value.name = "";
    await loadData();
  } catch (e: any) {
    formError.value = e.message;
  }
}

async function toggleBinding(id: number) {
  await api.post(`/api/bindings/${id}/toggle`);
  await loadData();
}

async function resetBreaker(id: number) {
  await api.post(`/api/bindings/${id}/reset-breaker`);
  await loadData();
}

async function resetDebt(id: number) {
  await api.post(`/api/bindings/${id}/reset-debt`);
  await loadData();
}

async function deleteBinding(id: number) {
  if (!confirm("Delete this copy-trading binding?")) return;
  await api.delete(`/api/bindings/${id}`);
  await loadData();
}

onMounted(loadData);
</script>

<template>
  <div class="space-y-6">
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
      <div>
        <h2 class="text-xl sm:text-2xl font-bold text-white tracking-tight">N:M Copy Routing Matrix</h2>
        <p class="text-xs sm:text-sm text-slate-400 mt-0.5">
          Bind lead portfolios to follower accounts with isolated sizing scales, leverage caps, and circuit breakers
        </p>
      </div>
      <button
        @click="showModal = true"
        class="px-4 py-2.5 rounded-xl bg-gradient-to-r from-accent to-brand text-slate-950 font-semibold text-xs sm:text-sm flex items-center gap-2 hover:opacity-90 shadow-lg shadow-accent/20"
      >
        <Plus class="w-4 h-4" />
        <span>New Copy Binding</span>
      </button>
    </div>

    <div v-if="loading && bindings.length === 0" class="text-center py-12 text-slate-400 text-sm">
      Loading routing matrix...
    </div>

    <div v-else-if="bindings.length === 0" class="p-10 rounded-2xl bg-surface border border-slate-800 text-center">
      <p class="text-slate-300 font-medium mb-1">No copy-trading bindings configured</p>
      <p class="text-xs text-slate-500 mb-4">Bind a monitored Lead source to an OKX or Binance account to start automated mirroring.</p>
      <button @click="showModal = true" class="px-4 py-2 rounded-xl bg-accent/15 text-accent border border-accent/30 text-xs font-semibold">
        + Create First Binding
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
                {{ b.isEnabled ? "ACTIVE" : "PAUSED" }}
              </span>
              <span v-if="b.baselineSeeded" class="px-2 py-0.5 rounded bg-slate-800 text-slate-400 text-[10px] font-mono">
                BASELINE SEEDED
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
              @click="toggleBinding(b.id)"
              class="px-3 py-1.5 rounded-lg text-xs font-medium flex items-center gap-1.5 border transition-colors"
              :class="
                b.isEnabled
                  ? 'bg-amber-500/10 border-amber-500/30 text-amber-300 hover:bg-amber-500/20'
                  : 'bg-emerald-500/10 border-emerald-500/30 text-emerald-300 hover:bg-emerald-500/20'
              "
            >
              <Power class="w-3.5 h-3.5" />
              <span>{{ b.isEnabled ? "Pause" : "Enable" }}</span>
            </button>

            <button
              v-if="b.pendingDebtUsd > 0"
              @click="resetDebt(b.id)"
              class="px-3 py-1.5 rounded-lg bg-surface2 hover:bg-slate-700 text-xs text-amber-300 flex items-center gap-1.5"
            >
              <RotateCcw class="w-3.5 h-3.5" />
              <span>Clear Debt (${{ b.pendingDebtUsd }})</span>
            </button>

            <button
              v-if="b.circuitBreakerTripped"
              @click="resetBreaker(b.id)"
              class="px-3 py-1.5 rounded-lg bg-rose-500 text-white text-xs font-semibold flex items-center gap-1.5 hover:bg-rose-600"
            >
              <AlertTriangle class="w-3.5 h-3.5" />
              <span>Reset Breaker</span>
            </button>

            <button
              @click="deleteBinding(b.id)"
              class="p-2 rounded-lg text-slate-500 hover:text-rose-400 hover:bg-rose-500/10"
            >
              <Trash2 class="w-4 h-4" />
            </button>
          </div>
        </div>

        <!-- Risk Parameters Grid -->
        <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3 mt-4 p-3 rounded-xl bg-background/60 border border-slate-800/80 text-xs font-mono">
          <div>
            <span class="text-[10px] text-slate-500 block">RISK SCALE</span>
            <span class="text-accent font-bold">{{ b.riskScale }}x</span>
          </div>
          <div>
            <span class="text-[10px] text-slate-500 block">MAX LEV (SYM / TOT)</span>
            <span class="text-slate-200">{{ b.maxSingleLeverage }}x / {{ b.maxTotalLeverage }}x</span>
          </div>
          <div>
            <span class="text-[10px] text-slate-500 block">DRAWDOWN BREAKER</span>
            <span class="text-slate-200">{{ b.maxDrawdownPct }}%</span>
          </div>
          <div>
            <span class="text-[10px] text-slate-500 block">SLIPPAGE GUARD</span>
            <span class="text-slate-200">{{ b.maxSlippagePct }}%</span>
          </div>
          <div>
            <span class="text-[10px] text-slate-500 block">ORDER POLICY</span>
            <span class="text-slate-200 uppercase">{{ b.orderMode }}</span>
          </div>
          <div>
            <span class="text-[10px] text-slate-500 block">PENDING DEBT</span>
            <span :class="b.pendingDebtUsd > 0 ? 'text-amber-400' : 'text-slate-400'">${{ b.pendingDebtUsd }}</span>
          </div>
        </div>

        <div v-if="b.circuitBreakerTripped" class="mt-3 p-2.5 rounded-lg bg-rose-500/15 border border-rose-500/30 text-xs text-rose-300 flex items-center gap-2">
          <AlertTriangle class="w-4 h-4 shrink-0" />
          <span>Circuit Breaker Tripped: {{ b.circuitBreakerReason }} (Only reduce/close orders allowed)</span>
        </div>
      </div>
    </div>

    <!-- Modal -->
    <div v-if="showModal" class="fixed inset-0 z-50 bg-black/75 backdrop-blur-sm flex items-center justify-center p-4">
      <div class="w-full max-w-xl bg-surface border border-slate-800 rounded-2xl p-6 shadow-2xl max-h-[90vh] overflow-y-auto">
        <div class="flex items-center justify-between mb-5">
          <h3 class="text-lg font-bold text-white">Create Copy Binding</h3>
          <button @click="showModal = false" class="text-slate-400 hover:text-white"><X class="w-5 h-5" /></button>
        </div>
        <form @submit.prevent="createBinding" class="space-y-4">
          <div v-if="formError" class="p-3 rounded-xl bg-rose-500/10 text-rose-400 text-xs">{{ formError }}</div>
          <div>
            <label class="block text-xs text-slate-400 mb-1">Binding Label</label>
            <input v-model="form.name" required placeholder="e.g. Alpha -> OKX Demo 1x" class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white" />
          </div>
          <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label class="block text-xs text-slate-400 mb-1">Lead Source</label>
              <select v-model="form.leadId" required class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white">
                <option :value="null" disabled>Select Lead...</option>
                <option v-for="l in leads" :key="l.id" :value="l.id">{{ l.name }}</option>
              </select>
            </div>
            <div>
              <label class="block text-xs text-slate-400 mb-1">Follower Account</label>
              <select v-model="form.accountId" required class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white">
                <option :value="null" disabled>Select Account...</option>
                <option v-for="a in accounts" :key="a.id" :value="a.id">{{ a.name }} ({{ a.exchange }} {{ a.environment }})</option>
              </select>
            </div>
          </div>
          <div class="grid grid-cols-2 sm:grid-cols-3 gap-3">
            <div>
              <label class="block text-xs text-slate-400 mb-1">Risk Scale (x)</label>
              <input v-model.number="form.riskScale" type="number" step="0.1" min="0.1" max="20" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white" />
            </div>
            <div>
              <label class="block text-xs text-slate-400 mb-1">Max Single Lev</label>
              <input v-model.number="form.maxSingleLeverage" type="number" step="0.5" min="1" max="100" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white" />
            </div>
            <div>
              <label class="block text-xs text-slate-400 mb-1">Max Total Lev</label>
              <input v-model.number="form.maxTotalLeverage" type="number" step="0.5" min="1" max="100" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white" />
            </div>
            <div>
              <label class="block text-xs text-slate-400 mb-1">Max Drawdown %</label>
              <input v-model.number="form.maxDrawdownPct" type="number" step="1" min="1" max="100" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white" />
            </div>
            <div>
              <label class="block text-xs text-slate-400 mb-1">Max Slippage %</label>
              <input v-model.number="form.maxSlippagePct" type="number" step="0.1" min="0.1" max="50" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white" />
            </div>
            <div>
              <label class="block text-xs text-slate-400 mb-1">Order Mode</label>
              <select v-model="form.orderMode" class="w-full px-3 py-2 rounded-xl bg-background border border-slate-700 text-xs text-white">
                <option value="ioc_then_market">IOC → Market</option>
                <option value="ioc_only">IOC Only</option>
                <option value="market_only">Market Only</option>
              </select>
            </div>
          </div>
          <div class="flex justify-end gap-3 pt-2">
            <button type="button" @click="showModal = false" class="px-4 py-2 rounded-xl bg-surface2 text-slate-300 text-xs font-medium">Cancel</button>
            <button type="submit" class="px-4 py-2 rounded-xl bg-accent text-slate-950 font-semibold text-xs">Create Binding</button>
          </div>
        </form>
      </div>
    </div>
  </div>
</template>
