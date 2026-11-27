<script setup lang="ts">
import { computed, ref, useId } from "vue";
import { Calculator, Copy, Layers } from "lucide-vue-next";
import type { LeadSnapshot, LegacyPosition, TelemetryOrder } from "@/types/api";
import { useNow } from "@/composables/useNow";
import { useAgo } from "@/composables/useAgo";
import { useI18n } from "@/i18n";
import { useDisplayUnit } from "@/composables/useDisplayUnit";
import { useCopyCapital } from "@/composables/useCopyCapital";
import { useUiStore } from "@/stores/ui";
import { calculateSizing } from "@/utils/sizing";
import { baseAsset, finiteNumber, formatDateTime, formatLeverage, formatPct, formatPrice, formatQty, formatUsd, signedUsd } from "@/utils/format";
import { normalizeSide, pnlClass, sideLabel } from "@/utils/status";
import { beijingDayStart, positionsConfirmed } from "@/utils/positions";
import { telemetryDirectionLabel } from "@/constants/telemetry";
import FreshnessStamp from "@/components/FreshnessStamp.vue";
import EmptyState from "@/components/EmptyState.vue";
import StatTile from "@/components/StatTile.vue";
import PriceActionPanel from "./PriceActionPanel.vue";

const props = withDefaults(defineProps<{
  snapshot: LeadSnapshot;
  embedded?: boolean;
  withCalculator?: boolean;
  displayName?: string | null;
}>(), { embedded: false, withCalculator: false });
const { t } = useI18n();
const { unit, showQuantity, showNotional, sizeText } = useDisplayUnit();
const now = useNow();
const ago = useAgo();
const summary = computed(() => props.snapshot.openPositionsSummary ?? null);
const positions = computed(() => props.snapshot.openPositions ?? []);
// 隐藏或未知仓位不等于空仓：只有确知的持仓列表才参与“空仓 / 持仓中”的判断。
const confirmed = computed(() => positionsConfirmed(props.snapshot));
const flat = computed(() => confirmed.value && positions.value.length === 0);
const marginBalance = computed(() => summary.value?.marginBalance ?? props.snapshot.profile?.marginBalance ?? null);
const dayStart = computed(() => beijingDayStart(now.value));

// 合并后的动态最优杠杆与跟单开仓测算（不在顶层调用 useUiStore，保持无 Pinia 独立测试可用）。
const PRESETS = [500, 1000, 2000, 5000, 10000];
const MODES = [
  { value: 0.5, label: "calc.multDefensive" },
  { value: 1, label: "calc.multProportional" },
  { value: 1.2, label: "calc.multAggressive" },
];
const capitalId = useId();
const capitalInput = useCopyCapital();
const multiplier = ref(1);
const copying = ref(false);
const capital = computed(() => {
  const value = finiteNumber(capitalInput.value);
  return value != null && value > 0 && value <= 1e12 ? value : null;
});
const sizing = computed(() => props.withCalculator
  ? calculateSizing(positions.value, finiteNumber(marginBalance.value), capital.value, multiplier.value, summary.value)
  : null);
type SizingRow = NonNullable<typeof sizing.value>["rows"][number];
const marginPct = computed(() => share(sizing.value?.totalMargin ?? null));
const overCommitted = computed(() => sizing.value?.freeCash != null && sizing.value.freeCash < 0);

