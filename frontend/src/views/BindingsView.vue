<script setup lang="ts">
import { computed, nextTick, onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from "vue";
import { AlertTriangle, ArrowRight, ChevronDown, Download, Pencil, Plus, RefreshCw, RotateCcw, Save, Trash2 } from "lucide-vue-next";
import { api, isCancelled } from "@/api/client";
import { useDataCacheStore } from "@/stores/dataCache";
import { useAsyncAction } from "@/composables/useAsyncAction";
import { useI18n } from "@/i18n";
import type { Binding, RiskSettings } from "@/types/api";
import type { TableColumn } from "@/types/ui";
import { bindingRisk, DEFAULT_RISK, FLAT_REQUIRED_RISK_FIELDS, ORDER_MODES, RISK_FIELD_GROUPS, RISK_RULES, RISK_SCALE_RULE, RISK_SWITCHES, riskReductions, SLIPPAGE_ACTIONS, TP_SL_MODES, validateFields, validateNumber } from "@/constants/validation";
import { bindingPresentation, bindingSummary, fetchTodayExecutionCounts, type ExecutionCounts } from "@/constants/bindingStatus";
import { errorMessage, fieldErrors, reasonText } from "@/utils/errors";
import { formatQty, formatUsd } from "@/utils/format";
import BaseModal from "@/components/BaseModal.vue";
import DataTable from "@/components/DataTable.vue";
import EnvBadge from "@/components/EnvBadge.vue";
import FreshnessStamp from "@/components/FreshnessStamp.vue";
import MoreMenu from "@/components/MoreMenu.vue";
import PageHeader from "@/components/PageHeader.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import NumberField from "@/components/NumberField.vue";
import RiskPolicyFields from "@/components/RiskPolicyFields.vue";

defineOptions({ name: "BindingsView" });
const { t } = useI18n();
const cache = useDataCacheStore();
const { pending, error: actionError, run } = useAsyncAction();
const bindings = computed(() => cache.bindings?.data.items || []);
const accounts = computed(() => cache.accounts?.data || []);
const leads = computed(() => cache.leads?.data || []);
const board = computed(() => cache.board?.data ?? null);
const intents = computed(() => cache.intents?.data ?? null);
const defaults = computed(() => cache.settings?.data.risk_defaults ?? null);
// 急停取较新的响应；较新响应未知时不拿旧的 false 代替。
const killSwitch = computed(() => cache.board && (!cache.bindings || cache.board.fetchedAt >= cache.bindings.fetchedAt)
  ? cache.board.data.globalKillSwitch : cache.bindings?.data.globalKillSwitch ?? null);
const summary = computed(() => bindingSummary(bindings.value, board.value, intents.value, killSwitch.value));
const presentations = computed(() => new Map(bindings.value.map(binding => [binding.id, bindingPresentation(binding, board.value, intents.value, killSwitch.value)])));
function status(binding: Binding) { return presentations.value.get(binding.id)!; }
const rows = computed(() => [...bindings.value].sort((a, b) => {
  const rank = { danger: 0, warning: 1, neutral: 2 };
  return rank[status(a).tone] - rank[status(b).tone] || a.id - b.id;
}));
const columns = computed<TableColumn[]>(() => [
  { key: "relationship", label: t("bind.relationship"), width: "25%", mobileWide: true },
  // 开关与操作列固定触控尺寸，数字列留足不折行宽度。
  { key: "enabled", label: t("common.enable"), width: "68px" },
  { key: "scale", label: t("bind.riskScale"), width: "7%", numeric: true },
  { key: "status", label: t("bind.runtimeStatus"), width: "21%", mobileWide: true },
  { key: "positions", label: t("bind.positionsAndDebt"), width: "12%", numeric: true },
  { key: "executions", label: t("bind.todayExecutions"), width: "15%", numeric: true },
  { key: "actions", label: t("common.actions"), width: "68px", mobileWide: true },
]);
const loading = ref(false);
const loadError = ref("");
const statistics = ref<Record<number, ExecutionCounts> | null>(null);
const statisticsError = ref("");
const statisticsLoading = ref(false);
const statisticsAsOf = ref<number | null>(null);
const editing = ref<Binding | null>(null);
const showModal = ref(false);
const initialRisk = ref<RiskSettings | null>(null);
const formElement = ref<HTMLFormElement | null>(null);
const form = ref({ name: "", leadId: null as number | null, accountId: null as number | null, riskScale: 1 as number | null,
  risk: { ...DEFAULT_RISK }, whitelist: "", blacklist: "", symbolMap: "" });
const errors = ref<Record<string, string>>({});
const environment = computed(() => editing.value?.accountEnvironment ?? accounts.value.find(account => account.id === form.value.accountId)?.environment ?? null);
const protectionLocked = computed(() => !!editing.value && (status(editing.value)?.positionCount == null ||
  status(editing.value)?.pendingCount == null || status(editing.value).positionCount! > 0 || status(editing.value).pendingCount! > 0));
const lockMessage = computed(() => t(editing.value && (status(editing.value)?.positionCount == null || status(editing.value)?.pendingCount == null)
  ? "bind.unverifiedProtection" : "bind.lockedProtection"));
const disabledRiskFields = computed(() => protectionLocked.value ? FLAT_REQUIRED_RISK_FIELDS : []);
const tableError = computed(() => loadError.value || (!showModal.value ? actionError.value : ""));
let loadSequence = 0;
let statisticsSequence = 0;
let pollTimer: ReturnType<typeof setInterval> | undefined;
function execCounts(binding: Binding): ExecutionCounts | null {
  if (binding.todaySuccessCount != null || binding.todayFailedCount != null) {
    return { success: binding.todaySuccessCount ?? 0, failed: binding.todayFailedCount ?? 0 };
  }
  return statistics.value?.[binding.id] ?? null;
}
async function refreshStatistics() {
  if (statisticsLoading.value || board.value?.dayStart == null || board.value.serverTime == null) return;
  if (bindings.value.length && bindings.value.every(binding => binding.todaySuccessCount != null)) {
    statisticsAsOf.value = board.value.serverTime;
    statisticsError.value = "";
    return;
  }
  const request = ++statisticsSequence;
  const cutoff = board.value.serverTime;
  statisticsLoading.value = true;
  try {
    const counts = await fetchTodayExecutionCounts(board.value.dayStart, cutoff, () => request === statisticsSequence);
    if (request === statisticsSequence) {
      statistics.value = counts; statisticsAsOf.value = cutoff; statisticsError.value = "";
    }
  } catch (cause: unknown) {
    if (request === statisticsSequence && !isCancelled(cause)) {
      statistics.value = null; statisticsAsOf.value = null; statisticsError.value = errorMessage(cause);
    }
  } finally { if (request === statisticsSequence) statisticsLoading.value = false; }
}
async function load(force = false) {
  const request = ++loadSequence;
  loading.value = true;
  if (force) { loadError.value = ""; actionError.value = ""; }
  try {
    await Promise.all([cache.fetchBindings(force), cache.fetchAccounts(force), cache.fetchLeads(force),
      cache.fetchSettings(force), cache.fetchBoard(force), cache.fetchIntents(force)]);
    if (request === loadSequence) { loadError.value = ""; void refreshStatistics(); }
  } catch (cause: unknown) {
    if (request === loadSequence && !isCancelled(cause)) loadError.value = errorMessage(cause);
  } finally { if (request === loadSequence) loading.value = false; }
}
// 跨日与登出都作废局部统计，不能把昨天或上一会话的计数继续标为“今日”。
watch(() => board.value?.dayStart, (dayStart, previous) => {
  if (dayStart === previous) return;
  statisticsSequence++; statisticsLoading.value = false; statistics.value = null; statisticsAsOf.value = null; statisticsError.value = "";
  if (dayStart != null && pollTimer) void refreshStatistics();
});
watch(() => cache.bindings, value => {
  if (value == null) { showModal.value = false; editing.value = null; errors.value = {}; actionError.value = ""; }
});
function open(binding: Binding | null) {
  editing.value = binding; errors.value = {}; actionError.value = "";
  initialRisk.value = binding ? bindingRisk(binding) : { ...(defaults.value ?? DEFAULT_RISK) };
  form.value = { name: binding?.name || "", leadId: binding?.leadId ?? null, accountId: binding?.accountId ?? null,
    riskScale: binding ? binding.riskScale : 1, risk: { ...initialRisk.value },
    whitelist: binding?.symbolWhitelist?.join(", ") || "", blacklist: binding?.symbolBlacklist?.join(", ") || "",
    symbolMap: Object.entries(binding?.symbolMap || {}).map(([source, target]) => `${source}=${target}`).join("\n") };
  showModal.value = true;
}
function symbols(value: string) { return [...new Set(value.split(/[\s,，]+/).map(symbol => symbol.trim().toUpperCase()).filter(Boolean))]; }
function sectionHasError(section: keyof typeof RISK_FIELD_GROUPS) {
  const fields: readonly string[] = section === "protection" ? [...RISK_FIELD_GROUPS.protection, ...FLAT_REQUIRED_RISK_FIELDS] :
    section === "risk" ? [...RISK_FIELD_GROUPS.risk, "enable_circuit_breaker", "enable_leverage_cap"] : [...RISK_FIELD_GROUPS.execution, "order_mode", "slippage_action"];
  return fields.some(field => !!errors.value[field]);
}
async function focusError() {
  await nextTick();
  formElement.value?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus();
}
async function saveBinding() {
  errors.value = validateFields(form.value.risk, RISK_RULES);
  const scaleError = validateNumber(form.value.riskScale, RISK_SCALE_RULE);
  if (scaleError) errors.value.riskScale = scaleError;
  if (!form.value.name.trim()) errors.value.name = t("validation.required");
  else if (form.value.name.trim().length > 120) errors.value.name = t("validation.maxLength", { n: 120 });
  if (!form.value.accountId || !form.value.leadId) errors.value.accountId = t("bind.errSelectBoth");
  for (const key of Object.keys(RISK_SWITCHES) as (keyof typeof RISK_SWITCHES)[]) if (form.value.risk[key] == null) errors.value[key] = t("validation.required");
  if (!ORDER_MODES.includes(form.value.risk.order_mode as typeof ORDER_MODES[number])) errors.value.order_mode = t("validation.invalid");
  if (!SLIPPAGE_ACTIONS.includes(form.value.risk.slippage_action as typeof SLIPPAGE_ACTIONS[number])) errors.value.slippage_action = t("validation.invalid");
  if (!TP_SL_MODES.includes(form.value.risk.tp_sl_mode as typeof TP_SL_MODES[number])) errors.value.tp_sl_mode = t("validation.invalid");
  const whitelist = symbols(form.value.whitelist);
  const blacklist = symbols(form.value.blacklist);
  if (whitelist.some(symbol => blacklist.includes(symbol))) errors.value.whitelist = t("bind.symbolConflict");
  const symbolMap: Record<string, string> = {};
  for (const line of form.value.symbolMap.split("\n").filter(line => line.trim())) {
    const [source, target, extra] = line.trim().toUpperCase().split("=").map(value => value.trim());
    if (!source || !target || extra != null || symbolMap[source]) { errors.value.symbolMap = t("bind.symbolMapError"); break; }
    symbolMap[source] = target;
  }
  if (protectionLocked.value && editing.value) {
    for (const key of FLAT_REQUIRED_RISK_FIELDS) if (form.value.risk[key] !== initialRisk.value?.[key]) errors.value[key] = lockMessage.value;
    const original = editing.value.symbolMap ?? {};
    if (Object.keys(symbolMap).length !== Object.keys(original).length || Object.keys(symbolMap).some(key => symbolMap[key] !== original[key])) {
      errors.value.symbolMap = lockMessage.value;
    }
  }
  if (Object.keys(errors.value).length) { await focusError(); return; }
  const reduced = riskReductions(initialRisk.value, form.value.risk);
  const payload = { name: form.value.name.trim(), risk_scale: form.value.riskScale, ...form.value.risk,
    symbol_whitelist: whitelist, symbol_blacklist: blacklist, symbol_map: symbolMap };
  // 新建实盘绑定默认停用，启用必须再走独立的强确认。
  const result = await run(() => editing.value ? api.put(`/api/bindings/${editing.value.id}`, payload) :
    api.post("/api/bindings", { ...payload, leadId: form.value.leadId, accountId: form.value.accountId, isEnabled: environment.value === "demo" }), {
    confirm: editing.value || environment.value !== "demo" || reduced.length > 0 ? { title: t("common.save"),
      message: reduced.length ? t("confirm.riskOff", { items: reduced.join("、") }) : t("confirm.edit", { name: form.value.name }),
      environment: environment.value, strong: environment.value == null || reduced.length > 0 } : undefined,
    onError: cause => { errors.value = fieldErrors(cause); void focusError(); },
  });
  if (result.ok) { showModal.value = false; await load(true); }
}
async function operate(binding: Binding, kind: "toggle" | "delete" | "reset-breaker" | "reset-debt" | "adopt-existing") {
  // 未知状态不能通过取反被解释为恢复交易。
  if (kind === "toggle" && binding.isEnabled == null) return;
  const title = t(kind === "delete" ? "common.delete" : kind === "toggle" ? (binding.isEnabled ? "common.disable" : "common.enable") : kind === "reset-breaker" ? "bind.resetBreaker" : kind === "adopt-existing" ? "bind.adoptExisting" : "bind.pendingDebt");
  const messageKey = kind === "delete" ? "confirm.delete" : kind === "toggle" ? "confirm.toggle" : kind === "reset-breaker" ? "confirm.resetBreaker" : kind === "adopt-existing" ? "confirm.adoptExisting" : "confirm.resetDebt";
  const result = await run(async () => {
    // PUT 明确目标状态，避免旧页面上的 toggle 意外恢复交易。
    if (kind === "toggle") await api.put(`/api/bindings/${binding.id}`, { isEnabled: !binding.isEnabled });
    else if (kind === "delete") await api.delete(`/api/bindings/${binding.id}?auto_disable=true`);
    else await api.post(`/api/bindings/${binding.id}/${kind}`);
  }, { confirm: { title, message: t(messageKey, { name: binding.name || "—", action: title }),
    environment: binding.accountEnvironment, strong: binding.accountEnvironment == null || kind === "reset-breaker" || (kind === "toggle" && !binding.isEnabled) } });
  if (result.ok) await load(true);
}
function start() {
  if (pollTimer) return;
  void load();
  pollTimer = setInterval(() => { if (!loading.value && !pending.value) void load(); }, 15000);
}
function stop() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = undefined; loadSequence++; statisticsSequence++; loading.value = false; statisticsLoading.value = false;
}
onMounted(start);
onActivated(start);
onDeactivated(stop);
onUnmounted(stop);
</script>
<template>
  <div class="min-w-0 space-y-5">
    <PageHeader :title="t('bind.title')" :summary="cache.bindings ? t('bind.summary', { n: summary.total, tripped: summary.tripped, disabled: summary.disabled, pending: summary.pending ?? t('common.unknown'), unknown: summary.unknown }) : t('common.loading')">
      <template #actions><button class="icon-button" :disabled="loading || pending" :aria-label="t('common.refresh')" :title="t('common.refresh')" @click="load(true)"><RefreshCw :size="18" :class="{ 'animate-spin': loading }" /></button><button class="button-primary" :disabled="pending || !cache.accounts || !cache.leads" @click="open(null)"><Plus :size="17" />{{ t("bind.newBtn") }}</button></template>
    </PageHeader>
    <div v-if="killSwitch === true" class="flex items-start gap-2.5 rounded-xl border border-danger/35 bg-danger/10 px-3.5 py-2.5 text-xs sm:text-sm text-danger" role="alert" data-testid="binding-kill-banner"><AlertTriangle :size="18" class="mt-0.5 shrink-0" /><p>{{ t("bind.killBanner") }}</p></div>
    <DataTable :rows="rows" :columns="columns" :row-key="binding => binding.id" :loading="loading" :error="tableError" :empty="t('bind.emptyTitle')" :label="t('bind.title')" :row-tone="binding => status(binding).tone === 'danger' ? 'bg-danger/10' : status(binding).tone === 'warning' ? 'bg-warning/5' : ''" @retry="load(true)">
      <template #cell-relationship="{ row }">
        <div class="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-sm font-semibold [overflow-wrap:anywhere]"><RouterLink v-if="row.leadId != null" :to="`/leads/${row.leadId}`" class="min-w-0 hover:text-accent">{{ row.leadName ?? t("common.unknown") }}</RouterLink><span v-else class="min-w-0">{{ row.leadName ?? t("common.unknown") }}</span><ArrowRight :size="14" class="shrink-0 text-muted" /><span class="min-w-0">{{ row.accountName ?? t("common.unknown") }}</span><EnvBadge :environment="row.accountEnvironment" /></div>
        <p class="mt-1 break-words text-xs text-muted">{{ row.name ?? t("common.unknown") }} · {{ row.accountExchange?.toUpperCase() ?? t("common.unknown") }}</p>
      </template>
      <template #cell-enabled="{ row }"><button class="icon-button" role="switch" :aria-checked="row.isEnabled === true" :disabled="pending || row.isEnabled == null" :aria-label="t('bind.enableSwitch', { name: row.name ?? t('common.unknown') })" :title="t(row.isEnabled == null ? 'state.UNKNOWN' : row.isEnabled ? 'common.disable' : 'common.enable')" @click="operate(row, 'toggle')"><span class="relative h-5 w-9 rounded-full" :class="row.isEnabled ? 'bg-accent' : 'bg-surface3'" aria-hidden="true"><span class="absolute top-1 h-3 w-3 rounded-full bg-foreground" :class="row.isEnabled ? 'right-1' : 'left-1'" /></span></button></template>
      <template #cell-scale="{ row }"><span class="font-semibold">{{ formatQty(row.riskScale) }}</span><span v-if="row.riskScale != null" class="ml-1 text-xs text-muted">{{ t("unit.x") }}</span></template>
      <template #cell-status="{ row }"><StatusBadge :state="status(row).state" /><p v-for="reason in status(row).reasons" :key="reason" class="mt-1 break-words text-xs" :class="status(row).tone === 'danger' ? 'text-danger' : 'text-muted'">{{ reasonText(reason) }}</p><RouterLink v-if="status(row).pendingCount != null && status(row).pendingCount! > 0" to="/executions?tab=intents" class="mt-1 block text-xs text-warning">{{ t("bind.pendingCount", { n: status(row).pendingCount ?? t("common.unknown") }) }}</RouterLink></template>
      <template #cell-positions="{ row }"><p class="font-semibold">{{ t("bind.positionCount", { n: status(row).positionCount ?? t("common.unknown") }) }}</p><p class="mt-1 text-xs" :class="row.pendingDebtUsd != null && row.pendingDebtUsd > 0 ? 'text-warning' : 'text-muted'">{{ formatUsd(row.pendingDebtUsd) }}</p></template>
      <template #cell-executions="{ row }"><p class="whitespace-nowrap text-xs" :class="(execCounts(row)?.failed ?? 0) > 0 ? 'text-danger' : 'text-muted'">{{ execCounts(row) == null ? t("common.unknown") : t("bind.executionCounts", { success: execCounts(row)?.success ?? 0, failed: execCounts(row)?.failed ?? 0 }) }}</p></template>
      <template #cell-actions="{ row }"><MoreMenu><button class="button-secondary justify-start" :disabled="pending" @click="open(row)"><Pencil :size="16" />{{ t("common.edit") }}</button><RouterLink :to="`/executions?binding_id=${row.id}`" class="button-secondary justify-start">{{ t("executions.title") }}</RouterLink><button class="button-secondary justify-start" :disabled="pending" @click="operate(row, 'adopt-existing')"><Download :size="16" />{{ t("bind.adoptExisting") }}</button><button v-if="row.circuitBreakerTripped === true" class="button-danger justify-start" :disabled="pending" @click="operate(row, 'reset-breaker')"><RotateCcw :size="16" />{{ t("bind.resetBreaker") }}</button><button v-if="row.pendingDebtUsd != null && row.pendingDebtUsd > 0" class="button-secondary justify-start text-warning" :disabled="pending" @click="operate(row, 'reset-debt')"><RotateCcw :size="16" />{{ t("bind.clearDebt", { amt: formatUsd(row.pendingDebtUsd) }) }}</button><button class="button-danger justify-start" :disabled="pending" @click="operate(row, 'delete')"><Trash2 :size="16" />{{ t("common.delete") }}</button></MoreMenu></template>
      <template #mobile-card="{ row }">
        <div class="space-y-2.5">
          <div class="flex items-start justify-between gap-2">
            <div class="min-w-0 flex-1">
              <div class="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-sm font-bold [overflow-wrap:anywhere]">
                <RouterLink v-if="row.leadId != null" :to="`/leads/${row.leadId}`" class="hover:text-accent">{{ row.leadName ?? t("common.unknown") }}</RouterLink>
                <span v-else>{{ row.leadName ?? t("common.unknown") }}</span>
                <ArrowRight :size="13" class="shrink-0 text-muted" />
                <span>{{ row.accountName ?? t("common.unknown") }}</span>
                <EnvBadge :environment="row.accountEnvironment" />
              </div>
              <p class="mt-0.5 truncate text-xs text-muted">{{ row.name ?? t("common.unknown") }} · {{ row.accountExchange?.toUpperCase() ?? t("common.unknown") }}</p>
            </div>
            <div class="flex shrink-0 items-center gap-1.5">
              <button class="inline-flex min-h-9 items-center" role="switch" :aria-checked="row.isEnabled === true" :disabled="pending || row.isEnabled == null" :aria-label="t('bind.enableSwitch', { name: row.name ?? t('common.unknown') })" @click="operate(row, 'toggle')"><span class="relative h-5 w-9 rounded-full" :class="row.isEnabled ? 'bg-accent' : 'bg-surface3'" aria-hidden="true"><span class="absolute top-1 h-3 w-3 rounded-full bg-foreground" :class="row.isEnabled ? 'right-1' : 'left-1'" /></span></button>
              <MoreMenu><button class="button-secondary justify-start" :disabled="pending" @click="open(row)"><Pencil :size="16" />{{ t("common.edit") }}</button><RouterLink :to="`/executions?binding_id=${row.id}`" class="button-secondary justify-start">{{ t("executions.title") }}</RouterLink><button class="button-secondary justify-start" :disabled="pending" @click="operate(row, 'adopt-existing')"><Download :size="16" />{{ t("bind.adoptExisting") }}</button><button v-if="row.circuitBreakerTripped === true" class="button-danger justify-start" :disabled="pending" @click="operate(row, 'reset-breaker')"><RotateCcw :size="16" />{{ t("bind.resetBreaker") }}</button><button v-if="row.pendingDebtUsd != null && row.pendingDebtUsd > 0" class="button-secondary justify-start text-warning" :disabled="pending" @click="operate(row, 'reset-debt')"><RotateCcw :size="16" />{{ t("bind.clearDebt", { amt: formatUsd(row.pendingDebtUsd) }) }}</button><button class="button-danger justify-start" :disabled="pending" @click="operate(row, 'delete')"><Trash2 :size="16" />{{ t("common.delete") }}</button></MoreMenu>
            </div>
          </div>
          <div class="flex flex-wrap items-center gap-2">
            <StatusBadge :state="status(row).state" />
            <span v-for="reason in status(row).reasons" :key="reason" class="text-xs" :class="status(row).tone === 'danger' ? 'text-danger' : 'text-muted'">{{ reasonText(reason) }}</span>
            <RouterLink v-if="status(row).pendingCount != null && status(row).pendingCount! > 0" to="/executions?tab=intents" class="text-xs text-warning">{{ t("bind.pendingCount", { n: status(row).pendingCount ?? t("common.unknown") }) }}</RouterLink>
          </div>
          <div class="grid grid-cols-3 gap-2 rounded-lg bg-background/40 px-2.5 py-2 text-xs">
            <div>
              <span class="text-muted">{{ t("bind.riskScale") }}</span>
              <p class="mt-0.5 font-mono font-bold tabular-nums">{{ formatQty(row.riskScale) }}<span v-if="row.riskScale != null" class="ml-0.5 text-[11px] font-normal text-muted">{{ t("unit.x") }}</span></p>
            </div>
            <div>
              <span class="text-muted">{{ t("bind.positionsAndDebt") }}</span>
              <p class="mt-0.5 font-mono font-semibold tabular-nums">{{ t("bind.positionCount", { n: status(row).positionCount ?? t("common.unknown") }) }}</p>
              <p v-if="(row.pendingDebtUsd ?? 0) > 0" class="font-mono text-[11px] text-warning tabular-nums">{{ formatUsd(row.pendingDebtUsd) }}</p>
            </div>
            <div class="text-right">
              <span class="text-muted">{{ t("bind.todayExecutions") }}</span>
              <p class="mt-0.5 whitespace-nowrap font-mono tabular-nums" :class="(execCounts(row)?.failed ?? 0) > 0 ? 'text-danger' : 'text-muted'">{{ execCounts(row) == null ? t("common.unknown") : t("bind.executionCounts", { success: execCounts(row)?.success ?? 0, failed: execCounts(row)?.failed ?? 0 }) }}</p>
            </div>
          </div>
        </div>
      </template>
    </DataTable>
    <footer v-if="bindings.length" class="flex flex-wrap items-center justify-between gap-2 text-xs text-muted">
      <span v-if="statisticsAsOf" class="inline-flex items-center gap-2">{{ t("bind.statsAsOf") }}<FreshnessStamp :timestamp="statisticsAsOf" /></span>
      <span v-else-if="statisticsError" class="text-warning">{{ t("bind.statsError") }} · {{ statisticsError }}</span>
      <span v-else-if="statisticsLoading">{{ t("common.loading") }}</span>
      <RouterLink to="/executions" class="text-accent">{{ t("executions.title") }}</RouterLink>
    </footer>
    <BaseModal :open="showModal" :title="t(editing ? 'bind.editModalTitle' : 'bind.modalTitle')" :busy="pending" wide @close="showModal = false">
      <form ref="formElement" novalidate @submit.prevent="saveBinding"><fieldset class="space-y-5" :disabled="pending">
        <p v-if="actionError" class="error-banner" role="alert">{{ actionError }}</p>
        <div class="flex flex-wrap items-center gap-3"><EnvBadge :environment="environment" /><span v-if="!editing && environment === 'live'" class="text-sm text-warning">{{ t("bind.liveDisabled") }}</span></div>
        <details open class="rounded-xl border border-border/50 bg-background/40 px-3.5 py-2.5"><summary class="flex min-h-10 cursor-pointer items-center justify-between gap-2 text-sm font-semibold">{{ t("bind.basicSection") }}<ChevronDown :size="17" /></summary>
          <div class="mt-3 grid gap-4 sm:grid-cols-2">
            <label class="field-label">{{ t("bind.label") }} *<input v-model="form.name" name="name" class="form-input mt-1" :aria-invalid="!!errors.name" required maxlength="120" /><span v-if="errors.name" class="field-error" role="alert">{{ errors.name }}</span></label>
            <NumberField v-model="form.riskScale" name="riskScale" :rule="RISK_SCALE_RULE" :error="errors.riskScale || errors.risk_scale" />
            <label class="field-label">{{ t("bind.leadSource") }} *<select v-model="form.leadId" class="form-input mt-1" :disabled="!!editing" :aria-invalid="!!errors.lead_id" required><option :value="null">{{ t("bind.selectLead") }}</option><option v-for="lead in leads" :key="lead.id" :value="lead.id">{{ lead.name ?? t("common.unknown") }}</option></select><span v-if="errors.lead_id" class="field-error" role="alert">{{ errors.lead_id }}</span></label>
            <label class="field-label">{{ t("bind.followerAccount") }} *<select v-model="form.accountId" class="form-input mt-1" :disabled="!!editing" :aria-invalid="!!(errors.accountId || errors.account_id)" required><option :value="null">{{ t("bind.selectAccount") }}</option><option v-for="account in accounts" :key="account.id" :value="account.id">{{ account.name ?? t("common.unknown") }} · {{ t(account.environment === 'live' ? 'env.live' : account.environment === 'demo' ? 'env.demo' : 'common.unknown') }}</option></select><span v-if="errors.accountId || errors.account_id" class="field-error" role="alert">{{ errors.accountId || errors.account_id }}</span></label>
            <div class="sm:col-span-2"><RiskPolicyFields v-model="form.risk" section="execution" :errors="errors" :defaults="defaults" /></div>
          </div>
        </details>
        <details :open="sectionHasError('risk')" class="rounded-xl border border-border/50 bg-background/40 px-3.5 py-2.5"><summary class="flex min-h-10 cursor-pointer items-center justify-between gap-2 text-sm font-semibold">{{ t("bind.riskSection") }}<ChevronDown :size="17" /></summary><div class="mt-3"><RiskPolicyFields v-model="form.risk" section="risk" :errors="errors" :defaults="defaults" /></div></details>
        <details :open="sectionHasError('protection')" class="rounded-xl border border-border/50 bg-background/40 px-3.5 py-2.5"><summary class="flex min-h-10 cursor-pointer items-center justify-between gap-2 text-sm font-semibold">{{ t("bind.protectionSection") }}<ChevronDown :size="17" /></summary><div class="mt-3 space-y-3"><p v-if="protectionLocked" class="text-xs text-warning">{{ lockMessage }}</p><RiskPolicyFields v-model="form.risk" section="protection" :errors="errors" :defaults="defaults" :disabled-fields="disabledRiskFields" /></div></details>
        <details :open="!!(errors.whitelist || errors.symbol_whitelist || errors.symbol_blacklist || errors.symbolMap || errors.symbol_map)" class="rounded-xl border border-border/50 bg-background/40 px-3.5 py-2.5"><summary class="flex min-h-10 cursor-pointer items-center justify-between gap-2 text-sm font-semibold">{{ t("bind.filterSection") }}<ChevronDown :size="17" /></summary>
          <div class="mt-3 grid gap-4 sm:grid-cols-2">
            <label class="field-label">{{ t("bind.symbolWhitelist") }}<textarea v-model="form.whitelist" :placeholder="t('bind.symbolPlaceholder')" :aria-invalid="!!(errors.whitelist || errors.symbol_whitelist)" class="form-input mt-1" rows="2" /><span class="mt-1 block text-xs text-muted">{{ t("bind.whitelistHelp") }}</span><span v-if="errors.whitelist || errors.symbol_whitelist" class="field-error" role="alert">{{ errors.whitelist || errors.symbol_whitelist }}</span></label>
            <label class="field-label">{{ t("bind.symbolBlacklist") }}<textarea v-model="form.blacklist" :placeholder="t('bind.symbolPlaceholder')" :aria-invalid="!!errors.symbol_blacklist" class="form-input mt-1" rows="2" /><span class="mt-1 block text-xs text-muted">{{ t("bind.blacklistHelp") }}</span><span v-if="errors.symbol_blacklist" class="field-error" role="alert">{{ errors.symbol_blacklist }}</span></label>
            <label class="field-label sm:col-span-2">{{ t("bind.symbolMap") }}<textarea v-model="form.symbolMap" :placeholder="t('bind.symbolMapPlaceholder')" :disabled="protectionLocked" :aria-invalid="!!(errors.symbolMap || errors.symbol_map)" class="form-input mt-1 font-mono" rows="3" /><span class="mt-1 block text-xs" :class="protectionLocked ? 'text-warning' : 'text-muted'">{{ protectionLocked ? lockMessage : t("bind.mappingHelp") }}</span><span v-if="errors.symbolMap || errors.symbol_map" class="field-error" role="alert">{{ errors.symbolMap || errors.symbol_map }}</span></label>
          </div>
        </details>
        <footer class="flex justify-end gap-3"><button type="button" class="button-secondary" @click="showModal = false">{{ t("common.cancel") }}</button><button class="button-primary" type="submit"><Save :size="17" />{{ t(pending ? "common.working" : "common.save") }}</button></footer>
      </fieldset></form>
    </BaseModal>
  </div>
</template>
