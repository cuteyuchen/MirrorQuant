<script setup lang="ts">
import { computed, onActivated, onMounted, ref } from "vue";
import { ChevronDown, Pencil, Plus, RefreshCw, Trash2, Wifi } from "lucide-vue-next";
import { api, ApiError, isCancelled } from "@/api/client";
import { useDataCacheStore } from "@/stores/dataCache";
import { useBoardStore } from "@/stores/board";
import { useAsyncAction } from "@/composables/useAsyncAction";
import { useNow } from "@/composables/useNow";
import { useI18n } from "@/i18n";
import { useDisplayUnit } from "@/composables/useDisplayUnit";
import type { Account, AccountTestResult } from "@/types/api";
import { finiteNumber, formatDateTime, formatPct, formatPrice, formatQty, formatUsd, signedUsd, timestampMs } from "@/utils/format";
import { pnlClass, sideLabel } from "@/utils/status";
import { errorMessage, reasonText } from "@/utils/errors";
import PageHeader from "@/components/PageHeader.vue";
import DataTable from "@/components/DataTable.vue";
import MoreMenu from "@/components/MoreMenu.vue";
import EmptyState from "@/components/EmptyState.vue";
import AccountFormModal from "@/components/AccountFormModal.vue";
import EnvBadge from "@/components/EnvBadge.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import FreshnessStamp from "@/components/FreshnessStamp.vue";

