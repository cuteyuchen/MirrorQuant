<script setup lang="ts">
import { onMounted, ref } from "vue";
import { Bell, CheckCircle2, Lock, Save, Send, ShieldCheck } from "lucide-vue-next";
import { api } from "@/api/client";

const settingsData = ref<any>(null);
const auditLogs = ref<any[]>([]);
const saving = ref(false);
const savedMsg = ref("");
const testMsg = ref("");

async function loadData() {
  const [sRes, lRes] = await Promise.all([
    api.get<any>("/api/settings"),
    api.get<{ items: any[] }>("/api/settings/audit-logs?limit=50"),
  ]);
  settingsData.value = sRes;
  auditLogs.value = lRes.items;
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
    savedMsg.value = "Settings saved successfully.";
    setTimeout(() => (savedMsg.value = ""), 3000);
    await loadData();
  } finally {
    saving.value = false;
  }
}

async function testNotify() {
  testMsg.value = "Sending...";
  try {
    const res = await api.post<any>("/api/settings/test-notify");
    testMsg.value = "Result: " + JSON.stringify(res.results);
  } catch (e: any) {
    testMsg.value = "Error: " + e.message;
  }
}

onMounted(loadData);
</script>

<template>
  <div class="space-y-6" v-if="settingsData">
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
      <div>
        <h2 class="text-xl sm:text-2xl font-bold text-white tracking-tight">System Settings & Audit Trail</h2>
        <p class="text-xs sm:text-sm text-slate-400 mt-0.5">
          Manage alert dispatchers, public dashboard visibility, vault security, and admin audit logs
        </p>
      </div>
      <button
        @click="saveSettings"
        :disabled="saving"
        class="px-4 py-2.5 rounded-xl bg-gradient-to-r from-accent to-brand text-slate-950 font-semibold text-xs sm:text-sm flex items-center gap-2 hover:opacity-90"
      >
        <Save class="w-4 h-4" />
        <span>{{ saving ? "Saving..." : "Save Configuration" }}</span>
      </button>
    </div>

    <div v-if="savedMsg" class="p-3 rounded-xl bg-emerald-500/15 border border-emerald-500/30 text-emerald-300 text-xs flex items-center gap-2">
      <CheckCircle2 class="w-4 h-4" />
      <span>{{ savedMsg }}</span>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <!-- Notifications -->
      <div class="rounded-2xl bg-surface border border-slate-800/80 p-5 space-y-4">
        <div class="flex items-center justify-between">
          <h3 class="text-sm font-bold text-white flex items-center gap-2">
            <Bell class="w-4 h-4 text-accent" />
            <span>Notification Channels</span>
          </h3>
          <button
            @click="testNotify"
            class="px-3 py-1 rounded-lg bg-surface2 hover:bg-slate-700 text-xs text-accent flex items-center gap-1.5"
          >
            <Send class="w-3.5 h-3.5" />
            <span>Test Alert</span>
          </button>
        </div>

        <p v-if="testMsg" class="text-[11px] font-mono text-slate-300 bg-background p-2 rounded-lg border border-slate-800 break-all">
          {{ testMsg }}
        </p>

        <div class="space-y-3 text-xs">
          <!-- Telegram -->
          <div class="p-3.5 rounded-xl bg-background/60 border border-slate-800 space-y-2.5">
            <label class="flex items-center gap-2 font-semibold text-slate-200 cursor-pointer">
              <input v-model="settingsData.notifications.telegram_enabled" type="checkbox" class="rounded bg-background border-slate-600 text-accent" />
              <span>Telegram Bot Alerts</span>
            </label>
            <div v-if="settingsData.notifications.telegram_enabled" class="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
              <input v-model="settingsData.notifications.telegram_bot_token" placeholder="Bot Token (1234:ABC...)" class="px-3 py-2 rounded-lg bg-surface border border-slate-700 text-white font-mono" />
              <input v-model="settingsData.notifications.telegram_chat_id" placeholder="Chat ID" class="px-3 py-2 rounded-lg bg-surface border border-slate-700 text-white font-mono" />
            </div>
          </div>

          <!-- WeCom / DingTalk -->
          <div class="p-3.5 rounded-xl bg-background/60 border border-slate-800 space-y-2.5">
            <label class="flex items-center gap-2 font-semibold text-slate-200 cursor-pointer">
              <input v-model="settingsData.notifications.wecom_enabled" type="checkbox" class="rounded bg-background border-slate-600 text-accent" />
              <span>WeCom / DingTalk Group Webhook</span>
            </label>
            <input
              v-if="settingsData.notifications.wecom_enabled"
              v-model="settingsData.notifications.wecom_webhook_url"
              placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."
              class="w-full px-3 py-2 rounded-lg bg-surface border border-slate-700 text-white font-mono"
            />
          </div>

          <!-- Generic Webhook -->
          <div class="p-3.5 rounded-xl bg-background/60 border border-slate-800 space-y-2.5">
            <label class="flex items-center gap-2 font-semibold text-slate-200 cursor-pointer">
              <input v-model="settingsData.notifications.webhook_enabled" type="checkbox" class="rounded bg-background border-slate-600 text-accent" />
              <span>Generic JSON Webhook</span>
            </label>
            <input
              v-if="settingsData.notifications.webhook_enabled"
              v-model="settingsData.notifications.webhook_url"
              placeholder="https://your-server.com/hook"
              class="w-full px-3 py-2 rounded-lg bg-surface border border-slate-700 text-white font-mono"
            />
          </div>

          <!-- Local JSONL Outbox -->
          <div class="p-3.5 rounded-xl bg-background/60 border border-slate-800 space-y-2.5">
            <label class="flex items-center gap-2 font-semibold text-slate-200 cursor-pointer">
              <input v-model="settingsData.notifications.outbox_enabled" type="checkbox" class="rounded bg-background border-slate-600 text-accent" />
              <span>Local JSONL Outbox (File Queue)</span>
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

      <!-- Security & Public Feed -->
      <div class="space-y-6">
        <div class="rounded-2xl bg-surface border border-slate-800/80 p-5 space-y-4">
          <h3 class="text-sm font-bold text-white flex items-center gap-2">
            <ShieldCheck class="w-4 h-4 text-emerald-400" />
            <span>Cryptographic Vault Status</span>
          </h3>
          <div class="grid grid-cols-2 gap-3 text-xs font-mono">
            <div class="p-3 rounded-xl bg-background/60 border border-slate-800">
              <span class="text-slate-500 block text-[10px]">CREDENTIAL CIPHER</span>
              <span class="text-emerald-400 font-semibold">{{ settingsData.security.encryptionAlgorithm }}</span>
            </div>
            <div class="p-3 rounded-xl bg-background/60 border border-slate-800">
              <span class="text-slate-500 block text-[10px]">PASSWORD HASH</span>
              <span class="text-emerald-400 font-semibold">{{ settingsData.security.passwordHashing }}</span>
            </div>
            <div class="p-3 rounded-xl bg-background/60 border border-slate-800">
              <span class="text-slate-500 block text-[10px]">AUTH PROTOCOL</span>
              <span class="text-slate-200">{{ settingsData.security.authMode }}</span>
            </div>
            <div class="p-3 rounded-xl bg-background/60 border border-slate-800">
              <span class="text-slate-500 block text-[10px]">MASTER KEY SOURCE</span>
              <span class="text-accent">{{ settingsData.security.masterKeySource }}</span>
            </div>
          </div>

          <div class="pt-3 border-t border-slate-800/80">
            <label class="flex items-center justify-between cursor-pointer">
              <div>
                <span class="text-xs font-semibold text-white block">Public Lead Monitoring Feed (`/public`)</span>
                <span class="text-[11px] text-slate-400">Allow unauthenticated visitors to view opted-in lead snapshots (accounts stay private)</span>
              </div>
              <input v-model="settingsData.public_monitoring_enabled" type="checkbox" class="rounded bg-background border-slate-600 text-accent w-4 h-4" />
            </label>
          </div>
        </div>
      </div>
    </div>

    <!-- Audit Logs -->
    <div class="rounded-2xl bg-surface border border-slate-800/80 overflow-hidden">
      <div class="px-5 py-4 border-b border-slate-800/80 flex items-center justify-between">
        <h3 class="text-sm font-bold text-white flex items-center gap-2">
          <Lock class="w-4 h-4 text-brand" />
          <span>Security & Operations Audit Log</span>
        </h3>
        <span class="text-xs font-mono text-slate-400">Latest {{ auditLogs.length }} Events</span>
      </div>
      <div class="overflow-x-auto max-h-[400px] overflow-y-auto">
        <table class="w-full text-left border-collapse text-xs">
          <thead>
            <tr class="border-b border-slate-800 text-slate-400 font-mono uppercase">
              <th class="py-2.5 px-4">Timestamp</th>
              <th class="py-2.5 px-4">User</th>
              <th class="py-2.5 px-4">Action</th>
              <th class="py-2.5 px-4">Details</th>
              <th class="py-2.5 px-4">IP</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-800/60 font-mono">
            <tr v-for="log in auditLogs" :key="log.id" class="hover:bg-slate-800/30">
              <td class="py-2.5 px-4 text-slate-400 whitespace-nowrap">{{ new Date(log.createdAt).toLocaleString() }}</td>
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