function share(value: number | null) { return value == null || capital.value == null ? null : value / capital.value * 100; }
function shareText(value: number | null) {
  const pct = share(value);
  return pct == null ? "" : t("calc.capitalShare", { pct: formatPct(pct, 1, false) });
}
function ratioText(row: SizingRow) { return row.ratio == null ? "" : `r=${row.ratio.toFixed(4)}`; }
function liqText(row: SizingRow) {
  if (row.liquidation == null || row.mark == null) return "";
  return `${t("board.fromMark")} ${formatPct((row.liquidation - row.mark) / row.mark * 100)}`;
}
function rowIssue(row: SizingRow) {
  return row.targetQty != null ? "" : t(row.entry == null || row.mark == null ? "calc.missingPrice" : "calc.unverifiedSizing");
}
function sizingRow(index: number): SizingRow | null {
  return sizing.value?.rows[index] ?? null;
}
function toastMessage(msg: string, tone?: "success" | "danger") {
  try { useUiStore().toast(msg, tone); } catch { /* 独立测试挂载未初始化 Pinia 时忽略提示条。 */ }
}
async function copyPlan() {
  const result = sizing.value;
  if (!result?.complete || copying.value) return;
  copying.value = true;
  try {
    const lines = [
      t("calc.planTitle", { name: props.displayName ?? props.snapshot?.profile?.nickname ?? t("board.lead") }),
      t("calc.estimated"),
      `${t("calc.capitalLabel")}: ${formatUsd(capital.value)} · ${t("calc.multiplierLabel")}: ${multiplier.value}x`,
      `${t("calc.totalNotional")}: ${formatUsd(result.totalNotional)} · ${t("calc.portfolioLev")}: ${formatLeverage(result.portfolioLeverage)}`,
      `${t("calc.marginOccupied")}: ${formatUsd(result.totalMargin)} (${formatPct(marginPct.value, 2, false)}) · ${t("calc.freeCashCushion")}: ${formatUsd(result.freeCash)}`,
      `${t("calc.buffer")}: ±${formatPct(result.buffer, 2, false)}`,
      "",
      ...result.rows.flatMap(row => [
        t("calc.planLine", {
          symbol: row.symbol ?? t("common.unknown"), side: sideLabel(row.side),
          qty: formatQty(row.targetQty), price: formatPrice(row.entry), leverage: formatQty(row.leverage),
        }),
        t("calc.planDetail", {
          notional: formatUsd(row.targetNotional), ratio: row.ratio == null ? t("common.unknown") : row.ratio.toFixed(4),
          margin: formatUsd(row.initialMargin), liq: formatPrice(row.liquidation),
        }),
      ]),
      "",
      t("calc.estimateNotice"),
    ];
    await navigator.clipboard.writeText(lines.join("\n"));
    toastMessage(t("calc.copied"));
  } catch { toastMessage(t("calc.copyError"), "danger"); }
  finally { copying.value = false; }
}

function sideOf(position: LegacyPosition) { return normalizeSide(position.positionSide ?? position.posSide, position.qty ?? position.pos); }
function sideClass(position: LegacyPosition) {
  const side = sideOf(position);
  return side === "LONG" ? "side-long" : side === "SHORT" ? "side-short" : "side-unknown";
}
function positionKey(position: LegacyPosition) { return `${position.symbol ?? position.instId}:${position.positionSide ?? position.posSide}:${position.openTime}`; }
function orderKey(order: TelemetryOrder) { return order.id ?? order.orderId ?? `${order.t ?? order.time}:${order.p ?? order.price}:${order.q ?? order.qty}`; }
function openedToday(position: LegacyPosition) { return position.openTime != null && position.openTime >= dayStart.value; }
// 副文案只在底层数值已知时出现，不拼出“可用 —”这类半截信息。
function known(value: number | null | undefined, build: (value: number) => string) {
  const number = finiteNumber(value);
  return number == null ? "" : build(number);
}

