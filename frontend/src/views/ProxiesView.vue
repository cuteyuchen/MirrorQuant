<script setup lang="ts">
import { computed, onActivated, onMounted, ref } from "vue";
import { Pencil, Plus, RefreshCw, Save, Trash2, Wifi } from "lucide-vue-next";
import { api, ApiError, isCancelled } from "@/api/client";
import { useDataCacheStore } from "@/stores/dataCache";
import { useAsyncAction } from "@/composables/useAsyncAction";
import { useI18n } from "@/i18n";
import type { ProxyNode, ProxyTestResponse } from "@/types/api";
import { PROXY_PRIORITY_RULE, validateNumber } from "@/constants/validation";
import { errorMessage, fieldErrors, reasonText } from "@/utils/errors";
import { formatDateTime, formatQty } from "@/utils/format";
import BaseModal from "@/components/BaseModal.vue";
import PageHeader from "@/components/PageHeader.vue";
import DataTable from "@/components/DataTable.vue";
import MoreMenu from "@/components/MoreMenu.vue";
import EnvBadge from "@/components/EnvBadge.vue";
import FreshnessStamp from "@/components/FreshnessStamp.vue";
import NumberField from "@/components/NumberField.vue";
import StatusBadge from "@/components/StatusBadge.vue";
defineOptions({ name: "ProxiesView" });
const cache = useDataCacheStore();
const { t } = useI18n();
const { pending, error, run } = useAsyncAction();
const proxies = computed(() => cache.proxies?.data || []);
const showModal = ref(false);
const editing = ref<ProxyNode | null>(null);
const errors = ref<Record<string, string>>({});
const form = ref({ name: "", url: "", region: "AUTO", priority: 10 as number | null });
const loading = ref(false);
const loadError = ref("");
const batchResult = ref<{ n: number; passed: number; failed: number } | null>(null);
const rows = computed(() => [...proxies.value].sort((a, b) => Number(!!issue(b)) - Number(!!issue(a)) || a.id - b.id));
const summary = computed(() => cache.proxies ? t("proxy.rowSummary", {
  n: proxies.value.length, failed: proxies.value.filter(proxy => proxy.isHealthy === false || proxy.lastError).length,
  unknown: proxies.value.filter(proxy => proxy.isHealthy == null).length,
  disabled: proxies.value.filter(proxy => proxy.isEnabled === false).length,
}) : t("proxy.summaryUnknown"));
const usageKnown = computed(() => !!cache.accounts && !!cache.leads);
let sequence = 0;
async function load(force = false) {
  if (loading.value && !force) return;
  const request = ++sequence; loading.value = true; loadError.value = "";
  // 引用列表请求失败只影响使用方说明，不遮住仍可操作的代理列表。
  const results = await Promise.allSettled([cache.fetchProxies(force), cache.fetchAccounts(force), cache.fetchLeads(force), cache.fetchBindings(force)]);
  if (request !== sequence) return;
  if (results[0].status === "rejected" && !isCancelled(results[0].reason)) loadError.value = errorMessage(results[0].reason);
  loading.value = false;
}
function references(proxyId: number) {
  return {
    accounts: (cache.accounts?.data || []).filter(account => account.proxyId === proxyId),
    leads: (cache.leads?.data || []).filter(lead => lead.proxyId === proxyId),
  };
}
function environments(proxyId: number) {
  const leadIds = references(proxyId).leads.map(lead => lead.id);
  return [...references(proxyId).accounts.map(account => account.environment),
    ...(cache.bindings?.data.items || []).filter(binding => binding.leadId != null && leadIds.includes(binding.leadId)).map(binding => binding.accountEnvironment)];
}
function issue(proxy: ProxyNode) {
  return proxy.lastError ? reasonText(proxy.lastError) : proxy.isHealthy === false ? t("proxy.unreachable")
    : proxy.isEnabled === true && proxy.isHealthy == null ? t("proxy.notChecked") : "";
}
function rowTone(proxy: ProxyNode) { return proxy.isHealthy === false || proxy.lastError ? "bg-danger/5" : issue(proxy) ? "bg-warning/5" : ""; }
function safeAddress(value: string | null) {
  if (!value) return t("common.unknown");
  // 即使旧后端漏掉掩码，也不在列表展示代理账号密码。
  try { const url = new URL(value); url.username = ""; url.password = ""; return url.toString(); }
  catch { return t("proxy.invalidAddress"); }
}
function edit(proxy: ProxyNode | null) { editing.value = proxy; error.value = ""; errors.value = {}; form.value = { name: proxy?.name || "", url: proxy?.url || "", region: proxy?.region || "AUTO", priority: proxy ? proxy.priority : 10 }; showModal.value = true; }
async function save() {
  errors.value = {};
  const validation = validateNumber(form.value.priority, PROXY_PRIORITY_RULE);
  if (validation) errors.value.priority = validation;
  if (!form.value.name.trim()) errors.value.name = t("validation.required");
  const keepUrl = editing.value && (!form.value.url.trim() || form.value.url === editing.value.url);
  if (!keepUrl && !form.value.url.trim()) errors.value.url = t("validation.required");
  else if (!keepUrl && form.value.url.trim().length < 3) errors.value.url = t("validation.invalid");
  if (Object.keys(errors.value).length) return;
  // 编辑留空或未修改 URL 都保留服务器凭据，未知地址绝不回写空串。
  const payload = { name: form.value.name.trim(), region: form.value.region.trim(), priority: form.value.priority,
    ...(keepUrl ? {} : { url: form.value.url.trim() }) };
  const result = await run(() => editing.value ? api.put(`/api/proxies/${editing.value.id}`, payload) : api.post("/api/proxies", payload), {
    confirm: editing.value ? { title: t("common.save"), message: t("confirm.edit", { name: form.value.name }), environments: environments(editing.value.id), strong: !usageKnown.value || !cache.bindings } : undefined,
    onError: cause => { errors.value = fieldErrors(cause); },
  });
  if (result.ok) { showModal.value = false; await load(true); }
}
function change(proxy: ProxyNode, operation: "toggle" | "delete", event?: Event) {
  // 开关只有确认成功且列表刷新后才反映新状态。
  if (event) (event.target as HTMLInputElement).checked = proxy.isEnabled === true;
  if (operation === "toggle" && proxy.isEnabled == null) return;
  const title = t(operation === "delete" ? "common.delete" : proxy.isEnabled ? "common.disable" : "common.enable");
  return run(async () => {
    if (operation === "delete") await api.delete(`/api/proxies/${proxy.id}`);
    else await api.put(`/api/proxies/${proxy.id}`, { isEnabled: !proxy.isEnabled });
    await cache.fetchProxies(true);
  }, { confirm: { title, message: t(operation === "delete" ? "confirm.delete" : "confirm.toggle", { name: proxy.name || "—", action: title }),
    environments: environments(proxy.id), strong: !usageKnown.value || !cache.bindings } });
}
function test(proxy: ProxyNode) {
  batchResult.value = null;
  return run(async () => {
    const response = await api.post<ProxyTestResponse>(`/api/proxies/${proxy.id}/test`);
    await cache.fetchProxies(true);
    if (response.test?.healthy !== true) throw new ApiError(400, response.test?.error || t("proxy.testUnknown"));
  });
}
function testAll() {
  batchResult.value = null;
  return run(async () => {
    // 单个节点失败不取消其余检测，最终结果逐项统计。
    const results = await Promise.allSettled(proxies.value.map(proxy => api.post<ProxyTestResponse>(`/api/proxies/${proxy.id}/test`)));
    const passed = results.filter(result => result.status === "fulfilled" && result.value.test?.healthy === true).length;
    batchResult.value = { n: results.length, passed, failed: results.length - passed };
    await cache.fetchProxies(true);
    if (batchResult.value.failed) throw new ApiError(400, t("proxy.testsFailed", { n: batchResult.value.failed }));
  }, { success: t("proxy.testsPassed") });
}
onMounted(() => { void load(); });
onActivated(() => { void load(); });
</script>
<template>
  <div class="space-y-5">
    <PageHeader :title="t('proxy.title')" :summary="summary"><template #actions>
      <button class="icon-button" :disabled="pending || loading" :aria-label="t('common.refresh')" :title="t('common.refresh')" @click="load(true)"><RefreshCw :size="18" :class="{ 'animate-spin': loading }" /></button>
      <button class="button-secondary" :disabled="pending || loading || !proxies.length" @click="testAll"><Wifi :size="17" />{{ t("proxy.testAll") }}</button>
      <button class="button-primary" :disabled="pending" @click="edit(null)"><Plus :size="17" />{{ t("proxy.addBtn") }}</button>
    </template></PageHeader>
    <p v-if="error && !showModal" class="error-banner" role="alert">{{ error }}</p>
    <p v-if="batchResult" class="text-sm" :class="batchResult.failed ? 'text-warning' : 'text-success'" role="status">{{ t("proxy.batchResult", batchResult) }}</p>
    <DataTable :rows="rows" :label="t('proxy.title')" :row-key="row => row.id" :row-tone="rowTone" :loading="loading" :error="loadError" :empty="t('proxy.emptyTitle')" @retry="load(true)" :columns="[
      { key: 'node', label: t('proxy.node'), width: '25%', mobileWide: true },
      { key: 'routing', label: t('proxy.routing'), width: '10%' },
      { key: 'health', label: t('proxy.health'), width: '13%', numeric: true },
      { key: 'checked', label: t('proxy.checked'), width: '19%', mobileWide: true },
      { key: 'usage', label: t('proxy.usedBy'), width: '23%', mobileWide: true },
      { key: 'actions', label: t('common.actions'), width: '10%' },
    ]">
      <template #cell-node="{ row }"><h2 class="break-words text-sm font-semibold">{{ row.name ?? t("common.unknown") }}</h2><p class="mt-1 break-all text-xs text-muted">{{ safeAddress(row.url) }}</p><label class="mt-1 inline-flex min-h-11 items-center gap-2 text-xs"><input type="checkbox" role="switch" :checked="row.isEnabled === true" :disabled="pending || row.isEnabled == null" :aria-label="t('proxy.enabledFor', { name: row.name ?? '—' })" @change="change(row, 'toggle', $event)" /><span>{{ t(row.isEnabled == null ? "state.UNKNOWN" : row.isEnabled ? "common.enabled" : "common.disabled") }}</span></label></template>
      <template #cell-routing="{ row }"><p>{{ row.region ?? t("common.unknown") }}</p><p class="mt-1 text-xs text-muted">{{ t("proxy.priority") }} <span class="tabular-nums">{{ formatQty(row.priority) }}</span></p></template>
      <template #cell-health="{ row }"><StatusBadge :state="row.isHealthy == null ? 'unknown' : row.isHealthy ? 'healthy' : 'error'" /><p class="mt-1 tabular-nums">{{ row.latencyMs == null ? t("common.unknown") : t("proxy.latencyValue", { n: formatQty(row.latencyMs) }) }}</p></template>
      <template #cell-checked="{ row }"><FreshnessStamp :timestamp="row.lastCheckedAt" :warn-after="300" :critical-after="900" /><p class="mt-1 text-xs text-muted">{{ formatDateTime(row.lastCheckedAt, { short: true, withSeconds: false }) }}</p><p v-if="issue(row)" class="mt-1 break-words text-xs" :class="row.isHealthy === false || row.lastError ? 'text-danger' : 'text-warning'">{{ issue(row) }}</p></template>
      <template #cell-usage="{ row }"><p v-if="!usageKnown" class="text-xs text-warning">{{ t("proxy.usageUnknown") }}</p><div v-else class="space-y-2"><p v-for="account in references(row.id).accounts" :key="`account-${account.id}`" class="flex flex-wrap items-center gap-2 text-xs"><span>{{ t("accts.account") }} · {{ account.name ?? t("common.unknown") }}</span><EnvBadge :environment="account.environment" /></p><p v-for="lead in references(row.id).leads" :key="`lead-${lead.id}`" class="break-words text-xs">{{ t("nav.leads") }} · {{ lead.name ?? t("common.unknown") }}</p><p v-if="!references(row.id).accounts.length && !references(row.id).leads.length" class="text-xs text-muted">{{ t("proxy.unused") }}</p></div></template>
      <template #cell-actions="{ row }"><div class="flex flex-wrap items-center justify-end gap-1"><button class="icon-button" :disabled="pending" :aria-label="t('proxy.testLatency')" :title="t('proxy.testLatency')" @click="test(row)"><Wifi :size="17" /></button><button class="icon-button" :disabled="pending" :aria-label="t('common.edit')" :title="t('common.edit')" @click="edit(row)"><Pencil :size="17" /></button><MoreMenu><button class="menu-item text-danger" :disabled="pending" @click="change(row, 'delete')"><Trash2 :size="16" />{{ t("common.delete") }}</button></MoreMenu></div></template>
    </DataTable>
    <BaseModal :open="showModal" :title="t(editing ? 'proxy.editTitle' : 'proxy.modalTitle')" :busy="pending" @close="showModal = false">
      <form @submit.prevent="save" novalidate><fieldset class="space-y-4" :disabled="pending">
        <p v-if="error" class="error-banner" role="alert">{{ error }}</p>
        <label class="field-label">{{ t("proxy.nodeName") }} *<input v-model="form.name" name="name" required maxlength="100" class="form-input mt-1" :aria-invalid="!!errors.name" /><span v-if="errors.name" class="field-error">{{ errors.name }}</span></label>
        <label class="field-label">{{ t("proxy.proxyUrl") }}<span v-if="!editing" aria-hidden="true"> *</span><input v-model="form.url" name="url" type="password" :required="!editing" minlength="3" maxlength="255" autocomplete="off" class="form-input mt-1" :placeholder="editing ? t('proxy.keepUrl') : ''" :aria-invalid="!!errors.url" /><span v-if="errors.url" class="field-error">{{ errors.url }}</span><span class="mt-1 block text-xs text-muted">{{ t(editing ? "proxy.keepUrl" : "proxy.urlHelp") }}</span></label>
        <label class="field-label">{{ t("proxy.region") }}<input v-model="form.region" name="region" maxlength="32" class="form-input mt-1" :aria-invalid="!!errors.region" /><span v-if="errors.region" class="field-error">{{ errors.region }}</span></label>
        <NumberField v-model="form.priority" :rule="PROXY_PRIORITY_RULE" :error="errors.priority" />
        <footer class="flex justify-end gap-3"><button type="button" class="button-secondary" @click="showModal = false">{{ t("common.cancel") }}</button><button type="submit" class="button-primary"><Save :size="17" />{{ t("common.save") }}</button></footer>
      </fieldset></form>
    </BaseModal>
  </div>
</template>