defineOptions({ name: "AccountsView" });
const cache = useDataCacheStore();
const board = useBoardStore();
const now = useNow();
const { t } = useI18n();
const { unit, showQuantity, sizeText } = useDisplayUnit();
const sizeLabel = computed(() => unit.value === "notional" ? t("board.notional") : unit.value === "quantity" ? t("common.quantity") : `${t("common.quantity")} / ${t("board.notional")}`);
const { pending, error, run } = useAsyncAction();
const accounts = computed(() => cache.accounts?.data || []);
const proxies = computed(() => cache.proxies?.data || []);
const showModal = ref(false);
const editing = ref<Account | null>(null);
const testResult = ref<Record<number, AccountTestResult>>({});
const expanded = ref<number | null>(null);
const loading = ref(false);
const loadError = ref("");
const rows = computed(() => [...accounts.value].sort((a, b) => severity(b) - severity(a) || a.id - b.id));
const summary = computed(() => cache.accounts ? t("accts.rowSummary", {
  n: accounts.value.length, issues: accounts.value.filter(account => !!issue(account)).length,
  disabled: accounts.value.filter(account => account.isEnabled === false).length,
}) : t("accts.summaryUnknown"));
let sequence = 0;
async function load(force = false) {
  if (loading.value && !force) return;
  const request = ++sequence; loading.value = true; loadError.value = "";
  // 代理及看板不可用不影响账户列表；金额未知仍显示横线。
  const results = await Promise.allSettled([cache.fetchAccounts(force), cache.fetchProxies(force), board.refresh(force)]);
  if (request !== sequence) return;
  const core = results[0];
  if (core.status === "rejected" && !isCancelled(core.reason)) loadError.value = errorMessage(core.reason);
  loading.value = false;
}
function accountBoard(account: Account) { return board.data?.accounts?.find(item => item.id === account.id); }
function metrics(account: Account) {
  // 资金摘要消费状态条正在刷新的同一份数据；最新 null 不能回退成旧的已知金额。
  const current = accountBoard(account);
  return current ? { equity: current.equity, available: current.available, unrealized: current.unrealizedPnl, syncedAt: current.lastSyncAt, status: current.status }
    : { equity: account.totalEquity ?? account.usdtEquity ?? null, available: account.availEquity, unrealized: account.unrealizedPnl, syncedAt: account.lastSyncAt, status: account.status };
}
function equity(account: Account) { return metrics(account).equity; }
function drawdown(account: Account) {
  const current = finiteNumber(equity(account)); const high = finiteNumber(account.hwmEquity);
  return current == null || high == null || high <= 0 ? null : Math.max(0, (high - current) / high * 100);
}
function issue(account: Account) {
  if (testResult.value[account.id]?.ok === false) return reasonText(testResult.value[account.id].error);
  if (account.lastError) return reasonText(account.lastError);
  if (account.riskEquityLegacyUnverified || accountBoard(account)?.riskLegacyUnverified) return t("accts.legacyShort");
  if (metrics(account).status?.toLowerCase() === "error") return t("accts.connectionError");
  if (account.isEnabled == null) return t("accts.enabledUnknown");
  if (account.isEnabled !== true) return "";
  const synced = timestampMs(metrics(account).syncedAt);
  if (synced == null) return t("accts.syncMissing");
  return now.value - synced > 90000 ? t("accts.syncStale") : "";
}
function severity(account: Account) { return issue(account) ? account.lastError || metrics(account).status?.toLowerCase() === "error" || testResult.value[account.id]?.ok === false ? 2 : 1 : 0; }
function rowTone(account: Account) { return severity(account) === 2 ? "bg-danger/5" : severity(account) === 1 ? "bg-warning/5" : ""; }
function toggleDetails(account: Account) { expanded.value = expanded.value === account.id ? null : account.id; }
function positionRows(account: Account) {
  // 数量和价格保留交易所实际单位；估算/开仓价不冒充实时行情。
  return (account.positions || []).map(position => {
    const qty = finiteNumber(position.pos ?? position.qty);
    const price = finiteNumber(position.currentPrice ?? position.curPx ?? position.markPx ?? position.markPrice);
    return {
      key: `${position.instId ?? position.symbol}:${position.posSide ?? position.positionSide}:${position.cTime ?? position.openTime}`,
      symbol: position.symbol ?? position.instId, side: sideLabel(position.posSide ?? position.positionSide, qty),
      qty: qty == null ? null : Math.abs(qty), entry: position.avgPx ?? position.avgEntry,
      price: position.priceSource === "entry" || position.priceSource === "unavailable" || price == null || price <= 0 ? null : price,
      priceSource: position.priceSource, pnl: position.upl ?? position.uPnL,
      pnlPct: position.uplRatio == null ? position.uPnLRoePct : position.uplRatio * 100, leverage: position.lever ?? position.entryLeverage,
    };
  });
}
function edit(account: Account | null) { editing.value = account; showModal.value = true; }
async function change(account: Account, operation: "toggle" | "delete", event?: Event) {
  // 取消确认时也保持开关显示服务器现态，不做乐观启用。
  if (event) (event.target as HTMLInputElement).checked = account.isEnabled === true;
  // 启用状态未知时不能通过取反猜测恢复交易。
  if (operation === "toggle" && account.isEnabled == null) return;
  const title = t(operation === "delete" ? "common.delete" : account.isEnabled ? "common.disable" : "common.enable");
  await run(async () => {
    if (operation === "delete") await api.delete(`/api/accounts/${account.id}`);
    else await api.put(`/api/accounts/${account.id}`, { isEnabled: !account.isEnabled });
    await cache.fetchAccounts(true);
    await board.refresh(true);
  }, { confirm: { title, message: t(operation === "delete" ? "confirm.delete" : "confirm.toggle", { name: account.name || "—", action: title }), environment: account.environment, strong: account.environment == null } });
}
async function test(account: Account) {
  await run(async () => {
    const response = await api.post<AccountTestResult>(`/api/accounts/${account.id}/test`);
    testResult.value[account.id] = response;
    await cache.fetchAccounts(true);
    if (response.ok !== true) throw new ApiError(400, response.error || t("accts.testUnknown"));
    await board.refresh(true);
  });
}
function sync(account: Account) { return run(async () => {
  const response = await api.post<{ ok: boolean | null; error?: string | null }>(`/api/accounts/${account.id}/sync`);
  await cache.fetchAccounts(true);
  if (response.ok !== true) throw new ApiError(400, response.error || t("accts.testUnknown"));
  await board.refresh(true);
}); }
// 直接消费带请求序号保护的缓存，避免视图再保存一份过时列表。
onMounted(() => { void load(); });
onActivated(() => { void load(); });
</script>
<template>
  <div class="space-y-5">
    <PageHeader :title="t('accts.title')" :summary="summary"><template #actions><button class="icon-button" :disabled="loading || pending" :aria-label="t('common.refresh')" :title="t('common.refresh')" @click="load(true)"><RefreshCw :size="18" :class="{ 'animate-spin': loading }" /></button><button class="button-primary" :disabled="pending" @click="edit(null)"><Plus :size="17" />{{ t("accts.addBtn") }}</button></template></PageHeader>
    <p v-if="error" class="error-banner" role="alert">{{ error }}</p>
    <DataTable :rows="rows" :label="t('accts.title')" :row-key="row => row.id" :row-tone="rowTone" :loading="loading" :error="loadError" :empty="t('accts.emptyTitle')" selectable :expanded="expanded" @select="toggleDetails" @retry="load(true)" :columns="[
      { key: 'account', label: t('accts.account'), width: '18%', mobileWide: true },
      { key: 'funds', label: t('accts.funds'), numeric: true, width: '14%' },
      { key: 'today', label: t('board.todayPnl'), numeric: true, width: '13%' },
      { key: 'unrealized', label: t('board.unrealized'), numeric: true, width: '11%' },
      { key: 'drawdown', label: t('accts.drawdown'), numeric: true, width: '9%' },
      { key: 'sync', label: t('accts.lastSync'), width: '10%' },
      { key: 'status', label: t('common.status'), mobileWide: true, width: '17%' },
      { key: 'actions', label: t('common.actions'), width: '8%' },
    ]">
      <!-- 无空格长名称也可收缩换行，不挤出固定尺寸的展开按钮。 -->
      <template #cell-account="{ row }"><p class="flex items-start justify-between gap-2 font-semibold"><span class="min-w-0 flex-1 [overflow-wrap:anywhere]">{{ row.name ?? t("common.unknown") }}</span><ChevronDown :size="16" class="mt-0.5 shrink-0 text-muted" :class="{ 'rotate-180': expanded === row.id }" aria-hidden="true" /></p><div class="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted"><span>{{ row.exchange?.toUpperCase() ?? t("common.unknown") }}</span><EnvBadge :environment="row.environment" /></div></template>
      <template #cell-funds="{ row }"><strong class="text-base">{{ formatUsd(equity(row)) }}</strong><p class="text-xs text-muted">{{ t("accts.available") }} {{ formatUsd(metrics(row).available) }}</p></template>
      <template #cell-today="{ row }"><strong :class="pnlClass(accountBoard(row)?.todayPnl)">{{ signedUsd(accountBoard(row)?.todayPnl) }}</strong><p class="text-xs" :class="pnlClass(accountBoard(row)?.todayPnlPct)">{{ formatPct(accountBoard(row)?.todayPnlPct) }}</p></template>
      <template #cell-unrealized="{ row }"><span :class="pnlClass(metrics(row).unrealized)">{{ signedUsd(metrics(row).unrealized) }}</span></template>
      <template #cell-drawdown="{ row }"><span :class="(drawdown(row) ?? 0) >= 20 ? 'text-danger' : 'text-muted'">{{ formatPct(drawdown(row), 2, false) }}</span></template>
      <template #cell-sync="{ row }"><FreshnessStamp :timestamp="metrics(row).syncedAt" :warn-after="45" :critical-after="90" /><p class="mt-1 text-xs text-muted">{{ formatDateTime(metrics(row).syncedAt, { short: true, withSeconds: false }) }}</p></template>
      <template #cell-status="{ row }"><div class="flex flex-wrap items-center gap-2"><StatusBadge :state="row.isEnabled == null ? 'unknown' : row.isEnabled === false ? 'disabled' : metrics(row).status" /><label class="inline-flex min-h-11 items-center gap-2 text-xs"><input type="checkbox" role="switch" :checked="row.isEnabled === true" :aria-label="t('accts.enabledFor', { name: row.name ?? '—' })" :disabled="pending || row.isEnabled == null" @change="change(row, 'toggle', $event)" /><span>{{ t(row.isEnabled == null ? "state.UNKNOWN" : row.isEnabled ? "common.enabled" : "common.disabled") }}</span></label></div><p v-if="issue(row)" class="mt-1 break-words text-xs" :class="severity(row) === 2 ? 'text-danger' : 'text-warning'">{{ issue(row) }}</p></template>
      <template #cell-actions="{ row }"><div class="flex items-center justify-end gap-1"><button class="icon-button" :disabled="pending" :aria-label="t('common.edit')" :title="t('common.edit')" @click="edit(row)"><Pencil :size="17" /></button><MoreMenu><button class="menu-item" :disabled="pending" @click="test(row)"><Wifi :size="16" />{{ t("accts.test") }}</button><button class="menu-item" :disabled="pending" @click="sync(row)"><RefreshCw :size="16" />{{ t("accts.sync") }}</button><button class="menu-item text-danger" :disabled="pending" @click="change(row, 'delete')"><Trash2 :size="16" />{{ t("common.delete") }}</button></MoreMenu></div></template>
      <template #select-label><ChevronDown :size="16" />{{ t("accts.positions") }}</template>
      <template #details="{ row }">
        <div class="space-y-4">
          <p v-if="row.riskEquityLegacyUnverified || accountBoard(row)?.riskLegacyUnverified" class="border-l-2 border-warning bg-warning/5 p-3 text-sm text-warning" role="alert">{{ t("accts.legacy") }}</p>
          <div class="flex flex-wrap items-center justify-between gap-3"><h2 class="section-title">{{ t("accts.positions") }}</h2><p v-if="showQuantity" class="text-xs text-muted">{{ row.exchange === "okx" ? t("accts.contractQty") : t("accts.baseQty") }}</p></div>
          <EmptyState v-if="!row.positions?.length" :title="t(row.positions == null ? 'accts.positionsUnknown' : 'accts.positionsEmpty')" />
          <div v-else class="divide-y divide-border/50">
            <article v-for="position in positionRows(row)" :key="position.key" class="grid items-center gap-3 py-3 md:grid-cols-[minmax(0,1.2fr)_repeat(4,minmax(0,1fr))]">
              <div class="min-w-0"><h3 class="break-words text-sm font-semibold">{{ position.symbol ?? t("common.unknown") }}</h3><p class="text-xs text-muted">{{ position.side }} · {{ t("unit.leverage", { n: formatQty(position.leverage) }) }}</p></div>
              <dl class="number"><dt class="text-xs text-muted">{{ sizeLabel }}</dt><dd>{{ sizeText(position.qty, null, position.price) }}</dd></dl>
              <dl class="number"><dt class="text-xs text-muted">{{ t("board.entry") }}</dt><dd>{{ formatPrice(position.entry) }}</dd></dl>
              <dl class="number"><dt class="text-xs text-muted">{{ t("board.mark") }}</dt><dd class="font-semibold">{{ formatPrice(position.price) }}</dd><dd v-if="position.priceSource === 'estimated' || position.priceSource === 'entry'" class="text-xs text-warning">{{ t(`accts.price_${position.priceSource}`) }}</dd></dl>
              <dl class="number"><dt class="text-xs text-muted">{{ t("common.pnl") }}</dt><dd :class="pnlClass(position.pnl)">{{ signedUsd(position.pnl) }} <span class="text-xs">{{ formatPct(position.pnlPct) }}</span></dd></dl>
            </article>
          </div>
        </div>
      </template>
    </DataTable>
    <AccountFormModal :open="showModal" :account="editing" :proxies="proxies" @close="showModal = false" @saved="load(true)" />
  </div>
</template>