// 多空笔数优先用服务端汇总，缺失时才从列表推导。
const counts = computed(() => ({
  long: summary.value?.longCount ?? positions.value.filter(position => sideOf(position) === "LONG").length,
  short: summary.value?.shortCount ?? positions.value.filter(position => sideOf(position) === "SHORT").length,
}));
const status = computed(() => {
  if (!confirmed.value) return { text: t("pub.positionsUnknown"), sub: "", tone: "text-warning" };
  if (flat.value) return { text: t("tel.flatStatus"), sub: t("tel.flatDetail"), tone: "text-muted" };
  const { long, short } = counts.value;
  const parts = [short > 0 ? t("tel.shortCount", { n: short }) : "", long > 0 ? t("tel.longCount", { n: long }) : ""].filter(Boolean);
  return {
    text: t("tel.holdingStatus", { parts: parts.length ? parts.join(" + ") : t("pub.positionsCount", { n: positions.value.length }) }),
    sub: long > 0 && short > 0 ? t("tel.hedged") : short > 0 ? t("tel.pureShort") : long > 0 ? t("tel.pureLong") : "",
    tone: "",
  };
});
// 风险等级只看账户名义杠杆；杠杆未知时不贴标签，不把未知当成低风险。
const risk = computed(() => {
  if (!confirmed.value) return null;
  if (flat.value) return { label: t("tel.riskFlat"), tone: "tone-neutral" };
  const leverage = finiteNumber(summary.value?.totalNotionalLeverage);
  if (leverage == null) return null;
  if (leverage >= 12) return { label: t("tel.riskExtreme"), tone: "tone-danger" };
  if (leverage >= 5) return { label: t("tel.riskHigh"), tone: "tone-danger" };
  if (leverage >= 2) return { label: t("tel.riskMid"), tone: "tone-warning" };
  if (leverage >= 0.8) return { label: t("tel.riskLow"), tone: "tone-success" };
  return { label: t("tel.riskUltraLow"), tone: "tone-accent" };
});
// 持仓来源：交易所直出、订单流重建，或接口失败后沿用的旧持仓。
const source = computed(() => {
  const value = props.snapshot.openPositionsMeta?.source;
  if (value === "api" || value === "direct-holdings") return { label: t("tel.srcApi"), tone: "tone-success" };
  if (value === "derived-from-order-history" || value === "derived") return { label: t("tel.srcDerived"), tone: "tone-accent" };
  if (value === "stale-fallback") return { label: t("tel.srcStale"), tone: "tone-warning" };
  return null;
});
const mmrSource = computed(() => {
  const value = summary.value?.mmrSource;
  return value === "binance-official-brackets" ? t("tel.mmrOfficial") : value ? t("tel.mmrFallback") : t("common.unknown");
});
// 账户层面的六个数：持仓状态、本金、总持有、杠杆、保证金安全垫、浮动盈亏。
const tiles = computed(() => {
  const data = summary.value;
  const coverage = finiteNumber(data?.marginCoveragePct);
  const buffer = finiteNumber(data?.adverseMoveBufferPct);
  return [
    { key: "status", label: t("tel.posStatus"), value: status.value.text, sub: status.value.sub, valueClass: `font-sans ${status.value.tone}` },
    { key: "margin", label: t("tel.tileMarginBal"), value: formatUsd(marginBalance.value), sub: known(data?.availableMargin, value => t("tel.tileAvail", { val: formatUsd(value) })), valueClass: "text-base" },
    { key: "mark", label: t("tel.tileTotalMark"), value: formatUsd(data?.totalMarkNotional), sub: known(data?.totalEntryNotional, value => t("tel.tileEntryNotional", { val: formatUsd(value) })), valueClass: "text-base" },
    { key: "leverage", label: t("tel.tileNotionalLev"), value: formatLeverage(data?.totalNotionalLeverage), sub: known(data?.maxPositionLeverage, value => t("tel.tileMaxSingle", { val: formatLeverage(value) })), valueClass: "text-base text-accent" },
    { key: "mmr", label: t("tel.tileMmrBuffer"), value: formatPct(data?.estMaintenanceMarginRatioPct, 2, false),
      sub: coverage == null && buffer == null ? "" : t("tel.tileCovBuf", { cov: formatPct(coverage, 0, false), buf: formatPct(buffer, 2, false) }), valueClass: "text-base" },
    { key: "pnl", label: t("tel.tileLiveUpnl"), value: signedUsd(data?.totalUnrealizedPnl), sub: known(data?.totalMarginRoePct, value => t("tel.tileMarginRoe", { val: formatPct(value) })),
      valueClass: `text-base ${pnlClass(data?.totalUnrealizedPnl)}`, subClass: pnlClass(data?.totalMarginRoePct) },
  ];
});

