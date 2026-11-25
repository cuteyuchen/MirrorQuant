<script setup lang="ts">
import { computed, ref } from "vue";
import { Archive, History, Repeat, ScrollText, Zap } from "lucide-vue-next";
import type { ClosedPosition, LeadSnapshot, TelemetryOrder } from "@/types/api";
import type { TableColumn } from "@/types/ui";
import { useI18n } from "@/i18n";
import { useDisplayUnit } from "@/composables/useDisplayUnit";
import { finiteNumber, formatDateTime, formatPrice, formatQty, formatUsd, signedUsd } from "@/utils/format";
import { normalizeSide, pnlClass, sideLabel, statusLabel } from "@/utils/status";
import { telemetryDirectionLabel } from "@/constants/telemetry";
import DataTable from "@/components/DataTable.vue";

const props = defineProps<{ snapshot: LeadSnapshot; ordersOnly?: boolean }>();
const { t } = useI18n();
const { unit, showQuantity, showNotional, sizeText } = useDisplayUnit();
type Tab = "batches" | "cycles" | "closed" | "orders";
// 默认先看操作批次：它最接近“带单员刚刚做了什么”。
const tab = ref<Tab>("batches");
const PAGE = 15;
const limit = ref(PAGE);
const expanded = ref<string | null>(null);
const view = computed<Tab>(() => props.ordersOnly ? "orders" : tab.value);
function pick(value: Tab) { tab.value = value; limit.value = PAGE; expanded.value = null; }
const tabs = computed(() => [
  { value: "batches" as const, icon: Zap, label: t("feed.tabBatches"), count: props.snapshot.orderBatches?.length ?? null },
  { value: "cycles" as const, icon: Repeat, label: t("feed.tabCycles"), count: props.snapshot.tradeCycles?.length ?? null },
  { value: "closed" as const, icon: Archive, label: t("feed.tabClosed"), count: props.snapshot.closed?.length ?? null },
  { value: "orders" as const, icon: ScrollText, label: t("feed.tabOrders"), count: props.snapshot.orders?.length ?? null },
]);

