<script setup lang="ts">
import { onActivated, onMounted, ref } from "vue";
import { Bell, CheckCircle2, Lock, Save, Send, ShieldCheck } from "lucide-vue-next";
import { api } from "@/api/client";
import { useDataCacheStore } from "@/stores/dataCache";
import { useI18n } from "@/i18n";

defineOptions({ name: "SettingsView" });

const cache = useDataCacheStore();
const { t, isZh } = useI18n();

const settingsData = ref<any>(cache.settings?.data ? structuredClone(cache.settings.data) : null);
const auditLogs = ref<any[]>(cache.auditLogs?.data || []);
const saving = ref(false);
const savedMsg = ref("");
const testMsg = ref("");

async function loadData(force = false) {
  const [sRes, lItems] = await Promise.all([
    cache.fetchSettings(force, 20000),
    cache.fetchAuditLogs(force, 20000),
  ]);
  settingsData.value = JSON.parse(JSON.stringify(sRes));
  auditLogs.value = lItems;
}

async function saveSettings() {
  if (!settingsData.value) return;
  saving.value = true;
  savedMsg.value = "";
  try {
    await api.put("/api/settings", {
      global_kill_switch: settingsData.value.global_kill_switch,
      public_monitoring_enabled: settingsData.value.public_monitoring_enabled,
      notifications: settingsData.value.notifications,
      risk_defaults: settingsData.value.risk_defaults,
    });
    savedMsg.value = t("set.savedMsg");
    setTimeout(() => (savedMsg.value = ""), 3000);
    await loadData(true);
  } finally {
    saving.value = false;
  }
}

async function testNotify() {
  testMsg.value = t("set.sending");
  try {
    const res = await api.post<any>("/api/settings/test-notify");
    testMsg.value = JSON.stringify(res.results);
  } catch (e: any) {
    testMsg.value = "Error: " + e.message;
  }
}

onMounted(() => loadData(false));
onActivated(() => {
  if (cache.settings && !settingsData.value) {
    settingsData.value = JSON.parse(JSON.stringify(cache.settings.data));
  }
  if (cache.auditLogs) auditLogs.value = cache.auditLogs.data;
  loadData(false);
});
</script>

