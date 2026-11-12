<script setup lang="ts">
import { onActivated, onMounted, ref } from "vue";
import { Download, Eye, Plus, RefreshCw, Trash2, X } from "lucide-vue-next";
import { api } from "@/api/client";
import { useDataCacheStore } from "@/stores/dataCache";
import { useI18n } from "@/i18n";
import LeadTelemetryPanel from "@/components/LeadTelemetryPanel.vue";

defineOptions({ name: "LeadsView" });

const cache = useDataCacheStore();
const { t } = useI18n();

const leads = ref<any[]>(cache.leads?.data || []);
const proxies = ref<any[]>(cache.proxies?.data || []);
const loading = ref(!cache.leads);
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

const detailLead = ref<any>(null);
const detailSnapshot = ref<any>(null);

async function loadData(force = false) {
  if (!cache.leads) loading.value = true;
  try {
    const [lItems, pItems] = await Promise.all([
      cache.fetchLeads(force, 12000),
      cache.fetchProxies(force, 20000),
    ]);
    leads.value = lItems;
    proxies.value = pItems;
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
    await loadData(true);
  } catch (e: any) {
    formError.value = e.message;
  }
}

async function syncLead(id: number) {
  syncingId.value = id;
  try {
    await api.post(`/api/leads/${id}/sync`);
    await loadData(true);
    if (detailLead.value?.id === id) {
      detailSnapshot.value = await cache.fetchLeadSnapshot(id, true);
    }
  } catch (e) {
    console.error("Sync failed", e);
  } finally {
    syncingId.value = null;
  }
}

async function deleteLead(id: number) {
  if (!confirm(t("leads.deleteConfirm"))) return;
  await api.delete(`/api/leads/${id}`);
  await loadData(true);
}

async function openDetail(lead: any) {
  detailLead.value = lead;
  const cached = cache.leadSnapshots[lead.id]?.data;
  if (cached) detailSnapshot.value = cached;
  try {
    detailSnapshot.value = await cache.fetchLeadSnapshot(lead.id, !cached, 10000);
  } catch {
    if (!cached) detailSnapshot.value = null;
  }
}

function downloadCsv(leadId: number, type: "orders" | "cycles") {
  window.open(api.resolveUrl(`/api/leads/${leadId}/export-csv?export_type=${type}`), "_blank");
}

function formatUsd(val: number) {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(val || 0);
}

onMounted(() => loadData(false));
onActivated(() => {
  if (cache.leads) leads.value = cache.leads.data;
  if (cache.proxies) proxies.value = cache.proxies.data;
  loadData(false);
});
</script>

