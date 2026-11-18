<script setup lang="ts">
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ChevronLeft, ChevronRight, RefreshCw } from "lucide-vue-next";
import { isCancelled } from "@/api/client";
import { useDataCacheStore } from "@/stores/dataCache";
import { useSocketStore } from "@/stores/socket";
import { useI18n } from "@/i18n";
import { useDisplayUnit } from "@/composables/useDisplayUnit";
import type { ExecutionRecord } from "@/types/api";
import { errorMessage, reasonText } from "@/utils/errors";
import { actionLabel, sideLabel, statusLabel } from "@/utils/status";
import { formatDateTime, formatPct, formatPrice, timestampMs } from "@/utils/format";
import { EXECUTION_FILTERS, executionGroup, readExecutionCounts, readExecutionPage, type ExecutionCounts, type ExecutionFilter } from "@/utils/executions";
import PageHeader from "@/components/PageHeader.vue";
import DataTable from "@/components/DataTable.vue";
import ErrorState from "@/components/ErrorState.vue";
import BaseModal from "@/components/BaseModal.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import EnvBadge from "@/components/EnvBadge.vue";
import FreshnessStamp from "@/components/FreshnessStamp.vue";
import IntentList from "@/components/IntentList.vue";

defineOptions({ name: "ExecutionsView" });
const cache = useDataCacheStore();
const socket = useSocketStore();
const route = useRoute();
const router = useRouter();
const { t } = useI18n();
const { unit, sizeText } = useDisplayUnit();
const sizeLabel = computed(() => unit.value === "notional" ? t("board.notional") : unit.value === "quantity" ? t("common.quantity") : `${t("common.quantity")} / ${t("board.notional")}`);
const tab = computed(() => route.query.tab === "intents" ? "intents" : "history");
const bindingId = ref("");
const filter = ref<ExecutionFilter>("all");
const page = ref(0);
const pageSize = 20;
const items = ref<ExecutionRecord[]>([]);
const total = ref<number | null>(null);
const loading = ref(false);
const error = ref("");
const metadataError = ref("");
const counts = ref<ExecutionCounts>({ all: null, success: null, failed: null, skipped: null, blocked: null });
const countsLoading = ref(false);
const countsError = ref("");
const receivedAt = ref<number | null>(null);
const countsReceivedAt = ref<number | null>(null);
const intentLoading = ref(false);
const intentError = ref("");
const selected = ref<ExecutionRecord | null>(null);
const bindings = computed(() => cache.bindings?.data.items ?? []);
const selectedBinding = computed(() => bindingId.value ? Number(bindingId.value) : null);
const intents = computed(() => [...(cache.intents?.data ?? [])]
  .filter(intent => selectedBinding.value == null || intent.bindingId === selectedBinding.value)
  .sort((a, b) => (timestampMs(b.createdAt) ?? 0) - (timestampMs(a.createdAt) ?? 0)));
const summary = computed(() => tab.value === "intents"
  ? t("executions.pendingSummary", { n: cache.intents ? intents.value.length : t("common.unknown") })
  : t("executions.summary", { total: counts.value.all ?? t("common.unknown"), failed: counts.value.failed ?? t("common.unknown"),
    blocked: counts.value.blocked ?? t("common.unknown"), skipped: counts.value.skipped ?? t("common.unknown") }));
let active = false;
let sequence = 0;
let countSequence = 0;
let intentSequence = 0;
let timer: ReturnType<typeof setInterval> | undefined;
let eventTimer: ReturnType<typeof setTimeout> | undefined;