type Row = {
  key: string; time: number | null | undefined; symbol: string | null | undefined;
  badge: string; badgeClass: string; status: string | null;
  entry: number | null; exit: number | null; qty: number | null; notional: number | null;
  pnl: number | null | undefined; duration: number | null; counts: string | null;
  orderCount: number | null; leverage: string | null; children: TelemetryOrder[];
};
function orderKey(order: TelemetryOrder) {
  return String(order.id ?? order.orderId ?? `${order.symbol}:${order.dir}:${order.time ?? order.t}:${order.qty ?? order.q}:${order.price ?? order.p ?? order.avgPrice}`);
}
// 复合方向里“买入”类用盈利色、“卖出”类用亏损色，与交易所的买卖配色一致；认不出的方向保持中性。
const BUY = ["OPEN_LONG", "ADD_LONG", "CLOSE_SHORT", "REDUCE_SHORT"];
const SELL = ["OPEN_SHORT", "ADD_SHORT", "CLOSE_LONG", "REDUCE_LONG"];
function directionClass(direction?: string | null) {
  const value = direction?.trim().toUpperCase() ?? "";
  return BUY.includes(value) ? "pill tone-success" : SELL.includes(value) ? "pill tone-danger" : "pill";
}
function sideClass(side?: string | null) {
  const value = normalizeSide(side);
  return value === "LONG" ? "side-long" : value === "SHORT" ? "side-short" : "side-unknown";
}
function orderRow(order: TelemetryOrder): Row {
  const qty = order.qty ?? order.q ?? null;
  const price = order.avgPrice ?? order.price ?? order.p ?? null;
  return { key: orderKey(order), time: order.time ?? order.t, symbol: order.symbol,
    badge: telemetryDirectionLabel(order.dir), badgeClass: directionClass(order.dir), status: order.type ?? order.ty ?? null,
    entry: price, exit: null, qty, notional: qty != null && price != null ? qty * price : null, pnl: order.pnl,
    duration: null, counts: null, orderCount: null, leverage: null, children: [] };
}
// 杠杆与保证金模式都可能缺失：缺哪个就不写哪个，不把未知模式当成全仓。
function leverageText(position: ClosedPosition) {
  const leverage = position.leverage == null ? "" : t("unit.leverage", { n: formatQty(position.leverage) });
  const mode = position.isolated == null ? "" : t(position.isolated === true || String(position.isolated).toLowerCase() === "isolated" ? "feed.isolated" : "feed.cross");
  return [leverage, mode].filter(Boolean).join(" · ") || null;
}
// 稳定业务键用于展开状态，翻页或排序不会把另一笔订单展开。
const unsortedRows = computed<Row[]>(() => {
  if (view.value === "orders") return (props.snapshot.orders ?? []).map(orderRow);
  if (view.value === "batches") return (props.snapshot.orderBatches ?? []).map(batch => ({
    ...orderRow(batch), key: `batch:${orderKey(batch)}:${batch.startTime}:${batch.endTime}`,
    time: batch.endTime ?? batch.startTime ?? batch.time, entry: batch.avgPrice ?? null, qty: batch.qty ?? batch.totalQty ?? null,
    notional: batch.notional ?? batch.totalValue ?? null, pnl: batch.pnl ?? batch.totalPnl,
    orderCount: batch.orderCount ?? batch.count ?? (Array.isArray(batch.orders) ? batch.orders.length : batch.orders) ?? null,
    children: batch.items ?? (Array.isArray(batch.orders) ? batch.orders : []),
  }));
  if (view.value === "cycles") return (props.snapshot.tradeCycles ?? []).map(cycle => ({
    key: `cycle:${cycle.cycleId ?? `${cycle.symbol}:${cycle.positionSide}:${cycle.avgEntryPrice}`}`, time: cycle.openedAt,
    symbol: cycle.symbol, badge: sideLabel(cycle.positionSide), badgeClass: sideClass(cycle.positionSide), status: cycle.status ?? null,
    entry: cycle.avgEntryPrice ?? cycle.avgEntry ?? null, exit: cycle.avgExitPrice ?? cycle.avgClose ?? null,
    qty: null, notional: null, pnl: cycle.realizedPnl,
    duration: cycle.durationMs ?? (cycle.durationSec == null ? null : cycle.durationSec * 1000),
    counts: `${formatQty(cycle.entryCount ?? cycle.entryOrdersCount)} / ${formatQty(cycle.scaleInCount)} / ${formatQty(cycle.exitCount ?? cycle.exitOrdersCount)}`,
    orderCount: null, leverage: null, children: [],
  }));
  return (props.snapshot.closed ?? []).map(position => ({
    key: `closed:${position.id ?? `${position.symbol}:${position.side}:${position.opened}:${position.closed}`}`, time: position.closed,
    symbol: position.symbol, badge: sideLabel(position.side), badgeClass: sideClass(position.side), status: null,
    entry: position.avgCost ?? null, exit: position.avgClosePrice ?? position.avgClose ?? null,
    qty: position.maxQty ?? position.closedVol ?? position.maxOpen ?? null, notional: null, pnl: position.pnl,
    duration: position.heldMs ?? (position.closed != null && position.opened != null ? position.closed - position.opened : null),
    counts: null, orderCount: null, leverage: leverageText(position), children: [],
  }));
});
// 操作流倒序展示：最新的在最上面；时间缺失的沉底，原顺序保持稳定。
const rows = computed<Row[]>(() => unsortedRows.value
  .map((row, index) => ({ row, index }))
  .sort((a, b) => {
    const ta = a.row.time, tb = b.row.time;
    if (ta == null && tb == null) return a.index - b.index;
    if (ta == null) return 1;
    if (tb == null) return -1;
    if (tb !== ta) return tb - ta;
    return a.index - b.index;
  })
  .map(({ row }) => row));