<template>
  <div class="space-y-6">
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
      <div>
        <h2 class="text-xl sm:text-2xl font-bold text-white tracking-tight">{{ t("leads.title") }}</h2>
        <p class="text-xs sm:text-sm text-slate-400 mt-0.5">{{ t("leads.subtitle") }}</p>
      </div>
      <button
        @click="showModal = true"
        class="px-4 py-2.5 rounded-xl bg-gradient-to-r from-accent to-brand text-slate-950 font-semibold text-xs sm:text-sm flex items-center gap-2 hover:opacity-90 shadow-lg shadow-accent/20"
      >
        <Plus class="w-4 h-4" />
        <span>{{ t("leads.addBtn") }}</span>
      </button>
    </div>

    <div v-if="loading && leads.length === 0" class="text-center py-12 text-slate-400 text-sm">
      {{ t("leads.loading") }}
    </div>

    <div v-else-if="leads.length === 0" class="p-10 rounded-2xl bg-surface border border-slate-800 text-center">
      <p class="text-slate-300 font-medium mb-1">{{ t("leads.emptyTitle") }}</p>
      <p class="text-xs text-slate-500 mb-4">{{ t("leads.emptyDesc") }}</p>
      <button @click="showModal = true" class="px-4 py-2 rounded-xl bg-accent/15 text-accent border border-accent/30 text-xs font-semibold">
        {{ t("leads.addFirst") }}
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
              {{ t("leads.publicTag") }}
            </span>
          </div>

          <div class="grid grid-cols-2 gap-3 mt-4 p-3 rounded-xl bg-background/60 border border-slate-800/80 text-xs">
            <div>
              <span class="text-slate-500 block text-[11px]">{{ t("leads.marginBalance") }}</span>
              <span class="font-mono font-semibold text-slate-100">{{ formatUsd(lead.marginBalance) }}</span>
            </div>
            <div>
              <span class="text-slate-500 block text-[11px]">{{ t("leads.openPositions") }}</span>
              <span class="font-mono font-semibold text-accent">{{ lead.openPositionsCount }} {{ t("leads.activeSuffix") }}</span>
            </div>
            <div>
              <span class="text-slate-500 block text-[11px]">{{ t("leads.estUpnl") }}</span>
              <span
                class="font-mono font-semibold"
                :class="lead.openUnrealizedPnl >= 0 ? 'text-emerald-400' : 'text-rose-400'"
              >
                {{ lead.openUnrealizedPnl >= 0 ? "+" : "" }}{{ formatUsd(lead.openUnrealizedPnl) }}
              </span>
            </div>
            <div>
              <span class="text-slate-500 block text-[11px]">{{ t("leads.pollInterval") }}</span>
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
              <span>{{ syncingId === lead.id ? t("leads.syncing") : t("leads.syncNow") }}</span>
            </button>
            <button
              @click="openDetail(lead)"
              class="px-3 py-1.5 rounded-lg bg-accent/15 hover:bg-accent/25 text-accent text-xs font-medium flex items-center gap-1.5 transition-colors"
            >
              <Eye class="w-3.5 h-3.5" />
              <span>{{ t("leads.inspectBtn") }}</span>
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
          <h3 class="text-lg font-bold text-white">{{ t("leads.modalAddTitle") }}</h3>
          <button @click="showModal = false" class="text-slate-400 hover:text-white"><X class="w-5 h-5" /></button>
        </div>
        <form @submit.prevent="createLead" class="space-y-4">
          <div v-if="formError" class="p-3 rounded-xl bg-rose-500/10 text-rose-400 text-xs">{{ formError }}</div>
          <div>
            <label class="block text-xs text-slate-400 mb-1">{{ t("leads.displayName") }}</label>
            <input v-model="form.name" required :placeholder="t('leads.displayNamePlaceholder')" class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white" />
          </div>
          <div>
            <label class="block text-xs text-slate-400 mb-1">{{ t("leads.portfolioId") }}</label>
            <input v-model="form.portfolioId" required placeholder="e.g. 4123456789012345678" class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm font-mono text-white" />
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="block text-xs text-slate-400 mb-1">{{ t("leads.pollSec") }}</label>
              <input v-model.number="form.pollIntervalSec" type="number" min="5" max="3600" class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white" />
            </div>
            <div>
              <label class="block text-xs text-slate-400 mb-1">{{ t("leads.proxyNode") }}</label>
              <select v-model="form.proxyId" class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white">
                <option :value="null">{{ t("leads.autoDirect") }}</option>
                <option v-for="p in proxies" :key="p.id" :value="p.id">{{ p.name }} ({{ p.region }})</option>
              </select>
            </div>
          </div>
          <label class="flex items-center gap-2 text-xs text-slate-300 cursor-pointer">
            <input v-model="form.isPublicDashboard" type="checkbox" class="rounded bg-background border-slate-600 text-accent" />
            <span>{{ t("leads.exposePublic") }}</span>
          </label>
          <div class="flex justify-end gap-3 pt-2">
            <button type="button" @click="showModal = false" class="px-4 py-2 rounded-xl bg-surface2 text-slate-300 text-xs font-medium">{{ t("leads.cancel") }}</button>
            <button type="submit" class="px-4 py-2 rounded-xl bg-accent text-slate-950 font-semibold text-xs">{{ t("leads.saveLead") }}</button>
          </div>
        </form>
      </div>
    </div>

    <!-- Rich Inspect / Telemetry Modal -->
    <div v-if="detailLead" class="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-2 sm:p-5">
      <div class="w-full max-w-7xl max-h-[92vh] bg-background border border-slate-800 rounded-2xl flex flex-col shadow-2xl overflow-hidden">
        <div class="px-5 py-3.5 bg-surface border-b border-slate-800 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 class="text-base sm:text-lg font-bold text-white">{{ detailLead.name }}</h3>
            <p class="text-xs text-slate-400 font-mono">
              {{
                t("leads.inspectHeaderSub", {
                  id: detailLead.portfolioId,
                  mode: detailLead.positionShow === false ? t("leads.hiddenReconstructed") : t("leads.publicDirect"),
                })
              }}
            </p>
          </div>
          <div class="flex items-center flex-wrap gap-2">
            <button
              @click="syncLead(detailLead.id)"
              :disabled="syncingId === detailLead.id"
              class="px-3 py-1.5 rounded-lg bg-surface2 hover:bg-slate-700 text-xs text-slate-200 flex items-center gap-1.5"
            >
              <RefreshCw class="w-3.5 h-3.5" :class="{ 'animate-spin': syncingId === detailLead.id }" />
              <span>{{ syncingId === detailLead.id ? t("leads.syncing") : t("leads.syncNow") }}</span>
            </button>
            <button
              @click="downloadCsv(detailLead.id, 'cycles')"
              class="px-3 py-1.5 rounded-lg bg-surface2 hover:bg-slate-700 text-xs text-slate-200 flex items-center gap-1.5"
            >
              <Download class="w-3.5 h-3.5 text-accent" />
              <span>{{ t("leads.exportCyclesCsv") }}</span>
            </button>
            <button
              @click="downloadCsv(detailLead.id, 'orders')"
              class="px-3 py-1.5 rounded-lg bg-surface2 hover:bg-slate-700 text-xs text-slate-200 flex items-center gap-1.5"
            >
              <Download class="w-3.5 h-3.5 text-brand" />
              <span>{{ t("leads.exportOrdersCsv") }}</span>
            </button>
            <button @click="detailLead = null" class="p-1.5 text-slate-400 hover:text-white"><X class="w-5 h-5" /></button>
          </div>
        </div>

        <div class="p-4 sm:p-6 overflow-y-auto flex-1">
          <div v-if="!detailSnapshot" class="text-center py-12 text-slate-500 text-sm">
            {{ t("leads.noSnapshot") }}
          </div>
          <LeadTelemetryPanel
            v-else
            :snapshot="detailSnapshot"
            :portfolio-id="detailLead.portfolioId"
            :display-name="detailLead.name"
          />
        </div>
      </div>
    </div>
  </div>
</template>
