<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { Cpu, Globe, Lock, RefreshCw, Volume2, VolumeX } from "lucide-vue-next";
import { api } from "@/api/client";
import { useI18n } from "@/i18n";
import LangToggle from "@/components/LangToggle.vue";
import LeadTelemetryPanel from "@/components/LeadTelemetryPanel.vue";

const { t, isZh } = useI18n();

const loading = ref(true);
const enabled = ref(true);
const leads = ref<any[]>([]);
const lastUpdatedAt = ref<string>("");
const countdownSec = ref(5);
const soundEnabled = ref<boolean>(localStorage.getItem("mq_pub_sound") !== "0");
let lastOrderTimestamps: Record<number, number> = {};
let pollTimer: any = null;
let tickTimer: any = null;

function playPing() {
  if (!soundEnabled.value) return;
  try {
    const AudioCtx = window.AudioContext || (window as any).webkitAudioContext;
    if (!AudioCtx) return;
    const ctx = new AudioCtx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.setValueAtTime(740, ctx.currentTime);
    osc.frequency.exponentialRampToValueAtTime(1046, ctx.currentTime + 0.14);
    gain.gain.setValueAtTime(0.05, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.25);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.26);
  } catch {
    // ignore audio block
  }
}

function toggleSound() {
  soundEnabled.value = !soundEnabled.value;
  try {
    localStorage.setItem("mq_pub_sound", soundEnabled.value ? "1" : "0");
  } catch {
    // ignore
  }
  if (soundEnabled.value) playPing();
}

async function fetchPublic() {
  try {
    const res = await api.get<{ enabled: boolean; leads: any[] }>("/api/dashboard/public");
    enabled.value = res.enabled;
    leads.value = res.leads || [];
    lastUpdatedAt.value = new Date().toLocaleTimeString(isZh.value ? "zh-CN" : "en-US", { hour12: false });
    countdownSec.value = 5;

    for (const l of leads.value) {
      const latest = l.snapshot?.profile?.lastTradeTime || 0;
      if (lastOrderTimestamps[l.id] && latest > lastOrderTimestamps[l.id]) {
        playPing();
      }
      if (latest) lastOrderTimestamps[l.id] = latest;
    }
  } finally {
    loading.value = false;
  }
}

const beaconStatus = computed(() => {
  if (!leads.value.length) return { key: "pub.beaconLive", cls: "bg-emerald-500/15 text-emerald-400 border-emerald-500/30" };
  const gen = leads.value[0]?.snapshot?.meta?.generatedAt || 0;
  const age = Math.round((Date.now() - gen) / 1000);
  if (age > 90) return { key: "pub.beaconOff", cls: "bg-rose-500/15 text-rose-400 border-rose-500/30" };
  if (age > 25) return { key: "pub.beaconStale", cls: "bg-amber-500/15 text-amber-300 border-amber-500/30" };
  return { key: "pub.beaconLive", cls: "bg-emerald-500/15 text-emerald-400 border-emerald-500/30" };
});

onMounted(() => {
  fetchPublic();
  pollTimer = setInterval(fetchPublic, 5000);
  tickTimer = setInterval(() => {
    countdownSec.value = countdownSec.value > 1 ? countdownSec.value - 1 : 5;
  }, 1000);
});

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer);
  if (tickTimer) clearInterval(tickTimer);
});
</script>

<template>
  <div class="min-h-screen bg-background text-slate-100 p-4 md:p-8 max-w-[1600px] mx-auto space-y-6 overflow-x-hidden">
    <!-- Top Bar -->
    <header class="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-5 border-b border-slate-800">
      <div class="flex items-center gap-3 min-w-0">
        <div class="w-10 h-10 rounded-xl bg-accent/10 border border-accent/30 flex items-center justify-center shrink-0">
          <Cpu class="w-5 h-5 text-accent" />
        </div>
        <div class="min-w-0">
          <h1 class="text-xl sm:text-2xl font-bold text-white flex flex-wrap items-center gap-2">
            <span>{{ t("pub.title") }}</span>
            <span class="px-2 py-0.5 rounded-full text-xs font-mono border" :class="beaconStatus.cls">
              {{ t(beaconStatus.key) }}
            </span>
          </h1>
          <p class="text-sm text-slate-300 leading-relaxed">
            {{ t("pub.subtitle") }}
            <span v-if="lastUpdatedAt" class="font-mono text-slate-400"> · {{ lastUpdatedAt }}</span>
          </p>
        </div>
      </div>

      <div class="flex items-center flex-wrap gap-2 shrink-0">
        <LangToggle />
        <button
          type="button"
          @click="toggleSound"
          class="p-2 rounded-xl bg-surface border border-slate-700 hover:border-slate-500 text-slate-300 transition-colors"
          :title="soundEnabled ? t('sound.onTitle') : t('sound.offTitle')"
        >
          <Volume2 v-if="soundEnabled" class="w-4 h-4 text-accent" />
          <VolumeX v-else class="w-4 h-4 text-slate-400" />
        </button>
        <button
          @click="fetchPublic"
          class="px-3 py-2 rounded-xl bg-surface border border-slate-700 hover:border-slate-500 text-sm text-slate-300 flex items-center gap-1.5 transition-colors"
        >
          <RefreshCw class="w-3.5 h-3.5" :class="{ 'animate-spin': loading }" />
          <span>{{ t("pub.refresh") }}</span>
        </button>
        <router-link
          to="/login"
          class="px-3 py-2 rounded-xl bg-surface border border-slate-800 hover:border-accent/40 text-slate-400 hover:text-accent text-sm font-medium flex items-center gap-1.5 transition-colors"
        >
          <Lock class="w-3.5 h-3.5" />
          <span>{{ t("pub.console") }}</span>
        </router-link>
      </div>
    </header>

    <div v-if="!enabled" class="p-10 rounded-2xl bg-surface border border-slate-800 text-center">
      <Globe class="w-8 h-8 text-slate-400 mx-auto mb-3" />
      <h2 class="text-base font-bold text-white">{{ t("pub.disabledTitle") }}</h2>
      <p class="text-xs text-slate-400 mt-1">{{ t("pub.disabledDesc") }}</p>
    </div>

    <div v-else-if="leads.length === 0" class="p-10 rounded-2xl bg-surface border border-slate-800 text-center text-sm text-slate-400">
      {{ t("pub.empty") }}
    </div>

    <div v-else class="space-y-8">
      <LeadTelemetryPanel
        v-for="lead in leads"
        :key="lead.id"
        :snapshot="lead.snapshot"
        :portfolio-id="lead.portfolioId"
        :display-name="lead.name"
        :countdown-sec="countdownSec"
      />
    </div>
  </div>
</template>
