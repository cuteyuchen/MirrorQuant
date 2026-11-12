<script setup lang="ts">
import { onMounted, ref } from "vue";
import { Activity, Network, Plus, Trash2, X } from "lucide-vue-next";
import { api } from "@/api/client";

const proxies = ref<any[]>([]);
const loading = ref(true);
const testingId = ref<number | null>(null);
const showModal = ref(false);
const form = ref({
  name: "",
  url: "",
  region: "JP",
  priority: 10,
});
const formError = ref("");

async function loadData() {
  loading.value = true;
  try {
    const res = await api.get<{ items: any[] }>("/api/proxies");
    proxies.value = res.items;
  } finally {
    loading.value = false;
  }
}

async function createProxy() {
  formError.value = "";
  try {
    await api.post("/api/proxies", form.value);
    showModal.value = false;
    form.value = { name: "", url: "", region: "JP", priority: 10 };
    await loadData();
  } catch (e: any) {
    formError.value = e.message;
  }
}

async function testProxy(id: number) {
  testingId.value = id;
  try {
    await api.post(`/api/proxies/${id}/test`);
    await loadData();
  } finally {
    testingId.value = null;
  }
}

async function deleteProxy(id: number) {
  if (!confirm("Delete this proxy node?")) return;
  await api.delete(`/api/proxies/${id}`);
  await loadData();
}

onMounted(loadData);
</script>

<template>
  <div class="space-y-6">
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
      <div>
        <h2 class="text-xl sm:text-2xl font-bold text-white tracking-tight">Proxy Pool & Routing Nodes</h2>
        <p class="text-xs sm:text-sm text-slate-400 mt-0.5">
          Configure HTTP/SOCKS5 residential or datacenter nodes for compliant Binance BAPI & exchange routing
        </p>
      </div>
      <button
        @click="showModal = true"
        class="px-4 py-2.5 rounded-xl bg-gradient-to-r from-accent to-brand text-slate-950 font-semibold text-xs sm:text-sm flex items-center gap-2 hover:opacity-90 shadow-lg shadow-accent/20"
      >
        <Plus class="w-4 h-4" />
        <span>Add Proxy Node</span>
      </button>
    </div>

    <div v-if="loading && proxies.length === 0" class="text-center py-12 text-slate-400 text-sm">Loading proxies...</div>

    <div v-else-if="proxies.length === 0" class="p-10 rounded-2xl bg-surface border border-slate-800 text-center">
      <p class="text-slate-300 font-medium mb-1">No proxy nodes configured</p>
      <p class="text-xs text-slate-500 mb-4">Traffic defaults to direct connection. Add a TW/JP/EU proxy node if your server IP is geo-restricted.</p>
      <button @click="showModal = true" class="px-4 py-2 rounded-xl bg-accent/15 text-accent border border-accent/30 text-xs font-semibold">
        + Add Proxy Node
      </button>
    </div>

    <div v-else class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5">
      <div
        v-for="p in proxies"
        :key="p.id"
        class="rounded-2xl bg-surface border border-slate-800/80 p-5 flex flex-col justify-between space-y-4"
      >
        <div>
          <div class="flex items-start justify-between gap-2">
            <div class="flex items-center gap-2.5">
              <div class="w-9 h-9 rounded-xl bg-surface2 border border-slate-700 flex items-center justify-center">
                <Network class="w-4 h-4 text-accent" />
              </div>
              <div>
                <h3 class="font-bold text-sm text-white">{{ p.name }}</h3>
                <span class="text-[11px] font-mono text-slate-400">Priority: {{ p.priority }} · Region: {{ p.region }}</span>
              </div>
            </div>
            <span
              class="px-2 py-0.5 rounded-full text-[10px] font-mono border"
              :class="
                p.isHealthy === true
                  ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30'
                  : p.isHealthy === false
                  ? 'bg-rose-500/10 text-rose-400 border-rose-500/30'
                  : 'bg-slate-700/40 text-slate-400 border-slate-700'
              "
            >
              {{ p.isHealthy === true ? `${p.latencyMs}ms` : p.isHealthy === false ? "UNHEALTHY" : "UNTESTED" }}
            </span>
          </div>

          <div class="mt-3 p-2.5 rounded-xl bg-background/60 border border-slate-800 text-xs font-mono text-slate-300 break-all">
            {{ p.url }}
          </div>
          <p v-if="p.lastError" class="mt-2 text-[11px] text-rose-400 break-words">{{ p.lastError }}</p>
        </div>

        <div class="flex items-center justify-between pt-3 border-t border-slate-800/80">
          <button
            @click="testProxy(p.id)"
            :disabled="testingId === p.id"
            class="px-3 py-1.5 rounded-lg bg-accent/15 hover:bg-accent/25 text-accent text-xs font-medium flex items-center gap-1.5"
          >
            <Activity class="w-3.5 h-3.5" />
            <span>{{ testingId === p.id ? "Pinging..." : "Test Latency" }}</span>
          </button>
          <button @click="deleteProxy(p.id)" class="p-1.5 rounded-lg text-slate-500 hover:text-rose-400 hover:bg-rose-500/10">
            <Trash2 class="w-4 h-4" />
          </button>
        </div>
      </div>
    </div>

    <!-- Modal -->
    <div v-if="showModal" class="fixed inset-0 z-50 bg-black/75 backdrop-blur-sm flex items-center justify-center p-4">
      <div class="w-full max-w-md bg-surface border border-slate-800 rounded-2xl p-6 shadow-2xl">
        <div class="flex items-center justify-between mb-5">
          <h3 class="text-lg font-bold text-white">Add Proxy Node</h3>
          <button @click="showModal = false" class="text-slate-400 hover:text-white"><X class="w-5 h-5" /></button>
        </div>
        <form @submit.prevent="createProxy" class="space-y-4">
          <div v-if="formError" class="p-3 rounded-xl bg-rose-500/10 text-rose-400 text-xs">{{ formError }}</div>
          <div>
            <label class="block text-xs text-slate-400 mb-1">Node Name</label>
            <input v-model="form.name" required placeholder="e.g. Tokyo-Mihomo" class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white" />
          </div>
          <div>
            <label class="block text-xs text-slate-400 mb-1">Proxy URL (http:// or socks5://)</label>
            <input v-model="form.url" required placeholder="http://127.0.0.1:7890" class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm font-mono text-white" />
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="block text-xs text-slate-400 mb-1">Region Tag</label>
              <input v-model="form.region" placeholder="JP" class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white uppercase" />
            </div>
            <div>
              <label class="block text-xs text-slate-400 mb-1">Priority (Lower = First)</label>
              <input v-model.number="form.priority" type="number" min="1" max="1000" class="w-full px-3.5 py-2 rounded-xl bg-background border border-slate-700 text-sm text-white" />
            </div>
          </div>
          <div class="flex justify-end gap-3 pt-2">
            <button type="button" @click="showModal = false" class="px-4 py-2 rounded-xl bg-surface2 text-slate-300 text-xs font-medium">Cancel</button>
            <button type="submit" class="px-4 py-2 rounded-xl bg-accent text-slate-950 font-semibold text-xs">Save Node</button>
          </div>
        </form>
      </div>
    </div>
  </div>
</template>
