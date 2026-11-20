<script setup lang="ts">
import { onMounted, ref, watch } from "vue";
import { Send } from "lucide-vue-next";
import type { LeadSubscription, NotificationSettings, NotificationTestResponse } from "@/types/api";
import { api } from "@/api/client";
import { useAsyncAction } from "@/composables/useAsyncAction";
import { useI18n } from "@/i18n";
import { reasonText } from "@/utils/errors";
const props = defineProps<{ modelValue: NotificationSettings; errors: Record<string, string>; dirty: boolean }>();
const emit = defineEmits<{ "update:modelValue": [value: NotificationSettings] }>();
const { t } = useI18n();
const { pending, error, run } = useAsyncAction();
const results = ref<NotificationTestResponse["results"]>(null);

const subscriptions = ref<LeadSubscription[]>([]);
const subscriptionsLoading = ref(false);
const subPending = ref(false);

async function loadSubscriptions() {
  subscriptionsLoading.value = true;
  try {
    const res = await api.get<{ items: LeadSubscription[] }>("/api/notifications/subscriptions");
    subscriptions.value = res.items || [];
  } catch {
    // Keep empty on error
  } finally {
    subscriptionsLoading.value = false;
  }
}

async function toggleSubscription(sub: LeadSubscription, enabled: boolean) {
  subPending.value = true;
  const original = sub.isEnabled;
  sub.isEnabled = enabled;
  try {
    const res = await api.put<{ items: LeadSubscription[] }>("/api/notifications/subscriptions", {
      subscriptions: [{ leadId: sub.leadId, isEnabled: enabled }],
    });
    subscriptions.value = res.items || [];
  } catch {
    sub.isEnabled = original;
  } finally {
    subPending.value = false;
  }
}

