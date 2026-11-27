<script setup lang="ts">
import { computed, ref, useId } from "vue";
import { Calculator, ChevronDown, Copy } from "lucide-vue-next";
import { useI18n } from "@/i18n";
import { useDisplayUnit } from "@/composables/useDisplayUnit";
import { useUiStore } from "@/stores/ui";
import { useCopyCapital } from "@/composables/useCopyCapital";
import { calculateSizing } from "@/utils/sizing";
import { baseAsset, finiteNumber, formatLeverage, formatPct, formatPrice, formatQty, formatUsd } from "@/utils/format";
import { positionsConfirmed } from "@/utils/positions";
import { sideLabel } from "@/utils/status";
import type { LeadSnapshot } from "@/types/api";
import StatTile from "@/components/StatTile.vue";

const props = defineProps<{ snapshot?: LeadSnapshot | null; displayName?: string | null; compact?: boolean }>();
const { t } = useI18n();
const { showQuantity, showNotional } = useDisplayUnit();
const ui = useUiStore();
const bodyId = useId();
const capitalId = useId();
const PRESETS = [500, 1000, 2000, 5000, 10000];
const MODES = [{ value: 0.5, label: "calc.multDefensive" }, { value: 1, label: "calc.multProportional" }, { value: 1.2, label: "calc.multAggressive" }];
const capitalInput = useCopyCapital();
const multiplier = ref(1);
const collapsed = ref(false);
const copying = ref(false);
// 本金必须是有限正数；清空或填 0 时整份测算失败关闭，而不是按默认值继续算。
const capital = computed(() => {
  const value = finiteNumber(capitalInput.value);
  return value != null && value > 0 && value <= 1e12 ? value : null;
});
const summary = computed(() => props.snapshot?.openPositionsSummary ?? null);
const positions = computed(() => props.snapshot?.openPositions ?? []);
// 缺失列表与确知空仓分开显示，不能把未知仓位提示为无需跟单。
const positionsKnown = computed(() => positionsConfirmed(props.snapshot));
const leadMargin = computed(() => finiteNumber(summary.value?.marginBalance ?? props.snapshot?.profile?.marginBalance));
const result = computed(() => calculateSizing(positions.value, leadMargin.value, capital.value, multiplier.value, summary.value));
type SizingRow = (typeof result.value)["rows"][number];
const marginPct = computed(() => share(result.value.totalMargin));
const overCommitted = computed(() => result.value.freeCash != null && result.value.freeCash < 0);