function environment(record: ExecutionRecord) {
  return record.environment ?? bindings.value.find(binding => binding.id === record.bindingId)?.accountEnvironment
    ?? cache.accounts?.data.find(account => account.id === record.accountId)?.environment ?? null;
}
function bindingName(record: ExecutionRecord) { return record.bindingName ?? bindings.value.find(binding => binding.id === record.bindingId)?.name ?? t("common.unknown"); }
function accountName(record: ExecutionRecord) { return record.accountName ?? cache.accounts?.data.find(account => account.id === record.accountId)?.name ?? t("common.unknown"); }
function recordKey(record: ExecutionRecord) { return record.id ?? `${record.bindingId}:${record.createdAt}:${record.symbol}:${record.action}:${record.orderId}`; }
function displayStatus(record: ExecutionRecord) { return record.status?.toUpperCase() === "PARTIAL" ? "PARTIALLY_FILLED" : record.status; }
function rowTone(record: ExecutionRecord) {
  const group = executionGroup(record.status);
  return group === "failed" || group === "blocked" ? "bg-danger/5" : group === "success" ? "" : "bg-warning/5";
}
function resultReason(record: ExecutionRecord) {
  if (record.details?.trim()) return reasonText(record.details);
  const status = record.status?.toUpperCase();
  if (status === "PARTIAL" || status === "PARTIALLY_FILLED") return t("executions.partialWarning");
  if (status === "UNKNOWN" || status === "SUBMITTING") return t("executions.unconfirmedWarning");
  if (status === "DEFERRED_SOURCE_UNPROVEN") return t("executions.deferredWarning");
  if (status === "ACCUMULATED_DEBT") return reasonText("WAITING_EXECUTION");
  return statusLabel(displayStatus(record));
}
async function loadMetadata(force = false) {
  try { await Promise.all([cache.fetchBindings(force), cache.fetchAccounts(force)]); if (active) metadataError.value = ""; }
  catch (cause: unknown) { if (active && !isCancelled(cause)) metadataError.value = errorMessage(cause); }
}
async function loadCounts() {
  const request = ++countSequence;
  countsLoading.value = true;
  try {
    const result = await readExecutionCounts(selectedBinding.value);
    if (request !== countSequence || !active) return;
    counts.value = result; countsError.value = ""; countsReceivedAt.value = Date.now();
  } catch (cause: unknown) {
    if (request === countSequence && active && !isCancelled(cause)) {
      countsError.value = errorMessage(cause);
      counts.value = { all: null, success: null, failed: null, skipped: null, blocked: null };
    }
  } finally { if (request === countSequence) countsLoading.value = false; }
}
async function loadHistory(refreshCounts = false) {
  if (!active || tab.value !== "history") return;
  const request = ++sequence;
  loading.value = true; error.value = "";
  if (refreshCounts) void loadCounts();
  try {
    const response = await readExecutionPage({ bindingId: selectedBinding.value, filter: filter.value, page: page.value, pageSize });
    // 快速筛选、翻页和离开页面时，旧响应不能覆盖当前视图。
    if (request !== sequence || !active) return;
    items.value = response.items; total.value = response.total; receivedAt.value = Date.now();
    counts.value = { ...counts.value, [filter.value]: response.total };
  } catch (cause: unknown) { if (request === sequence && active && !isCancelled(cause)) error.value = errorMessage(cause); }
  finally { if (request === sequence) loading.value = false; }
}
async function loadIntents(force = false) {
  const request = ++intentSequence;
  intentLoading.value = true;
  try { await cache.fetchIntents(force); if (request === intentSequence && active) intentError.value = ""; }
  catch (cause: unknown) { if (request === intentSequence && active && !isCancelled(cause)) intentError.value = errorMessage(cause); }
  finally { if (request === intentSequence) intentLoading.value = false; }
}
function refresh() {
  void loadMetadata(true);
  if (tab.value === "intents") void loadIntents(true);
  else void loadHistory(true);
}
function resetPage() { page.value = 0; items.value = []; total.value = null; selected.value = null; sequence++; }
function changeBinding() {
  resetPage();
  countSequence++; countsLoading.value = false; countsError.value = "";
  counts.value = { all: null, success: null, failed: null, skipped: null, blocked: null };
  countsReceivedAt.value = null;
  if (tab.value === "history") void loadHistory(true);
}
function chooseFilter(value: ExecutionFilter) {
  if (filter.value === value) return;
  filter.value = value; resetPage(); void loadHistory();
}
function changePage(delta: number) {
  page.value = Math.max(0, page.value + delta);
  items.value = []; selected.value = null; void loadHistory();
}
function chooseTab(value: "history" | "intents") { void router.replace({ query: { ...route.query, tab: value === "intents" ? value : undefined } }); }
function start() {
  if (active) return;
  active = true;
  void loadMetadata();
  void loadIntents();
  if (tab.value === "history") void loadHistory(true);
  timer = setInterval(() => { if (tab.value === "history" && !loading.value && !countsLoading.value) void loadHistory(true); }, 15000);
}
function stop() {
  active = false; sequence++; countSequence++; intentSequence++;
  clearInterval(timer); clearTimeout(eventTimer); timer = undefined; eventTimer = undefined;
  loading.value = false; countsLoading.value = false; intentLoading.value = false; selected.value = null;
}
watch(tab, () => {
  selected.value = null; sequence++; loading.value = false;
  if (!active) return;
  if (tab.value === "intents") { countSequence++; countsLoading.value = false; void loadIntents(); }
  else void loadHistory(countsReceivedAt.value == null || Date.now() - countsReceivedAt.value >= 15000);
});
watch(() => socket.eventSequence, () => {
  if (!active || tab.value !== "history") return;
  clearTimeout(eventTimer);
  eventTimer = setTimeout(() => { void loadHistory(countsReceivedAt.value == null || Date.now() - countsReceivedAt.value >= 15000); }, 300);
});
onMounted(start); onActivated(start); onDeactivated(stop); onUnmounted(stop);
</script>

