<script setup lang="ts">
import { computed, onActivated, onBeforeUnmount, onMounted, ref } from "vue";
import { onBeforeRouteLeave } from "vue-router";
import { RotateCcw, Save } from "lucide-vue-next";
import { api, isCancelled } from "@/api/client";
import { useDataCacheStore } from "@/stores/dataCache";
import { useAuthStore } from "@/stores/auth";
import { useAsyncAction } from "@/composables/useAsyncAction";
import { useI18n } from "@/i18n";
import { cloneSettings, settingsDirty, settingsPayload, SETTINGS_TABS, type SettingsTab } from "@/utils/settings";
import { DEFAULT_RESILIENCE, DEFAULT_RISK, ORDER_MODES, RESILIENCE_RULES, RISK_RULES, RISK_SWITCHES, SLIPPAGE_ACTIONS, TP_SL_MODES, riskReductions, validateFields } from "@/constants/validation";
import { errorMessage, fieldErrors } from "@/utils/errors";
import type { NotificationSettings, SettingsResponse } from "@/types/api";
import PageHeader from "@/components/PageHeader.vue";
import ErrorState from "@/components/ErrorState.vue";
import EmptyState from "@/components/EmptyState.vue";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";
import NotificationSettingsPanel from "@/components/settings/NotificationSettingsPanel.vue";
import RiskSettingsPanel from "@/components/settings/RiskSettingsPanel.vue";
import ResilienceSettingsPanel from "@/components/settings/ResilienceSettingsPanel.vue";
import SecuritySettingsPanel from "@/components/settings/SecuritySettingsPanel.vue";
import PublicSettingsPanel from "@/components/settings/PublicSettingsPanel.vue";

defineOptions({ name: "SettingsView" });
const cache = useDataCacheStore();
const auth = useAuthStore();
const { t } = useI18n();
const { pending, error, run } = useAsyncAction();
const activeTab = ref<SettingsTab>("notifications");
const settingsData = ref<SettingsResponse | null>(cache.settings ? cloneSettings(cache.settings.data) : null);
const baseline = ref<SettingsResponse | null>(settingsData.value ? cloneSettings(settingsData.value) : null);
const loading = ref(false);
const loadError = ref("");
const errors = ref<Record<string, string>>({});
const securityDirty = ref(false);
const securityBusy = ref(false);
const securityReset = ref(0);
const dirty = computed(() => settingsDirty(baseline.value, settingsData.value) || securityDirty.value);
const currentDirty = computed(() => settingsDirty(baseline.value, settingsData.value, activeTab.value));
const sectionAvailable = computed(() => activeTab.value === "public_monitoring_enabled" || activeTab.value === "security"
  || settingsData.value?.[activeTab.value] != null);