onMounted(() => {
  void loadSubscriptions();
});
// 修改配置后清掉旧测试结果，避免把上一份配置的成功当成当前状态。
watch(() => props.modelValue, () => { results.value = null; error.value = ""; }, { deep: true });
// optional 的字段可以留空（如钉钉加签密钥）；hint 说明该渠道怎样才收得到消息。
type ChannelField = { key: keyof NotificationSettings; label: string; optional?: boolean };
const channels: { id: string; enabled: keyof NotificationSettings; label: string; hint: string; fields: ChannelField[] }[] = [
  { id: "webhook", enabled: "webhook_enabled", label: "set.webhook", hint: "set.hintWebhook", fields: [{ key: "webhook_url", label: "set.webhookUrl" }] },
  { id: "telegram", enabled: "telegram_enabled", label: "set.telegram", hint: "set.hintTelegram", fields: [{ key: "telegram_bot_token", label: "set.botToken" }, { key: "telegram_chat_id", label: "set.chatId" }] },
  { id: "wecom", enabled: "wecom_enabled", label: "set.wecom", hint: "set.hintWecom", fields: [{ key: "wecom_webhook_url", label: "set.webhookUrl" }] },
  { id: "dingtalk", enabled: "dingtalk_enabled", label: "set.dingtalk", hint: "set.hintDingtalk", fields: [{ key: "dingtalk_webhook_url", label: "set.webhookUrl" }, { key: "dingtalk_secret", label: "set.dingtalkSecret", optional: true }] },
  { id: "outbox", enabled: "outbox_enabled", label: "set.outbox", hint: "set.hintOutbox", fields: [{ key: "outbox_path", label: "set.outboxPath" }] },
];
const events = { notify_on_execution: "set.notifyExecution", notify_on_lead_order: "set.notifyLead", notify_on_risk_block: "set.notifyRisk", notify_on_security_alert: "set.notifySecurity" } as const;
function update<K extends keyof NotificationSettings>(key: K, value: NotificationSettings[K]) {
  emit("update:modelValue", { ...props.modelValue, [key]: value });
}
function configured(channel: typeof channels[number]) {
  return channel.fields.every(field => field.optional || !!props.modelValue[field.key] || props.modelValue[`${field.key}_configured` as keyof NotificationSettings] === true);
}
function showFields(channel: typeof channels[number]) {
  // 明确停用时不展示配置项；状态未知时照常展示，有字段报错时强制展示，避免报错无处可看。
  return props.modelValue[channel.enabled] !== false
    || channel.fields.some(field => !!props.errors[`notifications.${field.key}`]);
}
async function test() {
  // 后端当前会测试所有已保存且启用的渠道，各卡片展示各自的真实结果。
  const response = await run(() => api.post<NotificationTestResponse>("/api/settings/test-notify"), { success: false });
  if (response.ok) results.value = response.value.results;
}
</script>
<template>
  <section class="space-y-4" :aria-label="t('settingsTab.notifications')">
    <label class="field-label block max-w-sm">{{ t("set.displayUnit") }}
      <select :value="modelValue.display_unit ?? 'notional'" name="notifications.display_unit" class="form-input mt-1" :aria-invalid="!!errors['notifications.display_unit']" @change="update('display_unit', ($event.target as HTMLSelectElement).value as NotificationSettings['display_unit'])">
        <option value="notional">{{ t("set.displayUnitNotional") }}</option>
        <option value="quantity">{{ t("set.displayUnitQuantity") }}</option>
        <option value="both">{{ t("set.displayUnitBoth") }}</option>
      </select>
      <span class="mt-1 block text-xs font-normal text-muted">{{ t("set.displayUnitHint") }}</span>
      <span v-if="errors['notifications.display_unit']" class="field-error block">{{ errors["notifications.display_unit"] }}</span>
    </label>
    <div class="grid gap-2 sm:grid-cols-2">
      <label v-for="(label, key) in events" :key="key" class="flex min-h-11 items-start gap-3 py-2">
        <input type="checkbox" :checked="modelValue[key] === true" @change="update(key, ($event.target as HTMLInputElement).checked)" />
        <span>{{ t(label) }}<span v-if="errors[`notifications.${key}`]" class="field-error block">{{ errors[`notifications.${key}`] }}</span></span>
      </label>
    </div>
    <details v-for="channel in channels" :key="channel.id" class="rounded-lg border border-border/60 bg-surface p-4" :open="!!channel.fields.find(field => errors[`notifications.${field.key}`])">
      <!-- 未知布尔值不能冒充停用，渠道摘要明确区分三态。 -->
      <summary class="cursor-pointer text-sm font-semibold"><span>{{ t(channel.label) }}</span><span class="ml-3 text-xs font-normal" :class="modelValue[channel.enabled] == null || !configured(channel) ? 'text-warning' : 'text-muted'">{{ t(configured(channel) ? "set.configured" : "set.notConfigured") }} · {{ t(modelValue[channel.enabled] == null ? "state.UNKNOWN" : modelValue[channel.enabled] ? "common.enable" : "common.disable") }}</span></summary>
      <div class="mt-4 space-y-4">
        <label class="flex min-h-11 items-center gap-3"><input type="checkbox" :checked="modelValue[channel.enabled] === true" @change="update(channel.enabled, ($event.target as HTMLInputElement).checked as never)" />{{ t("set.enableChannel") }}</label>
        <p v-if="errors[`notifications.${channel.enabled}`]" class="field-error">{{ errors[`notifications.${channel.enabled}`] }}</p>
        <div v-if="showFields(channel)" class="space-y-4">
        <p class="text-xs leading-relaxed text-muted">{{ t(channel.hint) }}</p>
        <label v-for="field in channel.fields" :key="field.key" class="field-label">{{ t(field.label) }} <span v-if="modelValue[channel.enabled] && !field.optional" aria-hidden="true">*</span>
          <input :value="(modelValue[field.key] as string | null | undefined) ?? ''" :name="`notifications.${field.key}`" :type="field.key.includes('url') || field.key.includes('token') || field.key.includes('secret') ? 'password' : 'text'" class="form-input mt-1" autocomplete="off" :aria-invalid="!!errors[`notifications.${field.key}`]" @input="update(field.key, ($event.target as HTMLInputElement).value as never)" />
          <span v-if="errors[`notifications.${field.key}`]" class="field-error block">{{ errors[`notifications.${field.key}`] }}</span>
        </label>
        <p class="text-xs text-muted">{{ t(dirty ? "set.testSaveFirst" : "set.testAllNotice") }}</p>
        <button type="button" class="button-secondary" :disabled="pending || dirty || !modelValue[channel.enabled] || !configured(channel)" @click="test"><Send :size="16" />{{ t("set.sendTest") }}</button>
        <p v-if="error" class="text-sm text-danger" role="alert">{{ error }}</p>
        <p v-else-if="results" class="text-sm" :class="results[channel.id]?.ok ? 'text-success' : 'text-warning'" role="status">
          {{ t("set.notifyTestResult") }}: {{ results[channel.id] ? results[channel.id]?.ok ? t("state.SUCCESS") : reasonText(results[channel.id]?.error) : t("set.notDispatched") }}
          <span v-if="results[channel.id]?.status"> · {{ t("set.httpStatus", { n: results[channel.id]!.status! }) }}</span>
          <!-- 平台回执码（钉钉 310000 多为关键词/加签不匹配）直接显示，便于对照排查。 -->
          <span v-if="results[channel.id]?.errcode"> · {{ t("set.errcode", { n: results[channel.id]!.errcode! }) }}</span>
        </p>
        </div>
      </div>
    </details>

    <div class="mt-6 rounded-lg border border-border/60 bg-surface p-4 space-y-4">
      <div>
        <h3 class="text-sm font-semibold text-foreground">{{ t("set.subscriptionsTitle") }}</h3>
        <p class="text-xs text-muted mt-1">{{ t("set.subscriptionsHint") }}</p>
      </div>
      <div v-if="subscriptionsLoading" class="text-xs text-muted">{{ t("common.loading") }}</div>
      <div v-else-if="!subscriptions.length" class="text-xs text-muted">{{ t("set.subEmpty") }}</div>
      <div v-else class="divide-y divide-border/40">
        <div v-for="sub in subscriptions" :key="sub.leadId" class="flex items-center justify-between py-2.5">
          <div class="min-w-0 pr-3">
            <div class="flex items-center gap-2">
              <span class="text-sm font-medium text-foreground truncate">{{ sub.leadName }}</span>
              <span
                class="inline-flex items-center px-1.5 py-0.5 rounded text-[11px] font-normal"
                :class="sub.hasBinding ? 'bg-success/15 text-success' : 'bg-surface-elevated text-muted'"
              >
                {{ t(sub.hasBinding ? "set.subCopying" : "set.subWatching") }}
              </span>
            </div>
            <div v-if="sub.leadPlatform" class="text-xs text-muted mt-0.5">{{ sub.leadPlatform }}</div>
          </div>
          <label class="flex items-center gap-2 cursor-pointer text-sm">
            <input
              type="checkbox"
              :checked="sub.isEnabled"
              :disabled="subPending"
              @change="toggleSubscription(sub, ($event.target as HTMLInputElement).checked)"
            />
            <span class="text-xs text-muted">{{ t(sub.isEnabled ? "common.enable" : "common.disable") }}</span>
          </label>
        </div>
      </div>
    </div>
  </section>
</template>