<template>
  <div class="min-w-0 space-y-5">
    <PageHeader :title="t('executions.title')" :summary="summary">
      <template #actions><button class="icon-button" :disabled="loading || countsLoading || intentLoading" :title="t('common.refresh')" :aria-label="t('common.refresh')" @click="refresh"><RefreshCw :size="18" :class="{ 'animate-spin': loading || intentLoading }" /></button></template>
    </PageHeader>
    <ErrorState v-if="metadataError" :message="metadataError" @retry="loadMetadata(true)" />
    <div class="flex flex-wrap items-end justify-between gap-4 border-b border-border/50">
      <nav class="flex min-w-0 flex-wrap" role="tablist" :aria-label="t('executions.title')">
        <button class="tab-button" role="tab" :aria-selected="tab === 'history'" @click="chooseTab('history')">{{ t("executions.historyTab") }}</button>
        <button class="tab-button flex items-center gap-2" role="tab" :aria-selected="tab === 'intents'" @click="chooseTab('intents')">{{ t("executions.intentsTab") }}<span v-if="cache.intents && cache.intents.data.length" class="rounded bg-warning/15 px-1.5 text-xs tabular-nums text-warning">{{ cache.intents.data.length }}</span></button>
      </nav>
      <div class="mb-2 w-full sm:w-64">
        <label for="execution-binding" class="field-label mb-1">{{ t("executions.binding") }}</label>
        <select id="execution-binding" v-model="bindingId" class="form-input" @change="changeBinding">
          <option value="">{{ t("common.all") }}</option>
          <option v-for="binding in bindings" :key="binding.id" :value="String(binding.id)">{{ binding.name ?? t("common.unknown") }}</option>
        </select>
      </div>
    </div>
    <template v-if="tab === 'history'">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div class="flex min-w-0 flex-wrap gap-1 rounded-md bg-surface p-1" role="group" :aria-label="t('executions.filters')">
          <button v-for="value in EXECUTION_FILTERS" :key="value" :data-testid="`filter-${value}`" class="min-h-11 rounded px-3 py-2 text-sm"
            :aria-pressed="filter === value" :class="filter === value ? 'bg-surface3 font-semibold text-foreground' : 'text-muted hover:bg-surface2'" @click="chooseFilter(value)">
            {{ t(`executions.filter.${value}`) }} <span class="ml-1 tabular-nums" :class="value === 'failed' || value === 'blocked' ? 'text-danger' : ''">{{ counts[value] ?? t("common.unknown") }}</span>
          </button>
        </div>
        <p class="flex flex-wrap items-center gap-2 text-xs text-muted">{{ t("executions.updated") }}<FreshnessStamp :timestamp="receivedAt" :warn-after="30" :critical-after="90" :stale="!!error" /></p>
      </div>
      <ErrorState v-if="countsError" :message="`${t('executions.countError')}: ${countsError}`" :busy="countsLoading" @retry="loadCounts" />
      <DataTable :rows="items" :row-key="recordKey" :row-tone="rowTone" :loading="loading" :error="error" :empty="t('executions.empty')"
        :label="t('executions.title')" selectable @select="selected = $event" @retry="loadHistory(true)" :columns="[
          { key: 'result', label: t('executions.result'), width: '22%', mobileWide: true },
          { key: 'symbol', label: t('feed.colDirSym'), width: '13%' },
          { key: 'binding', label: t('executions.binding'), width: '18%', mobileWide: true },
          { key: 'lead', label: t('executions.leadReference'), width: '13%', numeric: true },
          { key: 'fill', label: t('executions.fill'), width: '13%', numeric: true },
          { key: 'slippage', label: t('executions.slippage'), width: '9%', numeric: true },
          { key: 'time', label: t('common.time'), width: '12%', numeric: true },
        ]">
        <template #cell-result="{ row }">
          <StatusBadge :state="displayStatus(row)" />
          <p v-if="executionGroup(row.status) !== 'success'" class="mt-1.5 text-xs leading-relaxed" :class="executionGroup(row.status) === 'failed' || executionGroup(row.status) === 'blocked' ? 'text-danger' : 'text-warning'">{{ resultReason(row) }}</p>
        </template>
        <template #cell-symbol="{ row }"><strong>{{ row.symbol ?? row.instId ?? t("common.unknown") }}</strong><p class="mt-1 text-xs text-muted">{{ sideLabel(row.positionSide) }} · {{ actionLabel(row.action) }}</p></template>
        <template #cell-binding="{ row }">
          <strong>{{ row.leadName ?? t("common.unknown") }}</strong><p class="mt-1 text-xs text-muted">{{ bindingName(row) }}</p>
          <div class="mt-1.5 flex flex-wrap items-center gap-2"><span class="text-xs text-muted">{{ accountName(row) }}</span><EnvBadge :environment="environment(row)" /></div>
        </template>
        <template #cell-lead="{ row }"><span>{{ sizeText(row.leadQty, row.leadNotional, row.leadPrice) }}</span><p class="mt-1 text-xs text-muted">{{ formatPrice(row.leadPrice) }}</p></template>
        <template #cell-fill="{ row }"><strong>{{ sizeText(row.myQty, row.myNotional, row.myPrice) }}</strong><p class="mt-1 text-xs text-muted">{{ formatPrice(row.myPrice) }}</p></template>
        <template #cell-slippage="{ row }"><span>{{ formatPct(row.slippagePct, 4) }}</span></template>
        <template #cell-time="{ row }"><span class="text-xs tabular-nums">{{ formatDateTime(row.createdAt) }}</span></template>
        <template #select-label>{{ t("executions.expand") }}</template>
      </DataTable>
      <div class="flex flex-wrap items-center justify-between gap-3">
        <p class="text-xs text-muted">{{ t("common.page", { page: page + 1, total: total ?? t("common.unknown") }) }}</p>
        <div class="flex gap-2">
          <button class="icon-button" :disabled="loading || page === 0" :aria-label="t('common.previous')" :title="t('common.previous')" @click="changePage(-1)"><ChevronLeft :size="19" /></button>
          <button class="icon-button" :disabled="loading || !items.length || (total != null ? (page + 1) * pageSize >= total : items.length < pageSize)" :aria-label="t('common.next')" :title="t('common.next')" @click="changePage(1)"><ChevronRight :size="19" /></button>
        </div>
      </div>
    </template>
    <template v-else>
      <p class="flex flex-wrap items-center gap-2 text-xs text-muted">{{ t("executions.updated") }}<FreshnessStamp :timestamp="cache.intents?.fetchedAt" :warn-after="15" :critical-after="45" :stale="!!intentError" /></p>
      <IntentList :items="intents" :loading="intentLoading" :error="intentError" @retry="loadIntents(true)" />
    </template>
    <BaseModal drawer :open="selected != null" :title="t('executions.details')" @close="selected = null">
      <div v-if="selected" class="space-y-5">
        <header class="space-y-2">
          <div class="flex flex-wrap items-center gap-2"><StatusBadge :state="displayStatus(selected)" large /><EnvBadge :environment="environment(selected)" /></div>
          <h3 class="section-title">{{ selected.symbol ?? selected.instId ?? t("common.unknown") }} · {{ sideLabel(selected.positionSide) }} · {{ actionLabel(selected.action) }}</h3>
          <p class="text-sm text-muted">{{ selected.leadName ?? t("common.unknown") }} · {{ bindingName(selected) }} · {{ accountName(selected) }}</p>
        </header>
        <p v-if="executionGroup(selected.status) !== 'success'" class="warning-banner">{{ resultReason(selected) }}</p>
        <dl class="grid grid-cols-2 gap-4">
          <div class="number"><dt class="text-xs text-muted">{{ t("executions.leadReference") }} · {{ sizeLabel }}</dt><dd class="mt-1 font-semibold">{{ sizeText(selected.leadQty, selected.leadNotional, selected.leadPrice) }}</dd></div>
          <div class="number"><dt class="text-xs text-muted">{{ t("executions.leadReference") }} · {{ t("common.price") }}</dt><dd class="mt-1 font-semibold">{{ formatPrice(selected.leadPrice) }}</dd></div>
          <div class="number"><dt class="text-xs text-muted">{{ t("executions.fill") }} · {{ sizeLabel }}</dt><dd class="mt-1 font-semibold">{{ sizeText(selected.myQty, selected.myNotional, selected.myPrice) }}</dd></div>
          <div class="number"><dt class="text-xs text-muted">{{ t("executions.fill") }} · {{ t("common.price") }}</dt><dd class="mt-1 font-semibold">{{ formatPrice(selected.myPrice) }}</dd></div>
          <div class="number"><dt class="text-xs text-muted">{{ t("executions.slippage") }}</dt><dd class="mt-1">{{ formatPct(selected.slippagePct, 4) }}</dd></div>
          <div class="number"><dt class="text-xs text-muted">{{ t("common.time") }}</dt><dd class="mt-1 text-xs">{{ formatDateTime(selected.createdAt) }}</dd></div>
          <div class="col-span-2 min-w-0"><dt class="text-xs text-muted">{{ t("executions.orderId") }}</dt><dd class="mt-1 break-all font-mono text-sm">{{ selected.orderId ?? t("common.unknown") }}</dd></div>
          <div class="col-span-2 min-w-0"><dt class="text-xs text-muted">{{ t("executions.details") }}</dt><dd class="mt-1 whitespace-pre-wrap break-words text-sm leading-relaxed">{{ reasonText(selected.details) }}</dd></div>
        </dl>
      </div>
    </BaseModal>
  </div>
</template>