let sequence = 0;
async function loadData(force = false) {
  if (loading.value || dirty.value) return;
  const request = ++sequence;
  loading.value = true; loadError.value = "";
  try {
    const value = await cache.fetchSettings(force);
    if (request !== sequence || dirty.value) return;
    settingsData.value = cloneSettings(value); baseline.value = cloneSettings(value);
  } catch (cause: unknown) {
    if (request === sequence && !isCancelled(cause)) loadError.value = `${t("set.loadError")} ${errorMessage(cause)}`;
  } finally { if (request === sequence) loading.value = false; }
}
async function discard() {
  const result = await run(async () => {
    if (baseline.value) settingsData.value = cloneSettings(baseline.value);
    errors.value = {}; securityReset.value++; securityDirty.value = false;
  }, { confirm: { title: t("set.discard"), message: t("set.discardConfirm") }, success: false });
  return result.ok;
}
async function switchTab(tab: SettingsTab) {
  if (pending.value || securityBusy.value || tab === activeTab.value) return;
  if (dirty.value && !(await discard())) return;
  securityDirty.value = false; activeTab.value = tab; error.value = "";
}
function validate(): boolean {
  errors.value = {};
  const data = settingsData.value;
  if (!data) return false;
  if (activeTab.value === "risk_defaults" && data.risk_defaults) {
    errors.value = validateFields(data.risk_defaults, RISK_RULES, "risk_defaults.");
    for (const key of Object.keys(RISK_SWITCHES) as (keyof typeof RISK_SWITCHES)[]) {
      if (typeof data.risk_defaults[key] !== "boolean") errors.value[`risk_defaults.${key}`] = t("validation.required");
    }
    if (!ORDER_MODES.some(value => value === data.risk_defaults?.order_mode)) errors.value["risk_defaults.order_mode"] = t("validation.invalid");
    if (!SLIPPAGE_ACTIONS.some(value => value === data.risk_defaults?.slippage_action)) errors.value["risk_defaults.slippage_action"] = t("validation.invalid");
    if (!TP_SL_MODES.some(value => value === data.risk_defaults?.tp_sl_mode)) errors.value["risk_defaults.tp_sl_mode"] = t("validation.invalid");
  }
  if (activeTab.value === "resilience" && data.resilience) {
    errors.value = validateFields(data.resilience, RESILIENCE_RULES, "resilience.");
    if (!["alert_only", "auto_close"].includes(data.resilience.auto_close_orphan_positions || "")) errors.value["resilience.auto_close_orphan_positions"] = t("validation.required");
    if (!["auto_scale_down", "skip_alert"].includes(data.resilience.insufficient_margin_policy || "")) errors.value["resilience.insufficient_margin_policy"] = t("validation.required");
    if (typeof data.resilience.flip_close_first !== "boolean") errors.value["resilience.flip_close_first"] = t("validation.required");
  }
  if (activeTab.value === "notifications" && data.notifications) {
    const notification = data.notifications;
    for (const [key, value] of Object.entries(notification)) {
      if ((key.endsWith("_enabled") || key.startsWith("notify_on_")) && typeof value !== "boolean") errors.value[`notifications.${key}`] = t("validation.required");
    }
    const required: (keyof NotificationSettings)[] = [];
    if (notification.webhook_enabled) required.push("webhook_url");
    if (notification.telegram_enabled) required.push("telegram_bot_token", "telegram_chat_id");
    if (notification.wecom_enabled) required.push("wecom_webhook_url");
    if (notification.dingtalk_enabled) required.push("dingtalk_webhook_url");
    if (notification.outbox_enabled) required.push("outbox_path");
    for (const key of required) {
      if (!String(notification[key] ?? "").trim() && notification[`${key}_configured` as keyof NotificationSettings] !== true) errors.value[`notifications.${key}`] = t("validation.required");
    }
  }
  if (activeTab.value === "public_monitoring_enabled" && data.public_monitoring_enabled == null) errors.value.public_monitoring_enabled = t("validation.required");
  return Object.keys(errors.value).length === 0;
}
async function saveSettings() {
  if (!settingsData.value || !currentDirty.value || !validate()) return;
  const tab = activeTab.value;
  const data = cloneSettings(settingsData.value);
  const reductions = tab === "risk_defaults" ? riskReductions(baseline.value?.risk_defaults ?? null, data.risk_defaults) : [];
  const notificationOff = tab === "notifications" && Object.entries(data.notifications || {}).some(([key, value]) =>
    (key.startsWith("notify_on_") || key.endsWith("_enabled")) && value === false && baseline.value?.notifications?.[key as keyof NotificationSettings] === true);
  // 韧性阈值同样控制追单和熔断，任何变更都应明确确认后再应用。
  const resilienceRisk = tab === "resilience" && currentDirty.value;
  // 只保存当前页签，确认期间冻结草稿，绝不把旧 global_kill_switch 带回服务器。
  const result = await run(() => api.put<SettingsResponse>("/api/settings", settingsPayload(data, tab)), {
    confirm: reductions.length || notificationOff || resilienceRisk ? {
      title: t("common.save"), message: reductions.length ? t("confirm.riskOff", { items: reductions.join(" / ") }) : t("set.safetyChange"),
      strong: true, environments: cache.accounts?.data.map(account => account.environment) || [null],
    } : undefined,
    onError: cause => { errors.value = fieldErrors(cause); },
  });
  if (result.ok) {
    settingsData.value = cloneSettings(result.value); baseline.value = cloneSettings(result.value);
    await cache.fetchSettings(true).catch(() => { /* 保存已成功；下次激活再补拉只读缓存。 */ });
  }
}
async function applyDefaults() {
  if (dirty.value) return;
  await run(async () => {
    const result = await api.post<{ updatedCount: number | null }>("/api/settings/apply-defaults-to-bindings");
    await cache.fetchBindings(true);
    return result;
  }, { confirm: { title: t("set.applyDefaultsBtn"), message: t("confirm.applyDefaults"), strong: true,
    environments: cache.accounts?.data.map(account => account.environment) || [null] } });
}
function restoreMissing() {
  if (!settingsData.value) return;
  if (activeTab.value === "risk_defaults") settingsData.value.risk_defaults = { ...DEFAULT_RISK };
  if (activeTab.value === "resilience") settingsData.value.resilience = { ...DEFAULT_RESILIENCE };
}
onBeforeRouteLeave(async () => {
  if (!auth.authenticated || !dirty.value) return true;
  if (pending.value || securityBusy.value) return false;
  return discard();
});
function beforeUnload(event: BeforeUnloadEvent) {
  // 页面刷新无法弹自定义对话框，使用浏览器离开保护避免丢失当前草稿。
  if (!auth.authenticated || !dirty.value) return;
  event.preventDefault(); event.returnValue = "";
}
onMounted(() => { window.addEventListener("beforeunload", beforeUnload); void loadData(true); });
onBeforeUnmount(() => { sequence++; window.removeEventListener("beforeunload", beforeUnload); });
onActivated(() => { if (!dirty.value) void loadData(true); });
</script>
<template>
  <div class="space-y-4">
    <PageHeader :title="t('nav.settings')" :summary="t(dirty ? 'set.unsaved' : 'set.summary')" />
    <nav class="flex flex-wrap border-b border-border/50" role="tablist" :aria-label="t('nav.settings')">
      <button v-for="tab in SETTINGS_TABS" :id="`settings-tab-${tab}`" :key="tab" class="tab-button" role="tab" :aria-selected="activeTab === tab" :aria-controls="`settings-panel-${tab}`" :disabled="pending || securityBusy" @click="switchTab(tab)">{{ t(`settingsTab.${tab}`) }}<span v-if="settingsDirty(baseline, settingsData, tab) || tab === 'security' && securityDirty" class="ml-2 text-warning" :aria-label="t('set.unsaved')">*</span></button>
    </nav>
    <ErrorState v-if="loadError" :message="loadError" :busy="loading" @retry="loadData(true)" />
    <LoadingSkeleton v-if="loading && !settingsData" />
    <div v-else-if="settingsData" :id="`settings-panel-${activeTab}`" role="tabpanel" :aria-labelledby="`settings-tab-${activeTab}`">
      <SecuritySettingsPanel v-if="activeTab === 'security'" :key="securityReset" :security="settingsData.security" @dirty="securityDirty = $event" @busy="securityBusy = $event" />
      <form v-else @submit.prevent="saveSettings" novalidate>
        <fieldset class="space-y-5" :disabled="pending || loading">
          <p v-if="error" class="error-banner" role="alert">{{ error }}</p>
          <NotificationSettingsPanel v-if="activeTab === 'notifications' && settingsData.notifications" v-model="settingsData.notifications" :errors="errors" :dirty="currentDirty" />
          <RiskSettingsPanel v-else-if="activeTab === 'risk_defaults' && settingsData.risk_defaults" v-model="settingsData.risk_defaults" :errors="errors" :dirty="dirty" @apply="applyDefaults" />
          <ResilienceSettingsPanel v-else-if="activeTab === 'resilience' && settingsData.resilience" v-model="settingsData.resilience" :errors="errors" />
          <PublicSettingsPanel v-else-if="activeTab === 'public_monitoring_enabled'" v-model="settingsData.public_monitoring_enabled" :error="errors.public_monitoring_enabled" />
          <EmptyState v-else :title="t('set.sectionUnavailable')"><button v-if="activeTab !== 'notifications'" type="button" class="button-secondary" @click="restoreMissing">{{ t("set.loadDefaults") }}</button></EmptyState>
          <footer v-if="sectionAvailable" class="flex flex-wrap items-center justify-between gap-3 border-t border-border/50 pt-4">
            <p class="text-xs" :class="currentDirty ? 'text-warning' : 'text-muted'" role="status">{{ t(currentDirty ? "set.unsaved" : "set.savedState") }}</p>
            <div class="flex gap-2"><button type="button" class="button-secondary" :disabled="!currentDirty || pending" @click="discard"><RotateCcw :size="16" />{{ t("set.discard") }}</button><button type="submit" class="button-primary" :disabled="!currentDirty || pending"><Save :size="16" />{{ t(pending ? "common.working" : "common.save") }}</button></div>
          </footer>
        </fieldset>
      </form>
    </div>
    <EmptyState v-else-if="!loadError" :title="t('set.empty')"><button class="button-secondary" :disabled="loading" @click="loadData(true)">{{ t("common.retry") }}</button></EmptyState>
  </div>
</template>