// 持仓时长用共享时钟推进，只有后端提供的开仓时间才参与计算。
function held(opened?: number | null, duration?: number | null) {
  const ms = opened == null ? duration : Math.max(0, now.value - opened);
  if (ms == null) return t("common.unknown");
  const hours = Math.floor(ms / 3600000);
  return hours >= 24 ? t("dur.dayHour", { d: Math.floor(hours / 24), h: hours % 24 }) : t("dur.hourMin", { h: hours, m: Math.floor(ms % 3600000 / 60000) });
}
// 资金费率是小数，换算成百分数并保留四位。
function fundingText(rate: number) { return `${(rate * 100).toFixed(4)}%`; }
function positionTiles(position: LegacyPosition) {
  const entryCount = position.entryOrdersCount ?? position.entryOrders?.length ?? null;
  const lastAdd = ago(position.lastAddTime);
  const tiles = [
    { key: "entry", label: t("pos.avgEntry"), value: formatPrice(position.avgEntry), sub: known(position.entryValue, value => t("pos.entryVal", { val: formatUsd(value) })) },
    { key: "qty", label: t("common.quantity"), value: formatQty(position.qty ?? position.pos), sub: position.symbol ? baseAsset(position.symbol) : "" },
    { key: "value", label: t("pos.markVal"), value: formatUsd(position.markValue ?? position.notional), sub: known(position.sharePct, value => t("pos.share", { pct: formatPct(value, 1, false) })) },
    { key: "leverage", label: t("pos.notionalLev"), value: formatLeverage(position.notionalLeverage), sub: known(position.entryLeverage, value => t("pos.entryLev", { val: formatLeverage(value) })), valueClass: "text-accent" },
    // 新快照优先使用分档 MMR，旧快照仍兼容平铺字段。
    { key: "mmr", label: t("pos.mmrRate"), value: formatPct(position.mmr?.mmrPct ?? position.maintMarginPct, 2, false),
      sub: position.mmr?.tier != null ? t("pos.tier", { tier: position.mmr.tier }) : position.mmr?.exact === false ? t("tel.mmrFallback") : "" },
    { key: "maint", label: t("pos.maintMargin"), value: formatUsd(position.maintMargin), sub: known(position.maintMarginRoePct, value => t("pos.pctOfMb", { pct: formatPct(value, 2, false) })) },
    { key: "held", label: t("pos.heldDur"), value: held(position.openTime, position.heldMs), sub: lastAdd ? t("pos.lastAdd", { ago: lastAdd }) : "" },
    { key: "opened", label: t("board.openedAt"), value: formatDateTime(position.openTime, { short: true, withSeconds: false }), sub: entryCount == null ? "" : t("pos.entryOrdersCount", { n: entryCount }) },
  ];
  return tiles.filter(tile => (tile.key !== "qty" || showQuantity.value) && (tile.key !== "value" || showNotional.value));
}
// 建仓明细缺少方向时按持仓方向补“开多 / 开空”，方向也未知就保留破折号。
function entryDirection(order: TelemetryOrder, position: LegacyPosition) {
  if (order.dir) return telemetryDirectionLabel(order.dir);
  const side = sideOf(position);
  return side === "SHORT" ? t("dir.OPEN_SHORT") : side === "LONG" ? t("dir.OPEN_LONG") : t("common.unknown");
}
function orderNotional(order: TelemetryOrder) {
  const qty = finiteNumber(order.q ?? order.qty);
  const price = finiteNumber(order.p ?? order.price ?? order.avgPrice);
  return qty == null || price == null ? null : Math.abs(qty * price);
}
</script>
<template>
  <section class="card">
    <header class="card-header">
      <div class="flex min-w-0 flex-1 flex-wrap items-center gap-x-2.5 gap-y-2">
        <span class="section-icon" aria-hidden="true"><component :is="withCalculator ? Calculator : Layers" :size="18" /></span>
        <h3 class="section-title min-w-0">{{ t(withCalculator ? "tel.openAndCalcTitle" : "tel.openSectionTitle") }}</h3>
        <span v-if="source" class="pill" :class="source.tone">{{ source.label }}</span>
        <span v-if="risk" class="pill font-semibold" :class="risk.tone">{{ risk.label }}</span>
      </div>
      <div class="flex shrink-0 items-center gap-2">
        <button v-if="withCalculator && !flat && confirmed" type="button" class="button-secondary min-h-8 px-2.5 py-1 text-xs" :disabled="!sizing?.complete || copying" @click="copyPlan"><Copy :size="14" aria-hidden="true" />{{ t("calc.copyPlan") }}</button>
        <FreshnessStamp :timestamp="snapshot.meta?.priceUpdatedAt ?? summary?.priceAsOf" :stale="snapshot.meta?.stale" />
      </div>
    </header>
    <div class="card-body cq space-y-4">
      <dl class="cq-tiles-6">
        <StatTile v-for="tile in tiles" :key="tile.key" :label="tile.label" :value="tile.value" :sub="tile.sub" :value-class="tile.valueClass" :sub-class="tile.subClass" />
      </dl>
      <!-- 合并测算控制栏：共享带单本金与持仓列表，避免与独立的仓位测算卡重复信息。 -->
      <div v-if="withCalculator && confirmed && !flat" data-testid="merged-calculator-controls" data-section="calculator"
        class="grid min-w-0 gap-4 rounded-xl border border-accent/25 bg-accent/[0.04] p-3 sm:p-4 lg:grid-cols-2">
        <div class="min-w-0">
          <label :for="capitalId" class="field-label mb-1">{{ t("calc.capitalLabel") }}</label>
          <input :id="capitalId" v-model="capitalInput" type="number" inputmode="decimal" min="0" step="100" class="form-input font-mono tabular-nums"
            :placeholder="t('calc.capitalPlaceholder')" :aria-invalid="capital == null" :aria-describedby="capital == null ? `${capitalId}-error` : undefined" />
          <p v-if="capital == null" :id="`${capitalId}-error`" class="field-error" role="alert">{{ t("calc.capitalInvalid") }}</p>
          <div class="mt-2 flex flex-wrap items-center gap-1.5" role="group" :aria-label="t('calc.quickPreset')">
            <span class="text-xs text-muted">{{ t("calc.quickPreset") }}</span>
            <button v-for="preset in PRESETS" :key="preset" type="button" class="chip-button" :aria-pressed="capital === preset" @click="capitalInput = preset">{{ preset.toLocaleString("en-US") }}</button>
          </div>
        </div>
        <fieldset class="min-w-0">
          <legend class="field-label mb-1">{{ t("calc.multiplierLabel") }}</legend>
          <div class="tab-list" role="group">
            <button v-for="mode in MODES" :key="mode.value" type="button" class="tab-button" :aria-pressed="multiplier === mode.value" @click="multiplier = mode.value">{{ t(mode.label) }}</button>
          </div>
          <p class="mt-2 text-xs text-faint">{{ t("calc.ratioFormula") }} · {{ t("calc.dynLevFormula") }}</p>
        </fieldset>
      </div>
      <EmptyState v-if="!confirmed" :title="t('board.unknownPositions')" />
      <EmptyState v-else-if="flat" :title="t('tel.noOpenPositions')" />
      <div v-else class="space-y-3">
        <p v-if="withCalculator && sizing && !sizing.complete" class="warning-banner" role="alert">{{ t("calc.incomplete") }}</p>
        <!-- 嵌入模式（详情页）：持仓已在顶部 LeadComparison 中权威展示，这里仅保留 SMC/PA 结构分析、建仓订单与合并跟单测算 -->
        <template v-if="embedded">
          <article v-for="(position, idx) in positions" :key="positionKey(position)" class="cq min-w-0 rounded-xl border border-border/50 bg-background/40 p-3 sm:p-4" :class="openedToday(position) ? 'today-edge' : ''">
            <div class="flex flex-wrap items-center justify-between gap-2 border-b border-border/40 pb-2.5">
              <div class="flex flex-wrap items-center gap-1.5">
                <h4 class="text-base font-bold tracking-tight">{{ position.symbol ?? position.instId ?? t("common.unknown") }}</h4>
                <span :class="sideClass(position)">{{ sideLabel(position.positionSide ?? position.posSide, position.qty ?? position.pos) }}</span>
                <span v-if="openedToday(position)" class="pill tone-accent font-semibold">{{ t("board.today") }}</span>
                <span v-if="position.fundingRate != null" class="pill font-mono tabular-nums">{{ t("pos.fundingRate", { rate: fundingText(position.fundingRate) }) }}</span>
              </div>
              <div class="flex items-center gap-3">
                <span class="text-right font-mono text-sm font-bold tabular-nums" :class="pnlClass(position.uPnL)">
                  {{ signedUsd(position.uPnL) }}<span class="ml-1.5 text-xs font-semibold" :class="pnlClass(position.uPnLRoePct)">{{ formatPct(position.uPnLRoePct) }}</span>
                </span>
                <a :href="`#pos-${position.symbol ?? position.instId}`" class="inline-flex min-h-8 items-center gap-1 text-xs font-semibold text-accent transition hover:underline">
                  {{ t("common.details") }} ↑
                </a>
              </div>
            </div>
            <div class="mt-3 space-y-3">
              <div v-if="withCalculator && sizingRow(idx)" data-testid="position-sizing-strip" class="rounded-lg border border-accent/25 bg-accent/[0.04] p-2.5">
                <p v-if="rowIssue(sizingRow(idx)!)" class="mb-2 text-xs font-medium text-warning">{{ rowIssue(sizingRow(idx)!) }}</p>
                <dl class="grid grid-cols-2 gap-2.5 sm:grid-cols-5">
                  <StatTile v-if="showQuantity" :boxed="false" :label="t('calc.colTargetQty')" :value="formatQty(sizingRow(idx)!.targetQty)" :sub="baseAsset(position.symbol)" value-class="text-accent" />
                  <StatTile v-if="showNotional" :boxed="false" :label="t('calc.colTargetNotional')" :value="formatUsd(sizingRow(idx)!.targetNotional)" :sub="ratioText(sizingRow(idx)!)" />
                  <StatTile :boxed="false" :label="t('calc.colOptLev')" :value="sizingRow(idx)!.leverage != null ? t('unit.leverage', { n: formatQty(sizingRow(idx)!.leverage) }) : t('common.unknown')" value-class="text-accent" :sub="ratioText(sizingRow(idx)!)" />
                  <StatTile :boxed="false" :label="t('calc.colMarginReq')" :value="formatUsd(sizingRow(idx)!.initialMargin)" :sub="shareText(sizingRow(idx)!.initialMargin)" />
                  <StatTile :boxed="false" :label="t('calc.colEstLiq')" :value="formatPrice(sizingRow(idx)!.liquidation)" :sub="liqText(sizingRow(idx)!)" value-class="text-warning" />
                </dl>
              </div>
              <PriceActionPanel v-if="position.analysis?.ok" :analysis="position.analysis" />
              <p v-else class="tile self-start text-xs text-muted">{{ t("pa.pending") }}</p>
              <details v-if="position.entryOrders?.length" class="rounded-lg border border-border/40 bg-surface/60">
                <summary class="cursor-pointer rounded-lg px-3 py-2 text-xs font-semibold text-accent">{{ t("pos.entryFills") }} · {{ t("pos.entryOrdersCount", { n: position.entryOrders.length }) }}</summary>
                <ul class="divide-y divide-border/40 px-3 pb-1.5 font-mono text-xs tabular-nums">
                  <li v-for="order in position.entryOrders" :key="orderKey(order)" class="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 py-1.5">
                    <time class="text-muted">{{ formatDateTime(order.t ?? order.time, { short: true }) }}</time>
                    <span class="font-sans">{{ entryDirection(order, position) }}</span>
                    <span><strong class="font-semibold">{{ unit === "both" ? formatQty(order.q ?? order.qty) : sizeText(order.q ?? order.qty, orderNotional(order), order.p ?? order.price ?? order.avgPrice) }}</strong> <span class="text-accent">@ {{ formatPrice(order.p ?? order.price ?? order.avgPrice) }}</span></span>
                    <span v-if="unit === 'both'" class="text-muted">{{ formatUsd(orderNotional(order)) }}</span>
                  </li>
                </ul>
              </details>
            </div>
          </article>
        </template>
        <!-- 独立模式（公开看板等）：外层无权威持仓卡，渲染完整的持仓分析大卡 -->
        <template v-else>
          <article v-for="(position, idx) in positions" :key="positionKey(position)" class="cq min-w-0 rounded-xl border border-border/50 bg-background/40 p-3 sm:p-4" :class="openedToday(position) ? 'today-edge' : ''">
            <div class="cq-split">
              <div class="cq min-w-0">
                <div class="flex flex-wrap items-center gap-1.5">
                  <h4 class="text-base font-bold tracking-tight">{{ position.symbol ?? position.instId ?? t("common.unknown") }}</h4>
                  <span :class="sideClass(position)">{{ sideLabel(position.positionSide ?? position.posSide, position.qty ?? position.pos) }}</span>
                  <span v-if="openedToday(position)" class="pill tone-accent font-semibold">{{ t("board.today") }}</span>
                  <span v-if="position.fundingRate != null" class="pill font-mono tabular-nums">{{ t("pos.fundingRate", { rate: fundingText(position.fundingRate) }) }}</span>
                </div>
                <div class="mt-3 flex flex-wrap items-end justify-between gap-x-4 gap-y-2">
                  <div class="min-w-0">
                    <p class="flex items-center gap-1.5 text-xs text-muted"><span v-if="snapshot.meta?.stale === false" class="live-dot text-success" aria-hidden="true" />{{ t("pos.curPrice") }}</p>
                    <p class="mt-1 font-mono text-2xl font-bold leading-none tabular-nums tracking-tight sm:text-3xl [overflow-wrap:anywhere]">{{ formatPrice(position.currentPrice ?? position.markPrice) }}</p>
                    <p class="mt-1.5 font-mono text-xs font-semibold tabular-nums" :class="pnlClass(position.priceEdgePct)">{{ t("pos.vsAvg") }} {{ formatPct(position.priceDiffPct) }}</p>
                  </div>
                  <div class="min-w-0 text-right">
                    <p class="text-xs text-muted">{{ t("pos.floatPnl") }}</p>
                    <p class="mt-1 font-mono text-xl font-bold leading-none tabular-nums sm:text-2xl [overflow-wrap:anywhere]" :class="pnlClass(position.uPnL)">{{ signedUsd(position.uPnL) }}</p>
                    <p class="mt-1.5 font-mono text-xs font-semibold tabular-nums" :class="pnlClass(position.uPnLRoePct)">ROE {{ formatPct(position.uPnLRoePct) }}</p>
                  </div>
                </div>
                <dl class="cq-tiles mt-3.5">
                  <StatTile v-for="tile in positionTiles(position)" :key="tile.key" :label="tile.label" :value="tile.value" :sub="tile.sub" :value-class="tile.valueClass" />
                </dl>
                <div v-if="withCalculator && sizingRow(idx)" data-testid="position-sizing-strip" class="mt-3 rounded-lg border border-accent/25 bg-accent/[0.04] p-2.5">
                  <p v-if="rowIssue(sizingRow(idx)!)" class="mb-2 text-xs font-medium text-warning">{{ rowIssue(sizingRow(idx)!) }}</p>
                  <dl class="grid grid-cols-2 gap-2.5 sm:grid-cols-5">
                    <StatTile v-if="showQuantity" :boxed="false" :label="t('calc.colTargetQty')" :value="formatQty(sizingRow(idx)!.targetQty)" :sub="baseAsset(position.symbol)" value-class="text-accent" />
                    <StatTile v-if="showNotional" :boxed="false" :label="t('calc.colTargetNotional')" :value="formatUsd(sizingRow(idx)!.targetNotional)" :sub="ratioText(sizingRow(idx)!)" />
                    <StatTile :boxed="false" :label="t('calc.colOptLev')" :value="sizingRow(idx)!.leverage != null ? t('unit.leverage', { n: formatQty(sizingRow(idx)!.leverage) }) : t('common.unknown')" value-class="text-accent" :sub="ratioText(sizingRow(idx)!)" />
                    <StatTile :boxed="false" :label="t('calc.colMarginReq')" :value="formatUsd(sizingRow(idx)!.initialMargin)" :sub="shareText(sizingRow(idx)!.initialMargin)" />
                    <StatTile :boxed="false" :label="t('calc.colEstLiq')" :value="formatPrice(sizingRow(idx)!.liquidation)" :sub="liqText(sizingRow(idx)!)" value-class="text-warning" />
                  </dl>
                </div>
                <details v-if="position.entryOrders?.length" class="mt-3 rounded-lg border border-border/40 bg-surface/60">
                  <summary class="cursor-pointer rounded-lg px-3 py-2 text-xs font-semibold text-accent">{{ t("pos.entryFills") }} · {{ t("pos.entryOrdersCount", { n: position.entryOrders.length }) }}</summary>
                  <ul class="divide-y divide-border/40 px-3 pb-1.5 font-mono text-xs tabular-nums">
                    <li v-for="order in position.entryOrders" :key="orderKey(order)" class="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 py-1.5">
                      <time class="text-muted">{{ formatDateTime(order.t ?? order.time, { short: true }) }}</time>
                      <span class="font-sans">{{ entryDirection(order, position) }}</span>
                      <span><strong class="font-semibold">{{ unit === "both" ? formatQty(order.q ?? order.qty) : sizeText(order.q ?? order.qty, orderNotional(order), order.p ?? order.price ?? order.avgPrice) }}</strong> <span class="text-accent">@ {{ formatPrice(order.p ?? order.price ?? order.avgPrice) }}</span></span>
                      <span v-if="unit === 'both'" class="text-muted">{{ formatUsd(orderNotional(order)) }}</span>
                    </li>
                  </ul>
                </details>
              </div>
              <PriceActionPanel v-if="position.analysis?.ok" :analysis="position.analysis" />
              <p v-else class="tile self-start text-xs text-muted">{{ t("pa.pending") }}</p>
            </div>
          </article>
        </template>
        <div v-if="withCalculator && sizing && sizing.rows.length" class="rounded-xl border border-border/50 bg-background/40 p-3 sm:p-4">
          <h4 class="eyebrow mb-3">{{ t("calc.summaryTitle") }}</h4>
          <dl class="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <StatTile :label="t('calc.totalNotional')" :value="formatUsd(sizing.totalNotional)" :sub="t('calc.positionCount', { n: sizing.rows.length })" />
            <StatTile :label="t('calc.portfolioLev')" :value="formatLeverage(sizing.portfolioLeverage)" value-class="text-accent"
              :sub="`${t('calc.leadLeverage')} ${formatLeverage(summary?.totalNotionalLeverage)}`" />
            <StatTile :label="t('calc.marginOccupied')" :value="formatUsd(sizing.totalMargin)" :sub="shareText(sizing.totalMargin)" />
            <StatTile :label="t('calc.freeCashCushion')" :value="formatUsd(sizing.freeCash)" :value-class="sizing.freeCash == null ? '' : overCommitted ? 'text-danger' : 'text-success'"
              :sub="sizing.buffer == null ? '' : `${t('calc.buffer')} ±${formatPct(sizing.buffer, 2, false)}`" />
          </dl>
          <div v-if="marginPct != null" class="mt-3">
            <div class="meter-track" aria-hidden="true"><span class="h-full rounded-full" :class="overCommitted ? 'bg-danger' : 'bg-accent'" :style="{ width: `${Math.min(100, Math.max(0, marginPct))}%` }" /></div>
            <div class="mt-1.5 flex flex-wrap justify-between gap-2 text-xs">
              <span :class="overCommitted ? 'font-semibold text-danger' : 'text-muted'">{{ overCommitted ? t("calc.overCommitted") : t("calc.barMargin", { pct: formatPct(marginPct, 1, false) }) }}</span>
              <span class="text-muted">{{ t("calc.barFree", { pct: formatPct(Math.max(0, 100 - marginPct), 1, false) }) }}</span>
            </div>
          </div>
          <p class="mt-3 text-xs text-muted">{{ t("calc.crossMarginNotice") }}</p>
          <p class="mt-1 text-xs text-muted">{{ t("calc.estimateNotice") }}</p>
        </div>
        <p class="text-xs leading-relaxed text-faint">{{ t("tel.formulaNote", { mb: formatUsd(marginBalance), src: mmrSource }) }}</p>
      </div>
    </div>
  </section>
</template>
