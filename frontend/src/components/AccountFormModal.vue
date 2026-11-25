<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { Save, Wifi } from "lucide-vue-next";
import type { Account, AccountTestResult, Environment, ProxyNode } from "@/types/api";
import { api, ApiError } from "@/api/client";
import { useI18n } from "@/i18n";
import { useAsyncAction } from "@/composables/useAsyncAction";
import { useUiStore } from "@/stores/ui";
import { fieldErrors, reasonText } from "@/utils/errors";
import { formatUsd } from "@/utils/format";
import BaseModal from "./BaseModal.vue";
import EnvBadge from "./EnvBadge.vue";
const props = defineProps<{ open: boolean; account?: Account | null; proxies: ProxyNode[] }>();
const emit = defineEmits<{ close: []; saved: [] }>();
const { t } = useI18n();
const { pending, error, run } = useAsyncAction();
const ui = useUiStore();
const errors = ref<Record<string, string>>({});
const createdAccount = ref<Account | null>(null);
const currentAccount = computed(() => props.account || createdAccount.value);
const testResult = ref<AccountTestResult | null>(null);
const form = ref({ name: "", exchange: "okx", environment: "demo" as Environment | null, apiKey: "", secretKey: "", passphrase: "", proxyId: null as number | null, marginMode: "cross", positionMode: "hedge" });
watch(() => props.open, open => {
  // 编辑仅提交名称和代理，绝不回写掩码凭证或重置账户身份。
  if (open) {
    const account = props.account;
    form.value = { name: account?.name || "", exchange: account?.exchange || "okx",
      environment: account ? account.environment : "demo", apiKey: "", secretKey: "", passphrase: "",
      proxyId: account?.proxyId ?? null, marginMode: "cross", positionMode: "hedge" };
    createdAccount.value = null; testResult.value = null; errors.value = {}; error.value = "";
  } else { form.value.apiKey = ""; form.value.secretKey = ""; form.value.passphrase = ""; }
}, { immediate: true });
watch(form, () => { testResult.value = null; }, { deep: true });
// 注销立即销毁敏感草稿和旧结果，迟到请求不能污染后续会话。
watch(() => ui.sessionGeneration, () => {
  createdAccount.value = null; testResult.value = null; errors.value = {}; error.value = "";
  form.value.apiKey = ""; form.value.secretKey = ""; form.value.passphrase = "";
});
function currentSession(generation: number) { return ui.sessionGeneration === generation && props.open; }
function validate() {
  errors.value = {};
  if (!form.value.name.trim()) errors.value.name = t("validation.required");
  if (!currentAccount.value) {
    for (const [field, value] of [["api_key", form.value.apiKey], ["secret_key", form.value.secretKey]]) {
      if (value.trim().length < 4) errors.value[field] = t("validation.minLength", { n: 4 });
    }
    if (form.value.exchange === "okx" && !form.value.passphrase.trim()) errors.value.passphrase = t("validation.required");
    if (!["okx", "binance"].includes(form.value.exchange)) errors.value.exchange = t("validation.invalid");
    if (!["demo", "live"].includes(form.value.environment || "")) errors.value.environment = t("validation.required");
  }
  return Object.keys(errors.value).length === 0;
}
function confirmation() {
  const editing = currentAccount.value;
  return editing || form.value.environment === "live" ? {
    title: t(editing ? "common.save" : "accts.addBtn"),
    message: t(editing ? "confirm.edit" : "confirm.createLive", { name: form.value.name }),
    environment: editing ? editing.environment : form.value.environment, strong: editing?.environment == null && !!editing,
  } : undefined;
}
async function persist() {
  // 请求阶段只返回数据；本地状态与事件留到会话保护通过之后更新。
  const editing = currentAccount.value;
  const payload = editing ? { name: form.value.name.trim(), proxyId: form.value.proxyId }
    : { ...form.value, name: form.value.name.trim(), isEnabled: false };
  const response = await (editing ? api.put<{ item: Account | null }>(`/api/accounts/${editing.id}`, payload)
    : api.post<{ item: Account | null }>("/api/accounts", payload));
  const saved = response.item || editing;
  if (!saved) throw new ApiError(502, t("accts.createIncomplete"));
  return saved;
}
async function save() {
  if (!validate()) return;
  const generation = ui.sessionGeneration;
  const response = await run(persist, {
    confirm: confirmation(),
    onError: cause => { errors.value = fieldErrors(cause); },
  });
  if (response.ok && currentSession(generation)) { emit("saved"); emit("close"); }
}
async function testConnection() {
  if (!validate()) return;
  const generation = ui.sessionGeneration;
  testResult.value = null;
  // 后端只支持已保存账户；两阶段分别检查会话，注销后不能继续发起测试。
  const saved = await run(persist, {
    confirm: confirmation(),
    success: false,
    onError: cause => { errors.value = fieldErrors(cause); },
  });
  if (!currentSession(generation)) return;
  if (!saved.ok) {
    if (error.value) testResult.value = { ok: false, error: error.value };
    return;
  }
  if (!props.account) {
    createdAccount.value = saved.value;
    form.value.apiKey = ""; form.value.secretKey = ""; form.value.passphrase = "";
  }
  emit("saved");
  if (!currentSession(generation)) return;
  const tested = await run(async () => {
    const result = await api.post<AccountTestResult>(`/api/accounts/${saved.value.id}/test`);
    if (result.ok !== true) throw new ApiError(400, result.error || t("accts.testUnknown"));
    return result;
  }, {
    success: t("accts.testSuccess"),
    onError: cause => { errors.value = fieldErrors(cause); },
  });
  if (!currentSession(generation)) return;
  if (tested.ok) {
    testResult.value = tested.value;
    emit("saved");
  } else if (error.value) {
    testResult.value = { ok: false, error: error.value };
  }
}
</script>
<template>
  <BaseModal :open="open" :title="t(currentAccount ? 'accts.editTitle' : 'accts.modalTitle')" :busy="pending" @close="emit('close')">
    <form @submit.prevent="save" novalidate><fieldset class="space-y-4" :disabled="pending">
      <p v-if="error" class="error-banner" role="alert">{{ error }}</p>
      <EnvBadge :environment="currentAccount ? currentAccount.environment : form.environment" />
      <p class="text-xs text-muted">{{ t(currentAccount ? "accts.testPersistNotice" : "accts.createDisabledNotice") }}</p>
      <label class="field-label">{{ t("common.name") }} *<input v-model="form.name" name="name" class="form-input mt-1" required maxlength="120" :aria-invalid="!!errors.name" /><span v-if="errors.name" class="field-error">{{ errors.name }}</span></label>
      <div v-if="!currentAccount" class="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <label class="field-label">{{ t("accts.exchange") }}<select v-model="form.exchange" class="form-input mt-1"><option value="okx">{{ t("accts.exchangeOkx") }}</option><option value="binance">{{ t("accts.exchangeBinance") }}</option></select><span v-if="errors.exchange" class="field-error">{{ errors.exchange }}</span></label>
        <label class="field-label">{{ t("accts.environment") }}<select v-model="form.environment" class="form-input mt-1"><option value="demo">{{ t("accts.envDemo") }}</option><option value="live">{{ t("accts.envLive") }}</option></select><span v-if="errors.environment" class="field-error">{{ errors.environment }}</span></label>
      </div>
      <section v-if="!currentAccount" class="space-y-4 rounded-xl border border-border/50 bg-background/40 p-3.5">
        <h3 class="section-title">{{ t("accts.credentials") }}</h3>
        <label class="field-label">{{ t("accts.apiKey") }} *<input v-model="form.apiKey" name="api_key" type="password" autocomplete="off" required minlength="4" maxlength="256" class="form-input mt-1" :aria-invalid="!!errors.api_key" /><span v-if="errors.api_key" class="field-error">{{ errors.api_key }}</span></label>
        <label class="field-label">{{ t("accts.secretKey") }} *<input v-model="form.secretKey" name="secret_key" type="password" autocomplete="new-password" required minlength="4" maxlength="256" class="form-input mt-1" :aria-invalid="!!errors.secret_key" /><span v-if="errors.secret_key" class="field-error">{{ errors.secret_key }}</span></label>
        <label v-if="form.exchange === 'okx'" class="field-label">{{ t("accts.passphrase") }} *<input v-model="form.passphrase" name="passphrase" type="password" autocomplete="off" required maxlength="256" class="form-input mt-1" :aria-invalid="!!errors.passphrase" /><span v-if="errors.passphrase" class="field-error">{{ errors.passphrase }}</span></label>
      </section>
      <label class="field-label">{{ t("accts.proxy") }}<select v-model="form.proxyId" class="form-input mt-1" :aria-invalid="!!errors.proxy_id"><option :value="null">{{ t("accts.direct") }}</option><option v-if="form.proxyId != null && !proxies.some(proxy => proxy.id === form.proxyId)" :value="form.proxyId">{{ t("accts.proxyUnavailable", { id: form.proxyId }) }}</option><option v-for="proxy in proxies" :key="proxy.id" :value="proxy.id">{{ proxy.name ?? t("common.unknown") }}</option></select><span v-if="errors.proxy_id" class="field-error">{{ errors.proxy_id }}</span></label>
      <details v-if="!currentAccount" class="rounded-xl border border-border/50 bg-background/40 px-3.5 py-2.5" :open="!!errors.margin_mode || !!errors.position_mode">
        <summary class="cursor-pointer text-sm font-semibold">{{ t("accts.advanced") }}</summary>
        <div class="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label class="field-label">{{ t("accts.marginMode") }}<select v-model="form.marginMode" class="form-input mt-1"><option value="cross">{{ t("accts.cross") }}</option><option value="isolated">{{ t("accts.isolated") }}</option></select><span v-if="errors.margin_mode" class="field-error">{{ errors.margin_mode }}</span></label>
          <label class="field-label">{{ t("accts.posMode") }}<select v-model="form.positionMode" class="form-input mt-1"><option value="hedge">{{ t("accts.hedge") }}</option><option value="one_way">{{ t("accts.oneWay") }}</option></select><span v-if="errors.position_mode" class="field-error">{{ errors.position_mode }}</span></label>
        </div>
      </details>
      <section v-if="testResult" class="rounded-xl border p-3.5 text-sm" :class="testResult.ok === true ? 'border-success/40 bg-success/5' : 'border-warning/40 bg-warning/5'" role="status">
        <p class="font-semibold" :class="testResult.ok === true ? 'text-success' : 'text-warning'">{{ t(testResult.ok === true ? "accts.testSuccess" : "accts.testFailure") }}</p>
        <p v-if="testResult.error" class="mt-1 break-words text-muted">{{ reasonText(testResult.error) }}</p>
        <dl v-if="testResult.ok === true" class="mt-2 grid grid-cols-2 gap-3"><div class="number"><dt class="text-xs text-muted">{{ t("accts.equity") }}</dt><dd>{{ formatUsd(testResult.totalEquity ?? testResult.usdtEquity) }}</dd></div><div class="number"><dt class="text-xs text-muted">{{ t("accts.posMode") }}</dt><dd>{{ ["long_short_mode", "hedge"].includes(testResult.posMode || "") ? t("accts.hedge") : ["net_mode", "one_way"].includes(testResult.posMode || "") ? t("accts.oneWay") : t("common.unknown") }}</dd></div></dl>
      </section>
      <footer class="flex flex-wrap justify-end gap-3"><button type="button" class="button-secondary" @click="emit('close')">{{ t("common.cancel") }}</button><button type="button" data-testid="test-account" class="button-secondary" @click="testConnection"><Wifi :size="16" />{{ t(currentAccount ? "accts.test" : "accts.createAndTest") }}</button><button type="submit" class="button-primary"><Save :size="16" />{{ t(pending ? "common.working" : "common.save") }}</button></footer>
    </fieldset></form>
  </BaseModal>
</template>