<template>
  <div class="space-y-6" v-if="settingsData">
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
      <div>
        <h2 class="text-xl sm:text-2xl font-bold text-white tracking-tight">{{ t("set.title") }}</h2>
        <p class="text-xs sm:text-sm text-slate-400 mt-0.5">{{ t("set.subtitle") }}</p>
      </div>
      <button
        @click="saveSettings"
        :disabled="saving"
        class="px-4 py-2.5 rounded-xl bg-gradient-to-r from-accent to-brand text-slate-950 font-semibold text-xs sm:text-sm flex items-center gap-2 hover:opacity-90"
      >
        <Save class="w-4 h-4" />
        <span>{{ saving ? t("set.saving") : t("set.saveBtn") }}</span>
      </button>
    </div>

    <div v-if="savedMsg" class="p-3 rounded-xl bg-emerald-500/15 border border-emerald-500/30 text-emerald-300 text-xs flex items-center gap-2">
      <CheckCircle2 class="w-4 h-4" />
      <span>{{ savedMsg }}</span>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <div class="rounded-2xl bg-surface border border-slate-800/80 p-5 space-y-4">
        <div class="flex items-center justify-between">
          <h3 class="text-sm font-bold text-white flex items-center gap-2">
            <Bell class="w-4 h-4 text-accent" />
            <span>{{ t("set.notifChannels") }}</span>
          </h3>
          <button
            @click="testNotify"
            class="px-3 py-1 rounded-lg bg-surface2 hover:bg-slate-700 text-xs text-accent flex items-center gap-1.5"
          >
            <Send class="w-3.5 h-3.5" />
            <span>{{ t("set.testAlert") }}</span>
          </button>
        </div>

        <p v-if="testMsg" class="text-[11px] font-mono text-slate-300 bg-background p-2 rounded-lg border border-slate-800 break-all">
          {{ testMsg }}
        </p>

        <div class="space-y-3 text-xs">
          <div class="p-3.5 rounded-xl bg-background/60 border border-slate-800 space-y-2.5">
            <label class="flex items-center gap-2 font-semibold text-slate-200 cursor-pointer">
              <input v-model="settingsData.notifications.telegram_enabled" type="checkbox" class="rounded bg-background border-slate-600 text-accent" />
              <span>{{ t("set.tgLabel") }}</span>
            </label>
            <div v-if="settingsData.notifications.telegram_enabled" class="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
              <input v-model="settingsData.notifications.telegram_bot_token" placeholder="Bot Token (1234:ABC...)" class="px-3 py-2 rounded-lg bg-surface border border-slate-700 text-white font-mono" />
              <input v-model="settingsData.notifications.telegram_chat_id" placeholder="Chat ID" class="px-3 py-2 rounded-lg bg-surface border border-slate-700 text-white font-mono" />
            </div>
          </div>

          <div class="p-3.5 rounded-xl bg-background/60 border border-slate-800 space-y-2.5">
            <label class="flex items-center gap-2 font-semibold text-slate-200 cursor-pointer">
              <input v-model="settingsData.notifications.wecom_enabled" type="checkbox" class="rounded bg-background border-slate-600 text-accent" />
              <span>{{ t("set.wecomLabel") }}</span>
            </label>
            <input
              v-if="settingsData.notifications.wecom_enabled"
              v-model="settingsData.notifications.wecom_webhook_url"
              placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."
              class="w-full px-3 py-2 rounded-lg bg-surface border border-slate-700 text-white font-mono"
            />
          </div>

          <div class="p-3.5 rounded-xl bg-background/60 border border-slate-800 space-y-2.5">
            <label class="flex items-center gap-2 font-semibold text-slate-200 cursor-pointer">
              <input v-model="settingsData.notifications.webhook_enabled" type="checkbox" class="rounded bg-background border-slate-600 text-accent" />
              <span>{{ t("set.webhookLabel") }}</span>
            </label>
            <input
              v-if="settingsData.notifications.webhook_enabled"
              v-model="settingsData.notifications.webhook_url"
              placeholder="https://your-server.com/hook"
              class="w-full px-3 py-2 rounded-lg bg-surface border border-slate-700 text-white font-mono"
            />
          </div>

          <div class="p-3.5 rounded-xl bg-background/60 border border-slate-800 space-y-2.5">
            <label class="flex items-center gap-2 font-semibold text-slate-200 cursor-pointer">
              <input v-model="settingsData.notifications.outbox_enabled" type="checkbox" class="rounded bg-background border-slate-600 text-accent" />
              <span>{{ t("set.outboxLabel") }}</span>
            </label>
            <input
              v-if="settingsData.notifications.outbox_enabled"
              v-model="settingsData.notifications.outbox_path"
              placeholder="/path/to/outbox.jsonl"
              class="w-full px-3 py-2 rounded-lg bg-surface border border-slate-700 text-white font-mono"
            />
          </div>
        </div>
      </div>

      <div class="space-y-6">
        <div class="rounded-2xl bg-surface border border-slate-800/80 p-5 space-y-4">
          <h3 class="text-sm font-bold text-white flex items-center gap-2">
            <ShieldCheck class="w-4 h-4 text-emerald-400" />
            <span>{{ t("set.vaultStatus") }}</span>
          </h3>
          <div class="grid grid-cols-2 gap-3 text-xs font-mono">
            <div class="p-3 rounded-xl bg-background/60 border border-slate-800">
              <span class="text-slate-500 block text-[10px]">{{ t("set.cipher") }}</span>
              <span class="text-emerald-400 font-semibold">{{ settingsData.security.encryptionAlgorithm }}</span>
            </div>
            <div class="p-3 rounded-xl bg-background/60 border border-slate-800">
              <span class="text-slate-500 block text-[10px]">{{ t("set.pwHash") }}</span>
              <span class="text-emerald-400 font-semibold">{{ settingsData.security.passwordHashing }}</span>
            </div>
            <div class="p-3 rounded-xl bg-background/60 border border-slate-800">
              <span class="text-slate-500 block text-[10px]">{{ t("set.authProto") }}</span>
              <span class="text-slate-200">{{ settingsData.security.authMode }}</span>
            </div>
            <div class="p-3 rounded-xl bg-background/60 border border-slate-800">
              <span class="text-slate-500 block text-[10px]">{{ t("set.masterKeySrc") }}</span>
              <span class="text-accent">{{ settingsData.security.masterKeySource }}</span>
            </div>
          </div>

          <div class="pt-3 border-t border-slate-800/80">
            <label class="flex items-center justify-between cursor-pointer gap-3">
              <div>
                <span class="text-xs font-semibold text-white block">{{ t("set.publicToggleTitle") }}</span>
                <span class="text-[11px] text-slate-400">{{ t("set.publicToggleDesc") }}</span>
              </div>
              <input v-model="settingsData.public_monitoring_enabled" type="checkbox" class="rounded bg-background border-slate-600 text-accent w-4 h-4 shrink-0" />
            </label>
          </div>
        </div>
      </div>
    </div>

    <div class="rounded-2xl bg-surface border border-slate-800/80 overflow-hidden">
      <div class="px-5 py-4 border-b border-slate-800/80 flex items-center justify-between">
        <h3 class="text-sm font-bold text-white flex items-center gap-2">
          <Lock class="w-4 h-4 text-brand" />
          <span>{{ t("set.auditTitle") }}</span>
        </h3>
        <span class="text-xs font-mono text-slate-400">{{ t("set.latestEvents", { n: auditLogs.length }) }}</span>
      </div>
      <div class="overflow-x-auto max-h-[400px] overflow-y-auto">
        <table class="w-full text-left border-collapse text-xs">
          <thead>
            <tr class="border-b border-slate-800 text-slate-400 font-mono uppercase">
              <th class="py-2.5 px-4">{{ t("set.colTime") }}</th>
              <th class="py-2.5 px-4">{{ t("set.colUser") }}</th>
              <th class="py-2.5 px-4">{{ t("set.colAction") }}</th>
              <th class="py-2.5 px-4">{{ t("set.colDetails") }}</th>
              <th class="py-2.5 px-4">{{ t("set.colIp") }}</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-800/60 font-mono">
            <tr v-for="log in auditLogs" :key="log.id" class="hover:bg-slate-800/30">
              <td class="py-2.5 px-4 text-slate-400 whitespace-nowrap">{{ new Date(log.createdAt).toLocaleString(isZh ? "zh-CN" : "en-US") }}</td>
              <td class="py-2.5 px-4 text-white">{{ log.username }}</td>
              <td class="py-2.5 px-4 text-accent">{{ log.action }}</td>
              <td class="py-2.5 px-4 text-slate-300 font-sans">{{ log.details }}</td>
              <td class="py-2.5 px-4 text-slate-500">{{ log.ipAddress || "-" }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>