function share(value: number | null) { return value == null || capital.value == null ? null : value / capital.value * 100; }
function sideClass(side: string) { return side === "LONG" ? "side-long" : side === "SHORT" ? "side-short" : "side-unknown"; }
function shareText(value: number | null) {
  const pct = share(value);
  return pct == null ? "" : t("calc.capitalShare", { pct: formatPct(pct, 1, false) });
}
function ratioText(row: SizingRow) { return row.ratio == null ? "" : `r=${row.ratio.toFixed(4)}`; }
// 强平参考距现价多少个百分点；任一价格未知就保持未知，不用零顶替。
function liqText(row: SizingRow) {
  if (row.liquidation == null || row.mark == null) return "";
  return `${t("board.fromMark")} ${formatPct((row.liquidation - row.mark) / row.mark * 100)}`;
}
function rowIssue(row: SizingRow) {
  return row.targetQty != null ? "" : t(row.entry == null || row.mark == null ? "calc.missingPrice" : "calc.unverifiedSizing");
}
// 复制的是只读测算说明，不调用任何下单接口。
async function copyPlan() {
  if (!result.value.complete || copying.value) return;
  copying.value = true;
  try {
    const sizing = result.value;
    const lines = [
      t("calc.planTitle", { name: props.displayName ?? props.snapshot?.profile?.nickname ?? t("board.lead") }),
      t("calc.estimated"),
      `${t("calc.capitalLabel")}: ${formatUsd(capital.value)} · ${t("calc.multiplierLabel")}: ${multiplier.value}x`,
      `${t("calc.totalNotional")}: ${formatUsd(sizing.totalNotional)} · ${t("calc.portfolioLev")}: ${formatLeverage(sizing.portfolioLeverage)}`,
      `${t("calc.marginOccupied")}: ${formatUsd(sizing.totalMargin)} (${formatPct(marginPct.value, 2, false)}) · ${t("calc.freeCashCushion")}: ${formatUsd(sizing.freeCash)}`,
      `${t("calc.buffer")}: ±${formatPct(sizing.buffer, 2, false)}`,
      "",
      ...sizing.rows.flatMap(row => [
        t("calc.planLine", { symbol: row.symbol ?? t("common.unknown"), side: sideLabel(row.side),
          qty: formatQty(row.targetQty), price: formatPrice(row.entry), leverage: formatQty(row.leverage) }),
        t("calc.planDetail", { notional: formatUsd(row.targetNotional), ratio: row.ratio == null ? t("common.unknown") : row.ratio.toFixed(4),
          margin: formatUsd(row.initialMargin), liq: formatPrice(row.liquidation) }),
      ]),
      "",
      t("calc.estimateNotice"),
    ];
    await navigator.clipboard.writeText(lines.join("\n"));
    ui.toast(t("calc.copied"));
  } catch { ui.toast(t("calc.copyError"), "danger"); }
  finally { copying.value = false; }
}
</script>
<template>
  <!-- 紧凑模式嵌在带单员卡片里，用浅色描边代替整张卡片，避免“卡中卡”。 -->
  <section class="min-w-0" :class="compact ? 'rounded-xl border border-accent/25 bg-accent/[0.04]' : 'card card-accent'">
    <header class="card-header">
      <div class="flex min-w-0 flex-1 items-center gap-3">
        <span class="section-icon" aria-hidden="true"><Calculator :size="18" /></span>
        <div class="min-w-0">
          <div class="flex flex-wrap items-center gap-2"><h3 class="section-title">{{ t("calc.title") }}</h3><span class="pill tone-accent">{{ t("calc.badge") }}</span></div>
          <p v-if="!compact" class="mt-0.5 text-xs text-muted">{{ t("calc.desc") }}</p>
        </div>
      </div>
      <div class="flex shrink-0 items-center gap-1.5">
        <button type="button" class="button-secondary" :disabled="!result.complete || copying" @click="copyPlan"><Copy :size="16" aria-hidden="true" />{{ t("calc.copyPlan") }}</button>
        <button v-if="!compact" type="button" class="icon-button" :aria-expanded="!collapsed" :aria-controls="bodyId"
          :title="t(collapsed ? 'calc.expand' : 'calc.collapse')" :aria-label="t(collapsed ? 'calc.expand' : 'calc.collapse')" @click="collapsed = !collapsed">
          <ChevronDown :size="18" class="transition-transform" :class="collapsed ? '' : 'rotate-180'" aria-hidden="true" />
        </button>
      </div>
    </header>
    <div v-show="!collapsed" :id="bodyId" class="card-body space-y-3" :class="compact ? '!p-3 sm:!p-4' : ''">
      <!-- 输入区：本金与快捷金额在左，测算系数与带单员本金在右。 -->
      <div class="flex flex-wrap items-end justify-between gap-3">
        <div class="min-w-0 flex-1 basis-72">
          <div class="flex flex-wrap items-center gap-2">
            <label :for="capitalId" class="field-label shrink-0">{{ t("calc.capitalLabel") }}</label>
            <input :id="capitalId" v-model="capitalInput" type="number" inputmode="decimal" min="0" step="100" class="form-input !min-h-9 w-36 !py-1 font-mono tabular-nums sm:w-40"
              :placeholder="t('calc.capitalPlaceholder')" :aria-invalid="capital == null" :aria-describedby="capital == null ? `${capitalId}-error` : undefined" />
            <div class="flex flex-wrap items-center gap-1" role="group" :aria-label="t('calc.quickPreset')">
              <button v-for="preset in PRESETS" :key="preset" type="button" class="chip-button !min-h-7 !px-2 !py-0.5" :aria-pressed="capital === preset" @click="capitalInput = preset">{{ preset.toLocaleString("en-US") }}</button>
            </div>
          </div>
          <p v-if="capital == null" :id="`${capitalId}-error`" class="field-error" role="alert">{{ t("calc.capitalInvalid") }}</p>
        </div>
        <fieldset class="min-w-0">
          <legend class="sr-only">{{ t("calc.multiplierLabel") }}</legend>
          <div class="flex flex-wrap items-center gap-2.5">
            <div class="tab-list" role="group">
              <button v-for="mode in MODES" :key="mode.value" type="button" class="tab-button !min-h-8 !px-2.5 !py-1 text-xs" :aria-pressed="multiplier === mode.value" @click="multiplier = mode.value">{{ t(mode.label) }}</button>
            </div>
            <p class="text-xs text-muted">{{ t("calc.leadMarginBal") }} <strong class="font-mono tabular-nums text-foreground">{{ formatUsd(leadMargin) }}</strong></p>
          </div>
          <p v-if="!compact" class="mt-1 text-xs text-faint">{{ t("calc.ratioFormula") }} · {{ t("calc.dynLevFormula") }}</p>
        </fieldset>
      </div>
      <p v-if="!positionsKnown" class="warning-banner" role="alert">{{ t("calc.unknownPositions") }}</p>
      <p v-else-if="!result.rows.length" class="tile text-sm text-muted">{{ t("calc.flatNotice") }}</p>
      <template v-else>
        <p v-if="!result.complete" class="warning-banner" role="alert">{{ t("calc.incomplete") }}</p>
        <!-- 宽屏：一行一个持仓，从比例一路推到手数、杠杆、保证金与强平参考。 -->
        <div class="hidden overflow-x-auto xl:block">
          <table class="data-table min-w-[920px]">
            <caption class="sr-only">{{ t("calc.title") }}</caption>
            <thead><tr>
              <th scope="col" class="text-left">{{ t("calc.colSymbol") }}</th>
              <th scope="col" class="number">{{ t("calc.colPrice") }}</th>
              <th scope="col" class="number">{{ t("calc.colRatio") }}</th>
              <th v-if="showNotional" scope="col" class="number">{{ t("calc.colTargetNotional") }}</th>
              <th v-if="showQuantity" scope="col" class="number">{{ t("calc.colTargetQty") }}</th>
              <th scope="col" class="number">{{ t("calc.colOptLev") }}</th>
              <th scope="col" class="number">{{ t("calc.colMarginReq") }}</th>
              <th scope="col" class="number">{{ t("calc.colEstLiq") }}</th>
            </tr></thead>
            <tbody>
              <tr v-for="row in result.rows" :key="row.key" :class="row.targetQty == null ? 'bg-warning/10' : 'hover:bg-surface2/40'">
                <td>
                  <div class="flex flex-wrap items-center gap-1.5"><strong>{{ row.symbol ?? t("common.unknown") }}</strong><span :class="sideClass(row.side)">{{ sideLabel(row.side) }}</span></div>
                  <p v-if="rowIssue(row)" class="mt-1 text-xs font-medium text-warning">{{ rowIssue(row) }}</p>
                </td>
                <td class="number"><p>{{ formatPrice(row.entry) }}</p><p class="text-xs text-muted">{{ t("board.mark") }} {{ formatPrice(row.mark) }}</p></td>
                <td class="number"><p>{{ formatPct(row.ratio == null ? null : row.ratio * 100, 1, false) }}</p><p class="text-xs text-muted">{{ ratioText(row) }}</p></td>
                <td v-if="showNotional" class="number font-semibold">{{ formatUsd(row.targetNotional) }}</td>
                <td v-if="showQuantity" class="number"><strong class="text-accent">{{ formatQty(row.targetQty) }}</strong><p class="text-xs text-muted">{{ baseAsset(row.symbol) }}</p></td>
                <td class="number"><span v-if="row.leverage != null" class="pill tone-accent font-mono font-semibold">{{ t("unit.leverage", { n: formatQty(row.leverage) }) }}</span><template v-else>{{ t("common.unknown") }}</template></td>
                <td class="number"><p>{{ formatUsd(row.initialMargin) }}</p><p class="text-xs text-muted">{{ shareText(row.initialMargin) }}</p></td>
                <td class="number"><p class="font-semibold text-warning">{{ formatPrice(row.liquidation) }}</p><p class="text-xs text-muted">{{ liqText(row) }}</p></td>
              </tr>
            </tbody>
          </table>
        </div>
        <!-- 窄屏：每个持仓一张小卡，最重要的目标手数与建议杠杆放在最上面。 -->
        <div class="grid min-w-0 gap-3 sm:grid-cols-2 xl:hidden">
          <article v-for="row in result.rows" :key="row.key" class="tile" :class="row.targetQty == null ? '!border-warning/40 !bg-warning/[0.06]' : ''">
            <div class="flex flex-wrap items-center gap-1.5">
              <strong>{{ row.symbol ?? t("common.unknown") }}</strong><span :class="sideClass(row.side)">{{ sideLabel(row.side) }}</span>
              <span v-if="row.leverage != null" class="pill tone-accent ml-auto font-mono font-semibold">{{ t("unit.leverage", { n: formatQty(row.leverage) }) }}</span>
            </div>
            <p v-if="rowIssue(row)" class="mt-1 text-xs font-medium text-warning">{{ rowIssue(row) }}</p>
            <dl class="mt-2.5 grid grid-cols-2 gap-x-3 gap-y-2.5">
              <StatTile v-if="showQuantity" :boxed="false" :label="t('calc.colTargetQty')" :value="formatQty(row.targetQty)" :sub="baseAsset(row.symbol)" value-class="text-accent" />
              <StatTile v-if="showNotional" :boxed="false" :label="t('calc.colTargetNotional')" :value="formatUsd(row.targetNotional)" :sub="ratioText(row)" />
              <StatTile :boxed="false" :label="t('calc.colPrice')" :value="formatPrice(row.entry)" :sub="`${t('board.mark')} ${formatPrice(row.mark)}`" />
              <StatTile :boxed="false" :label="t('calc.colMarginReq')" :value="formatUsd(row.initialMargin)" :sub="shareText(row.initialMargin)" />
              <StatTile :boxed="false" class="col-span-2" :label="t('calc.colEstLiq')" :value="formatPrice(row.liquidation)" :sub="liqText(row)" value-class="text-warning" />
            </dl>
          </article>
        </div>
        <!-- 组合汇总：总名义、整体杠杆、保证金占用与剩余现金，占用条一眼看出还剩多少缓冲。 -->
        <div class="rounded-xl border border-border/50 bg-background/40 p-3 sm:p-4">
          <h4 class="eyebrow mb-3">{{ t("calc.summaryTitle") }}</h4>
          <dl class="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <StatTile :label="t('calc.totalNotional')" :value="formatUsd(result.totalNotional)" :sub="t('calc.positionCount', { n: result.rows.length })" />
            <StatTile :label="t('calc.portfolioLev')" :value="formatLeverage(result.portfolioLeverage)" value-class="text-accent"
              :sub="`${t('calc.leadLeverage')} ${formatLeverage(summary?.totalNotionalLeverage)}`" />
            <StatTile :label="t('calc.marginOccupied')" :value="formatUsd(result.totalMargin)" :sub="shareText(result.totalMargin)" />
            <StatTile :label="t('calc.freeCashCushion')" :value="formatUsd(result.freeCash)" :value-class="result.freeCash == null ? '' : overCommitted ? 'text-danger' : 'text-success'"
              :sub="result.buffer == null ? '' : `${t('calc.buffer')} ±${formatPct(result.buffer, 2, false)}`" />
          </dl>
          <div v-if="marginPct != null" class="mt-3">
            <div class="meter-track" aria-hidden="true"><span class="h-full rounded-full" :class="overCommitted ? 'bg-danger' : 'bg-accent'" :style="{ width: `${Math.min(100, Math.max(0, marginPct))}%` }" /></div>
            <div class="mt-1.5 flex flex-wrap justify-between gap-2 text-xs">
              <span :class="overCommitted ? 'font-semibold text-danger' : 'text-muted'">{{ overCommitted ? t("calc.overCommitted") : t("calc.barMargin", { pct: formatPct(marginPct, 1, false) }) }}</span>
              <span class="text-muted">{{ t("calc.barFree", { pct: formatPct(Math.max(0, 100 - marginPct), 1, false) }) }}</span>
            </div>
          </div>
          <p class="mt-3 text-xs text-muted">{{ t("calc.crossMarginNotice") }}</p>
        </div>
      </template>
      <p class="text-xs text-muted">{{ t("calc.estimateNotice") }}</p>
    </div>
  </section>
</template>
