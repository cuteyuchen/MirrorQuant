<script setup lang="ts">
import { computed, inject, nextTick, onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from "vue";
import { routeLocationKey, routerKey } from "vue-router";
import { ArrowUpRight, ChevronDown, Pencil, Plus, RefreshCw, Trash2, Wifi } from "lucide-vue-next";
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
import EmptyState from "@/components/EmptyState.vue";
import AccountFormModal from "@/components/AccountFormModal.vue";
import EnvBadge from "@/components/EnvBadge.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import FreshnessStamp from "@/components/FreshnessStamp.vue";

defineOptions({ name: "AccountsView" });
const cache = useDataCacheStore();
const board = useBoardStore();
const route = inject(routeLocationKey, null);
const router = inject(routerKey, null);
function openDetail(id: number) {
  if (router) void router.push(`/accounts/${id}`);
}
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
const active = ref(false);
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
  // 数量和价格保留交易所实际单位；盈亏百分比按实际占用保证金 (ROE%) 计算，而非名义价值百分比。
  return (account.positions || []).map(position => {
    const qty = finiteNumber(position.pos ?? position.qty);
    const price = finiteNumber(position.currentPrice ?? position.curPx ?? position.markPx ?? position.markPrice);
    const validPrice = position.priceSource === "entry" || position.priceSource === "unavailable" || price == null || price <= 0 ? null : price;
    const upl = finiteNumber(position.upl ?? position.uPnL);
    const notional = finiteNumber(position.notionalUsd ?? position.markValue ?? position.notional)
      ?? (qty != null && validPrice != null ? Math.abs(qty) * validPrice : null);
    const leverage = finiteNumber(position.lever ?? position.entryLeverage);
    const rawMargin = finiteNumber(position.marginUsd);
    const marginUsd = rawMargin != null && rawMargin > 0
      ? rawMargin
      : (notional != null && notional > 0 && leverage != null && leverage > 0 ? notional / leverage : null);
    const fallbackPct = position.uplRatio == null ? finiteNumber(position.uPnLRoePct) : position.uplRatio * 100;
    const pnlPct = upl != null && marginUsd != null && marginUsd > 0 ? (upl / marginUsd) * 100 : fallbackPct;
    return {
      key: `${position.instId ?? position.symbol}:${position.posSide ?? position.positionSide}:${position.cTime ?? position.openTime}`,
      symbol: position.symbol ?? position.instId, side: sideLabel(position.posSide ?? position.positionSide, qty),
      qty: qty == null ? null : Math.abs(qty), entry: position.avgPx ?? position.avgEntry,
      price: validPrice,
      priceSource: position.priceSource, pnl: upl,
      pnlPct, leverage,
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
  let canForceDelete = false;
  const result = await run(async () => {
    if (operation === "delete") await api.delete(`/api/accounts/${account.id}?auto_disable=true`);
    else await api.put(`/api/accounts/${account.id}`, { isEnabled: !account.isEnabled });
    await cache.fetchAccounts(true);
    await board.refresh(true);
  }, {
    confirm: { title, message: t(operation === "delete" ? "confirm.delete" : "confirm.toggle", { name: account.name || "—", action: title }), environment: account.environment, strong: account.environment == null },
    onError: cause => {
      if (operation === "delete" && cause instanceof ApiError && cause.status === 409) {
        const detail = cause.data?.detail as Record<string, unknown> | undefined;
        if (detail?.code === "EXCHANGE_UNREACHABLE_CAN_FORCE") canForceDelete = true;
      }
    },
  });
  if (!result.ok && canForceDelete) {
    error.value = "";
    await run(async () => {
      await api.delete(`/api/accounts/${account.id}?auto_disable=true&force=true`);
      await cache.fetchAccounts(true);
      await board.refresh(true);
    }, { confirm: { title: t("common.delete"), message: t("confirm.forceDeleteAccount"), environment: account.environment, strong: true } });
  }
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
// 直接消费带请求序号保护的缓存，并跟随全局看板轮询/实时流同步持仓及浮盈。
watch(() => board.receivedAt, (next, previous) => {
  if (active.value && next && next !== previous && !loading.value) {
    void cache.fetchAccounts(true);
  }
});
watch(
  () => [route?.query?.account, rows.value.length] as const,
  ([raw]) => {
    const target = Number(Array.isArray(raw) ? raw[0] : raw);
    if (!Number.isInteger(target) || target <= 0) return;
    if (!rows.value.some(account => account.id === target)) return;
    expanded.value = target;
    void nextTick(() => {
      document.getElementById(`account-row-${target}`)?.scrollIntoView?.({ behavior: "smooth", block: "center" });
    });
  },
  { immediate: true },
);
onMounted(() => { active.value = true; void load(); });
onActivated(() => { active.value = true; void load(); });
onDeactivated(() => { active.value = false; });
onUnmounted(() => { active.value = false; });
</script>
<template>
  <div class="space-y-5">
    <PageHeader :title="t('accts.title')" :summary="summary"><template #actions><button class="icon-button" :disabled="loading || pending" :aria-label="t('common.refresh')" :title="t('common.refresh')" @click="load(true)"><RefreshCw :size="18" :class="{ 'animate-spin': loading }" /></button><button class="button-primary" :disabled="pending" @click="edit(null)"><Plus :size="17" />{{ t("accts.addBtn") }}</button></template></PageHeader>
    <p v-if="error" class="error-banner" role="alert">{{ error }}</p>
    <DataTable :rows="rows" :label="t('accts.title')" :row-key="row => row.id" :row-tone="rowTone" :loading="loading" :error="loadError" :empty="t('accts.emptyTitle')" selectable :expanded="expanded" @select="toggleDetails" @retry="load(true)" :columns="[
      { key: 'account', label: t('accts.account'), width: '15%', mobileWide: true },
      { key: 'funds', label: t('accts.funds'), numeric: true, width: '13%' },
      { key: 'today', label: t('board.todayPnl'), numeric: true, width: '12%' },
      { key: 'unrealized', label: t('board.unrealized'), numeric: true, width: '10%' },
      { key: 'drawdown', label: t('accts.drawdown'), numeric: true, width: '7%' },
      { key: 'sync', label: t('accts.lastSync'), width: '9%' },
      { key: 'status', label: t('common.status'), mobileWide: true, width: '14%' },
      { key: 'actions', label: t('common.actions'), width: '20%' },
    ]">
      <!-- 无空格长名称也可收缩换行，不挤出固定尺寸的展开按钮。 -->
      <template #cell-account="{ row }"><div :id="`account-row-${row.id}`"><p class="flex items-start justify-between gap-2 font-semibold"><span class="min-w-0 flex-1 [overflow-wrap:anywhere]">{{ row.name ?? t("common.unknown") }}</span><ChevronDown :size="16" class="mt-0.5 shrink-0 text-muted" :class="{ 'rotate-180': expanded === row.id }" aria-hidden="true" /></p><div class="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted"><span>{{ row.exchange?.toUpperCase() ?? t("common.unknown") }}</span><EnvBadge :environment="row.environment" /></div></div></template>
      <template #cell-funds="{ row }"><strong class="text-base">{{ formatUsd(equity(row)) }}</strong><p class="text-xs text-muted">{{ t("accts.available") }} {{ formatUsd(metrics(row).available) }}</p></template>
      <template #cell-today="{ row }"><strong :class="pnlClass(accountBoard(row)?.todayPnl)">{{ signedUsd(accountBoard(row)?.todayPnl) }}</strong><p class="text-xs" :class="pnlClass(accountBoard(row)?.todayPnlPct)">{{ formatPct(accountBoard(row)?.todayPnlPct) }}</p></template>
      <template #cell-unrealized="{ row }"><span :class="pnlClass(metrics(row).unrealized)">{{ signedUsd(metrics(row).unrealized) }}</span></template>
      <template #cell-drawdown="{ row }"><span :class="(drawdown(row) ?? 0) >= 20 ? 'text-danger' : 'text-muted'">{{ formatPct(drawdown(row), 2, false) }}</span></template>
      <template #cell-sync="{ row }"><FreshnessStamp :timestamp="metrics(row).syncedAt" :warn-after="45" :critical-after="90" /><p class="mt-1 text-xs text-muted">{{ formatDateTime(metrics(row).syncedAt, { short: true, withSeconds: false }) }}</p></template>
      <template #cell-status="{ row }"><div class="flex flex-wrap items-center gap-2"><StatusBadge :state="row.isEnabled == null ? 'unknown' : row.isEnabled === false ? 'disabled' : metrics(row).status" /><label class="inline-flex min-h-11 items-center gap-2 text-xs"><input type="checkbox" role="switch" :checked="row.isEnabled === true" :aria-label="t('accts.enabledFor', { name: row.name ?? '—' })" :disabled="pending || row.isEnabled == null" @change="change(row, 'toggle', $event)" /><span>{{ t(row.isEnabled == null ? "state.UNKNOWN" : row.isEnabled ? "common.enabled" : "common.disabled") }}</span></label></div><p v-if="issue(row)" class="mt-1 break-words text-xs" :class="severity(row) === 2 ? 'text-danger' : 'text-warning'">{{ issue(row) }}</p></template>
      <template #cell-actions="{ row }"><div class="flex flex-wrap items-center justify-end gap-1" @click.stop><button type="button" class="button-secondary min-h-8 px-2 py-1 text-xs" :aria-label="t('accts.detail')" @click="openDetail(row.id)"><ArrowUpRight :size="14" />{{ t("accts.detail") }}</button><button type="button" class="button-secondary min-h-8 px-2 py-1 text-xs" :disabled="pending" :aria-label="t('common.edit')" @click="edit(row)"><Pencil :size="14" />{{ t("common.edit") }}</button><button type="button" class="button-secondary min-h-8 px-2 py-1 text-xs" :disabled="pending" :aria-label="t('accts.test')" @click="test(row)"><Wifi :size="14" />{{ t("accts.testShort") }}</button><button type="button" class="button-secondary min-h-8 px-2 py-1 text-xs" :disabled="pending" :aria-label="t('accts.sync')" @click="sync(row)"><RefreshCw :size="14" />{{ t("accts.syncShort") }}</button><button type="button" class="button-danger min-h-8 px-2 py-1 text-xs" :disabled="pending" :aria-label="t('common.delete')" @click="change(row, 'delete')"><Trash2 :size="14" />{{ t("common.delete") }}</button></div></template>
      <template #mobile-card="{ row }">
        <div class="space-y-3">
          <div class="flex items-start justify-between gap-2">
            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-2">
                <span class="min-w-0 flex-1 font-semibold [overflow-wrap:anywhere]">{{ row.name ?? t("common.unknown") }}</span>
                <EnvBadge :environment="row.environment" />
              </div>
              <div class="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted">
                <span>{{ row.exchange?.toUpperCase() ?? t("common.unknown") }}</span>
                <StatusBadge :state="row.isEnabled == null ? 'unknown' : row.isEnabled === false ? 'disabled' : metrics(row).status" />
              </div>
            </div>
            <div class="flex shrink-0 items-center gap-1" @click.stop>
              <label class="inline-flex min-h-9 items-center">
                <input type="checkbox" role="switch" :checked="row.isEnabled === true" :aria-label="t('accts.enabledFor', { name: row.name ?? '—' })" :disabled="pending || row.isEnabled == null" @change="change(row, 'toggle', $event)" />
              </label>
            </div>
          </div>
          <div class="grid grid-cols-2 gap-3 rounded-lg bg-background/40 p-2.5 text-xs">
            <dl class="number text-left">
              <dt class="text-muted">{{ t("accts.funds") }}</dt>
              <dd class="mt-0.5 text-sm font-bold">{{ formatUsd(equity(row)) }}</dd>
              <dd class="text-muted">{{ t("accts.available") }} {{ formatUsd(metrics(row).available) }}</dd>
            </dl>
            <dl class="number">
              <dt class="text-muted">{{ t("board.todayPnl") }}</dt>
              <dd class="mt-0.5 text-sm font-bold" :class="pnlClass(accountBoard(row)?.todayPnl)">{{ signedUsd(accountBoard(row)?.todayPnl) }}</dd>
              <dd :class="pnlClass(accountBoard(row)?.todayPnlPct)">{{ formatPct(accountBoard(row)?.todayPnlPct) }}</dd>
            </dl>
            <dl class="number text-left">
              <dt class="text-muted">{{ t("board.unrealized") }}</dt>
              <dd class="mt-0.5 font-semibold" :class="pnlClass(metrics(row).unrealized)">{{ signedUsd(metrics(row).unrealized) }}</dd>
            </dl>
            <dl class="number">
              <dt class="text-muted">{{ t("accts.drawdown") }}</dt>
              <dd class="mt-0.5 font-semibold" :class="(drawdown(row) ?? 0) >= 20 ? 'text-danger' : 'text-muted'">{{ formatPct(drawdown(row), 2, false) }}</dd>
            </dl>
          </div>
          <p v-if="issue(row)" class="text-xs" :class="severity(row) === 2 ? 'text-danger' : 'text-warning'">{{ issue(row) }}</p>
          <div class="flex items-center justify-between gap-2 text-xs text-muted">
            <FreshnessStamp :timestamp="metrics(row).syncedAt" :warn-after="45" :critical-after="90" />
            <span>{{ t("accts.positions") }} · {{ row.positions?.length ?? 0 }}</span>
          </div>
          <div class="flex flex-wrap items-center justify-end gap-1.5 border-t border-border/40 pt-2" @click.stop>
            <button type="button" class="button-secondary min-h-8 px-2.5 py-1 text-xs" :aria-label="t('accts.detail')" @click="openDetail(row.id)"><ArrowUpRight :size="14" />{{ t("accts.detail") }}</button>
            <button type="button" class="button-secondary min-h-8 px-2.5 py-1 text-xs" :disabled="pending" :aria-label="t('common.edit')" @click="edit(row)"><Pencil :size="14" />{{ t("common.edit") }}</button>
            <button type="button" class="button-secondary min-h-8 px-2.5 py-1 text-xs" :disabled="pending" :aria-label="t('accts.test')" @click="test(row)"><Wifi :size="14" />{{ t("accts.testShort") }}</button>
            <button type="button" class="button-secondary min-h-8 px-2.5 py-1 text-xs" :disabled="pending" :aria-label="t('accts.sync')" @click="sync(row)"><RefreshCw :size="14" />{{ t("accts.syncShort") }}</button>
            <button type="button" class="button-danger min-h-8 px-2.5 py-1 text-xs" :disabled="pending" :aria-label="t('common.delete')" @click="change(row, 'delete')"><Trash2 :size="14" />{{ t("common.delete") }}</button>
          </div>
        </div>
      </template>
      <template #select-label><ChevronDown :size="16" />{{ t("accts.positions") }}</template>
      <template #details="{ row }">
        <div class="space-y-4">
          <p v-if="row.riskEquityLegacyUnverified || accountBoard(row)?.riskLegacyUnverified" class="border-l-2 border-warning bg-warning/5 p-3 text-sm text-warning" role="alert">{{ t("accts.legacy") }}</p>
          <div class="flex flex-wrap items-center justify-between gap-3"><h2 class="section-title">{{ t("accts.positions") }}</h2><p v-if="showQuantity" class="text-xs text-muted">{{ row.exchange === "okx" ? t("accts.contractQty") : t("accts.baseQty") }}</p></div>
          <EmptyState v-if="!row.positions?.length" :title="t(row.positions == null ? 'accts.positionsUnknown' : 'accts.positionsEmpty')" />
          <div v-else class="space-y-2.5 md:space-y-0 divide-y md:divide-border/50">
            <article v-for="position in positionRows(row)" :key="position.key"
              class="rounded-xl border border-border/40 bg-background/40 p-2.5 md:grid md:items-center md:gap-3 md:rounded-none md:border-0 md:bg-transparent md:px-0 md:py-3 md:grid-cols-[minmax(0,1.2fr)_repeat(4,minmax(0,1fr))]">
              <div class="flex min-w-0 items-center justify-between gap-2 md:block">
                <div class="min-w-0">
                  <h3 class="break-words text-sm font-bold">{{ position.symbol ?? t("common.unknown") }}</h3>
                  <p class="text-xs text-muted">{{ position.side }} · {{ t("unit.leverage", { n: formatQty(position.leverage) }) }}</p>
                </div>
                <div class="shrink-0 text-right font-mono tabular-nums md:hidden">
                  <p class="text-sm font-bold" :class="pnlClass(position.pnl)">{{ signedUsd(position.pnl) }}</p>
                  <p class="text-xs font-semibold" :class="pnlClass(position.pnlPct)">{{ formatPct(position.pnlPct) }}</p>
                </div>
              </div>
              <div class="mt-2 grid grid-cols-3 gap-2 border-t border-border/40 pt-2 text-xs md:contents">
                <dl class="min-w-0 font-mono tabular-nums md:number"><dt class="font-sans text-[11px] text-muted">{{ sizeLabel }}</dt><dd class="mt-0.5 truncate font-semibold">{{ sizeText(position.qty, null, position.price) }}</dd></dl>
                <dl class="min-w-0 font-mono tabular-nums md:number"><dt class="font-sans text-[11px] text-muted">{{ t("board.entry") }}</dt><dd class="mt-0.5 font-semibold">{{ formatPrice(position.entry) }}</dd></dl>
                <dl class="min-w-0 text-right font-mono tabular-nums md:number"><dt class="font-sans text-[11px] text-muted">{{ t("board.mark") }}</dt><dd class="mt-0.5 font-semibold">{{ formatPrice(position.price) }}</dd><dd v-if="position.priceSource === 'estimated' || position.priceSource === 'entry'" class="text-[11px] text-warning">{{ t(`accts.price_${position.priceSource}`) }}</dd></dl>
              </div>
              <dl class="number hidden md:block"><dt class="text-xs text-muted">{{ t("common.pnl") }}</dt><dd :class="pnlClass(position.pnl)">{{ signedUsd(position.pnl) }} <span class="text-xs">{{ formatPct(position.pnlPct) }}</span></dd></dl>
            </article>
          </div>
        </div>
      </template>
    </DataTable>
    <AccountFormModal :open="showModal" :account="editing" :proxies="proxies" @close="showModal = false" @saved="load(true)" />
  </div>
</template>