// 每个页签只列它真正有的字段，不再用一套列硬套四种数据。
const columns = computed<TableColumn[]>(() => {
  const symbol = { key: "symbol", label: t("feed.colDirSym"), width: "23%", mobileWide: true };
  const pnl = { key: "pnl", label: t("feed.colRpnl"), numeric: true };
  if (view.value === "cycles") return [symbol, { key: "time", label: t("board.openedAt"), width: "20%" },
    { key: "prices", label: t("feed.cycleEntryExit"), numeric: true }, { key: "counts", label: t("feed.cycleCounts"), numeric: true }, pnl];
  if (view.value === "closed") return [symbol, { key: "time", label: t("feed.closedTime"), width: "19%" },
    { key: "prices", label: t("feed.cycleEntryExit"), numeric: true }, { key: "qty", label: unit.value === "notional" ? t("feed.colNotional") : t("feed.closedMaxQty"), numeric: true },
    { key: "leverage", label: t("feed.closedLev"), numeric: true }, { key: "pnl", label: t("feed.closedPnl"), numeric: true }];
  const base: TableColumn[] = [symbol, { key: "time", label: t("feed.colTime"), width: "19%" }, { key: "price", label: t("feed.colAvgPx"), numeric: true }];
  if (showQuantity.value) base.push({ key: "qty", label: view.value === "batches" ? t("feed.colQty") : t("common.quantity"), numeric: true });
  if (showNotional.value) base.push({ key: "notional", label: t("feed.colNotional"), numeric: true });
  return view.value === "batches" ? [...base, { key: "orders", label: t("feed.colOrders"), numeric: true }, pnl] : [...base, pnl];
});
const label = computed(() => tabs.value.find(item => item.value === view.value)?.label ?? t("telemetry.history"));
const empty = computed(() => t(view.value === "batches" ? "feed.noBatches" : view.value === "cycles" ? "feed.noCycles" : view.value === "closed" ? "feed.noClosed" : "feed.noOrders"));
function duration(ms: number | null | undefined) {
  if (ms == null) return t("common.unknown");
  const hours = Math.floor(ms / 3600000);
  return hours >= 24 ? t("dur.dayHour", { d: Math.floor(hours / 24), h: hours % 24 }) : t("dur.hourMin", { h: hours, m: Math.floor(ms % 3600000 / 60000) });
}
function childNotional(order: TelemetryOrder) {
  const qty = finiteNumber(order.qty ?? order.q);
  const price = finiteNumber(order.avgPrice ?? order.price ?? order.p);
  return qty == null || price == null ? null : Math.abs(qty * price);
}
</script>
<template>
  <section class="space-y-3">
    <div v-if="!ordersOnly" class="flex flex-wrap items-center justify-between gap-3">
      <h3 class="section-title flex items-center gap-2.5"><span class="section-icon" aria-hidden="true"><History :size="18" /></span>{{ t("telemetry.history") }}</h3>
      <div class="tab-list" role="tablist" :aria-label="t('telemetry.history')">
        <button v-for="item in tabs" :key="item.value" type="button" class="tab-button" role="tab" :aria-selected="view === item.value" @click="pick(item.value)">
          <component :is="item.icon" :size="15" aria-hidden="true" />{{ item.label }}
          <span v-if="item.count != null" class="rounded-full bg-surface3 px-1.5 text-xs font-semibold tabular-nums text-muted">{{ item.count }}</span>
        </button>
      </div>
    </div>
    <p v-if="view === 'batches'" class="text-xs text-muted">{{ t("feed.batchNote") }}</p>
    <DataTable :rows="rows.slice(0, limit)" :columns="columns" :label="label" :empty="empty" :row-key="row => row.key"
      :selectable="view === 'batches'" :expanded="expanded" @select="expanded = expanded === $event.key ? null : $event.key">
      <template #cell-symbol="{ row }">
        <div class="flex flex-wrap items-center gap-1.5">
          <strong>{{ row.symbol ?? t("common.unknown") }}</strong>
          <span :class="row.badgeClass">{{ row.badge }}</span>
          <span v-if="view === 'cycles' && row.status" class="pill" :class="row.status === 'OPEN' ? 'tone-accent' : ''">{{ statusLabel(row.status) }}</span>
        </div>
        <p v-if="view !== 'cycles' && row.status" class="mt-1 font-mono text-xs uppercase text-faint">{{ row.status }}</p>
      </template>
      <template #cell-time="{ row }">
        <time class="font-mono text-xs tabular-nums">{{ formatDateTime(row.time) }}</time>
        <p v-if="row.duration != null" class="mt-1 font-mono text-xs tabular-nums text-muted">{{ duration(row.duration) }}</p>
      </template>
      <template #cell-price="{ row }">{{ formatPrice(row.entry) }}</template>
      <template #cell-prices="{ row }">
        {{ formatPrice(row.entry) }}
        <!-- 还没平仓的轮次没有平仓均价，写“持仓中”而不是破折号。 -->
        <p class="mt-1 text-muted">→ {{ row.exit == null && row.status === "OPEN" ? t("feed.holding") : formatPrice(row.exit) }}</p>
      </template>
      <template #cell-qty="{ row }">{{ view === "closed" ? sizeText(row.qty, row.notional, row.entry) : formatQty(row.qty) }}</template>
      <template #cell-notional="{ row }">{{ formatUsd(row.notional) }}</template>
      <template #cell-orders="{ row }">{{ formatQty(row.orderCount) }}</template>
      <template #cell-counts="{ row }">{{ row.counts ?? t("common.unknown") }}</template>
      <template #cell-leverage="{ row }">{{ row.leverage ?? t("common.unknown") }}</template>
      <template #cell-pnl="{ row }"><strong :class="pnlClass(row.pnl)">{{ signedUsd(row.pnl) }}</strong></template>
      <template #mobile-card="{ row }">
        <div class="space-y-1.5">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <div class="flex min-w-0 flex-wrap items-center gap-1.5">
              <strong class="text-sm">{{ row.symbol ?? t("common.unknown") }}</strong>
              <span :class="row.badgeClass">{{ row.badge }}</span>
              <span v-if="view === 'cycles' && row.status" class="pill" :class="row.status === 'OPEN' ? 'tone-accent' : ''">{{ statusLabel(row.status) }}</span>
            </div>
            <strong class="font-mono text-sm tabular-nums" :class="pnlClass(row.pnl)">{{ signedUsd(row.pnl) }}</strong>
          </div>
          <div class="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 font-mono text-xs tabular-nums text-muted">
            <time>{{ formatDateTime(row.time, { short: true }) }}</time>
            <span>
              {{ formatPrice(row.entry) }}<template v-if="row.exit != null || row.status === 'OPEN'"> → {{ row.exit == null ? t("feed.holding") : formatPrice(row.exit) }}</template>
            </span>
            <span v-if="row.qty != null">{{ view === "closed" ? sizeText(row.qty, row.notional, row.entry) : formatQty(row.qty) }}</span>
            <span v-if="row.duration != null">{{ duration(row.duration) }}</span>
          </div>
        </div>
      </template>
      <template #select-label>{{ t("feed.toggleChildren") }}</template>
      <template #details="{ row }">
        <p class="mb-1 font-sans text-xs font-semibold text-muted">{{ t("feed.childOrders", { n: row.children.length }) }}</p>
        <ul class="divide-y divide-border/40 font-mono text-xs tabular-nums">
          <li v-for="(order, index) in row.children" :key="`${orderKey(order)}:${index}`" class="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 py-1.5">
            <time class="text-muted">{{ formatDateTime(order.time ?? order.t) }}</time>
            <span><strong class="font-semibold">{{ unit === "both" ? formatQty(order.qty ?? order.q) : sizeText(order.qty ?? order.q, childNotional(order), order.avgPrice ?? order.price ?? order.p) }}</strong> <span class="text-accent">@ {{ formatPrice(order.avgPrice ?? order.price ?? order.p) }}</span></span>
            <span v-if="unit === 'both'" class="text-muted">{{ formatUsd(childNotional(order)) }}</span>
            <span :class="pnlClass(order.pnl)">{{ signedUsd(order.pnl) }}</span>
          </li>
        </ul>
      </template>
    </DataTable>
    <div v-if="rows.length > PAGE" class="flex flex-wrap items-center gap-2">
      <button v-if="rows.length > limit" type="button" class="button-secondary" @click="limit += 20">{{ t("feed.showMore", { shown: limit, total: rows.length }) }}</button>
      <button v-if="limit > PAGE" type="button" class="button-ghost" @click="limit = PAGE">{{ t("feed.showLess") }}</button>
    </div>
  </section>
</template>
