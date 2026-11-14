<script setup lang="ts">
import { computed, ref } from "vue";
import {
  Calculator,
  Copy,
  Check,
  ChevronDown,
  ChevronUp,
  ShieldCheck,
  DollarSign,
  Wallet,
  ArrowUpRight,
  ArrowDownRight,
  Sparkles,
  Info,
  SlidersHorizontal,
} from "lucide-vue-next";
import { useI18n } from "@/i18n";
import { formatPrice, formatQty, formatUsd } from "@/utils/format";

export interface Props {
  snapshot?: any;
  leadMarginBalance?: number;
  openPositions?: any[];
  openPositionsSummary?: any;
  displayName?: string;
  initialCapital?: number;
  compact?: boolean;
}

const props = withDefaults(defineProps<Props>(), {
  initialCapital: 1000,
  compact: false,
});

const { t, isZh } = useI18n();

const capitalInput = ref<number | string>(props.initialCapital);
const multiplier = ref<number>(1.0);
const copied = ref(false);
const isExpanded = ref(true);

const quickPresets = [500, 1000, 2000, 5000, 10000];

const capital = computed<number>(() => {
  const val = Number(capitalInput.value);
  return Number.isNaN(val) || val < 0 ? 0 : val;
});

const summary = computed(() => props.openPositionsSummary || props.snapshot?.openPositionsSummary || {});
const profile = computed(() => props.snapshot?.profile || {});

const leadMarginBal = computed<number>(() => {
  if (props.leadMarginBalance && props.leadMarginBalance > 0) return Number(props.leadMarginBalance);
  if (summary.value.marginBalance && summary.value.marginBalance > 0) return Number(summary.value.marginBalance);
  if (profile.value.marginBalance && profile.value.marginBalance > 0) return Number(profile.value.marginBalance);
  return 0;
});

const positions = computed<any[]>(() => {
  if (props.openPositions && Array.isArray(props.openPositions)) return props.openPositions;
  if (props.snapshot?.openPositions && Array.isArray(props.snapshot.openPositions)) return props.snapshot.openPositions;
  return [];
});

// 1. First pass calculation of raw rows & MMR
const rawRows = computed(() => {
  const mb = leadMarginBal.value;
  const mult = multiplier.value;
  const cap = capital.value;

  return positions.value.map((p) => {
    const symbol = p.symbol || "UNKNOWN";
    const positionSide = (p.positionSide || "LONG").toUpperCase();
    const isShort = positionSide === "SHORT";
    const entryPrice = Number(p.avgEntry || p.currentPrice || p.markPrice || 1);
    const currentPrice = Number(p.currentPrice || p.markPrice || p.avgEntry || 1);
    const leadNotional = Number(p.entryValue || p.notional || (p.qty ? p.qty * p.avgEntry : 0) || 0);

    // Position ratio r = Lead Notional / Lead Margin Balance
    let r = 0;
    if (mb > 0) {
      r = leadNotional / mb;
    } else if (p.entryLeverage != null) {
      r = Number(p.entryLeverage);
    } else if (p.notionalLeverage != null) {
      r = Number(p.notionalLeverage);
    } else if (p.sharePct != null) {
      r = (Number(p.sharePct) / 100) * (summary.value.entryLeverage || 1);
    }

    const scaledR = r * mult;
    const targetNotional = cap * scaledR;
    const targetQty = entryPrice > 0 ? targetNotional / entryPrice : 0;

    // Dynamic Optimal Leverage: Clamp(ceil(scaledR / 0.05), 2, 50)
    const maxLevAllowed = p.mmr?.maxLev ? Math.min(50, Number(p.mmr.maxLev)) : 50;
    const optLev = Math.min(maxLevAllowed, Math.max(2, Math.ceil(scaledR / 0.05)));

    // Initial Margin Required = Target Notional / L_opt
    const initialMargin = optLev > 0 ? targetNotional / optLev : 0;

    // Maintenance Margin Rate (Binance MMR)
    const mmrPct = Number(p.mmr?.mmrPct ?? p.maintMarginPct ?? 0.65);
    const maintMargin = targetNotional * (mmrPct / 100);

    return {
      raw: p,
      symbol,
      positionSide,
      isShort,
      entryPrice,
      currentPrice,
      leadNotional,
      r,
      scaledR,
      targetNotional,
      targetQty,
      optLev,
      initialMargin,
      mmrPct,
      maintMargin,
      maxLevAllowed,
    };
  });
});

// 2. Portfolio Aggregates
const portfolio = computed(() => {
  const cap = capital.value;
  let totalNotional = 0;
  let totalInitialMargin = 0;
  let totalMaintMargin = 0;

  for (const r of rawRows.value) {
    totalNotional += r.targetNotional;
    totalInitialMargin += r.initialMargin;
    totalMaintMargin += r.maintMargin;
  }

  const portfolioLeverage = cap > 0 ? totalNotional / cap : 0;
  const marginOccupiedPct = cap > 0 ? (totalInitialMargin / cap) * 100 : 0;
  const freeCashCushion = Math.max(0, cap - totalInitialMargin);
  const freeCashPct = cap > 0 ? (freeCashCushion / cap) * 100 : 0;

  // Cross Margin adverse move buffer: (Total Margin - Total Maint Margin) / Total Notional * 100%
  const crossBufferPct =
    totalNotional > 0 && cap > 0 ? Math.max(0, ((cap - totalMaintMargin) / totalNotional) * 100) : 100;

  return {
    totalNotional,
    totalInitialMargin,
    totalMaintMargin,
    portfolioLeverage,
    marginOccupiedPct,
    freeCashCushion,
    freeCashPct,
    crossBufferPct,
  };
});

// 3. Final Formatted Rows with Liquidation Buffers
const calculatedRows = computed(() => {
  const bufPct = portfolio.value.crossBufferPct;

  return rawRows.value.map((r) => {
    // Under Cross margin, price can move up by bufPct (if short) or down by bufPct (if long)
    const estLiqPrice = r.isShort
      ? r.entryPrice * (1 + bufPct / 100)
      : Math.max(0, r.entryPrice * (1 - bufPct / 100));

    // Isolated margin buffer as safety reference
    const isoBufferPct = Math.max(0, (1 / r.optLev - r.mmrPct / 100) * 100);

    return {
      ...r,
      targetQtyFormatted: formatQty(r.targetQty),
      crossBufferPct: bufPct,
      bufferDisplay: (r.isShort ? "+" : "-") + bufPct.toFixed(2) + "%",
      estLiqPrice,
      isoBufferPct,
    };
  });
});

function selectPreset(amt: number) {
  capitalInput.value = amt;
}

async function copyPlan() {
  const cap = capital.value;
  const p = portfolio.value;
  const leadName = props.displayName || profile.value.nickname || "Lead Trader";

  const lines = [
    `=== ⚡ MirrorQuant 跟单开仓测算 [${leadName}] ===`,
    `💰 跟单资金: ${formatUsd(cap)} | 测算倍率: ${multiplier.value}x`,
    `📊 组合总名义: ${formatUsd(p.totalNotional, 2)} | 组合整体杠杆: ${p.portfolioLeverage.toFixed(2)}x`,
    `🔒 占用保证金: ${formatUsd(p.totalInitialMargin, 2)} (${p.marginOccupiedPct.toFixed(2)}%)`,
    `🛡️ 剩余现金安全垫: ${formatUsd(p.freeCashCushion, 2)} (${p.freeCashPct.toFixed(2)}%)`,
    `🛡️ 全仓抗爆安全垫: ±${p.crossBufferPct.toFixed(2)}%`,
    `----------------------------------------`,
    `📋 推荐开仓指令清单:`,
  ];

  calculatedRows.value.forEach((row, idx) => {
    const dir = row.isShort ? "做空 (SHORT)" : "做多 (LONG)";
    lines.push(
      `${idx + 1}. ${row.symbol} ${dir}` +
        `\n   • 目标名义: ${formatUsd(row.targetNotional, 2)} (占比 r=${(row.scaledR * 100).toFixed(1)}%)` +
        `\n   • 建议手数: ${row.targetQtyFormatted} @ ${formatPrice(row.entryPrice)}` +
        `\n   • 建议杠杆: ${row.optLev}x (初始占用: ${formatUsd(row.initialMargin, 2)})` +
        `\n   • 防爆防线: ${row.bufferDisplay} (预估强平参考: ${formatPrice(row.estLiqPrice)})`
    );
  });

  lines.push(`----------------------------------------`);
  lines.push(`⚠️ 提示: 全仓模式下保证金共用，请在交易所将逐个合约设置对应的推荐杠杆 L_opt。`);

  try {
    await navigator.clipboard.writeText(lines.join("\n"));
    copied.value = true;
    setTimeout(() => {
      copied.value = false;
    }, 2500);
  } catch (e) {
    console.error("Clipboard copy failed", e);
  }
}
</script>

<template>
  <div
    class="relative rounded-2xl bg-surface/90 border border-cyan-500/30 shadow-xl shadow-cyan-950/20 backdrop-blur-md overflow-hidden transition-all duration-300"
    :class="compact ? 'p-3 sm:p-4' : 'p-4 sm:p-5'"
  >
    <!-- Top Cyberpunk Accent Border Glow -->
    <div
      class="absolute top-0 left-0 right-0 h-[2px] bg-gradient-to-r from-transparent via-cyan-400 to-transparent opacity-80"
    ></div>

    <!-- Header & Toggle Bar -->
    <div class="flex flex-wrap items-center justify-between gap-3 pb-4 border-b border-slate-800/80">
      <div class="flex items-center gap-2.5 min-w-0 flex-1 basis-64">
        <div
          class="w-9 h-9 rounded-xl bg-gradient-to-br from-cyan-500/20 to-blue-500/10 border border-cyan-500/40 flex items-center justify-center shrink-0 shadow-inner shadow-cyan-500/20"
        >
          <Calculator class="w-4 h-4 text-cyan-400" />
        </div>
        <div class="min-w-0">
          <div class="flex items-center flex-wrap gap-2">
            <h3 class="text-base font-bold text-white tracking-tight flex items-center gap-1.5">
              <span>{{ t("calc.title") }}</span>
            </h3>
            <span
              class="px-2.5 py-1 rounded-full text-xs font-mono font-semibold bg-cyan-500/15 text-cyan-300 border border-cyan-500/30 flex items-center gap-1.5"
            >
              <Sparkles class="w-3 h-3" />
              {{ t("calc.badge") }}
            </span>
          </div>
          <p class="text-sm text-slate-300 leading-relaxed mt-1">
            {{ t("calc.desc") }}
          </p>
        </div>
      </div>

      <div class="flex items-center gap-2 shrink-0 ml-auto">
        <button
          v-if="calculatedRows.length > 0"
          type="button"
          @click="copyPlan"
          class="min-h-10 px-3 py-2 rounded-lg text-xs font-mono font-semibold transition-all duration-200 flex items-center gap-1.5 border cursor-pointer"
          :class="
            copied
              ? 'bg-emerald-500/20 text-emerald-400 border-emerald-500/40 shadow-sm shadow-emerald-500/20'
              : 'bg-slate-800/90 hover:bg-slate-700/90 text-cyan-300 border-cyan-500/30 hover:border-cyan-400/60'
          "
          :title="t('calc.copyPlan')"
        >
          <Check v-if="copied" class="w-3.5 h-3.5" />
          <Copy v-else class="w-3.5 h-3.5" />
          <span>{{ copied ? t("calc.copied") : t("calc.copyPlan") }}</span>
        </button>

        <button
          type="button"
          @click="isExpanded = !isExpanded"
          class="p-2.5 rounded-lg bg-slate-800/80 hover:bg-slate-700/80 border border-slate-700/80 text-slate-300 hover:text-white transition-colors"
          :title="isExpanded ? t('calc.collapse') : t('calc.expand')"
        >
          <ChevronUp v-if="isExpanded" class="w-4 h-4" />
          <ChevronDown v-else class="w-4 h-4" />
        </button>
      </div>
    </div>

    <!-- Collapsible Body Content -->
    <div v-show="isExpanded" class="mt-4 space-y-4">
      <!-- Controls Row: Capital Input, Quick Presets & Multiplier -->
      <div
        class="p-3.5 sm:p-4 rounded-xl bg-background/80 border border-slate-800/90 flex flex-wrap items-start justify-between gap-5"
      >
        <!-- Capital Input -->
        <div class="space-y-2 min-w-0 flex-1 basis-64 max-w-md">
          <label class="text-sm font-mono font-semibold text-slate-300 flex items-center flex-wrap gap-1.5">
            <Wallet class="w-3.5 h-3.5 text-cyan-400" />
            <span>{{ t("calc.capitalLabel") }}</span>
            <span v-if="leadMarginBal > 0" class="text-xs text-slate-300 font-normal">
              ({{ t("calc.leadMarginBal") }}: {{ formatUsd(leadMarginBal) }})
            </span>
          </label>
          <div class="relative flex items-center">
            <span class="absolute left-3 text-cyan-300 font-mono font-bold text-base">$</span>
            <input
              v-model="capitalInput"
              type="number"
              min="0"
              step="100"
              :placeholder="t('calc.capitalPlaceholder')"
              class="w-full pl-7 pr-3 py-2.5 rounded-xl bg-surface/90 border border-slate-700 focus:border-cyan-400 focus:ring-1 focus:ring-cyan-400/40 text-base font-mono font-bold text-white transition-all outline-none"
            />
          </div>

          <!-- Quick Select Buttons -->
          <div class="flex items-center flex-wrap gap-2 pt-1">
            <span class="text-xs font-mono text-slate-300 mr-1">{{ t("calc.quickPreset") }}:</span>
            <button
              v-for="preset in quickPresets"
              :key="preset"
              type="button"
              @click="selectPreset(preset)"
              class="min-h-10 px-3 py-2 rounded-lg text-xs font-mono font-semibold transition-colors border"
              :class="
                Number(capitalInput) === preset
                  ? 'bg-cyan-500/20 text-cyan-300 border-cyan-500/50'
                  : 'bg-surface/80 hover:bg-surface2 text-slate-300 border-slate-800 hover:text-white'
              "
            >
              {{ preset.toLocaleString() }}
            </button>
          </div>
        </div>

        <!-- Scale Mode Toggle -->
        <div class="space-y-2 min-w-0 flex-1 basis-64 max-w-md">
          <label class="text-sm font-mono font-semibold text-slate-300 flex items-center gap-1.5">
            <SlidersHorizontal class="w-3.5 h-3.5 text-cyan-400" />
            <span>{{ t("calc.multiplierLabel") }}</span>
          </label>
          <div class="grid grid-cols-[repeat(auto-fit,minmax(min(100%,6rem),1fr))] gap-2">
            <button
              type="button"
              @click="multiplier = 0.5"
              class="min-h-10 px-3 py-2 rounded-xl text-xs font-mono font-semibold transition-colors border"
              :class="
                multiplier === 0.5
                  ? 'bg-amber-500/20 text-amber-300 border-amber-500/50'
                  : 'bg-surface/80 hover:bg-surface2 text-slate-300 border-slate-800'
              "
            >
              {{ t("calc.multDefensive") }}
            </button>
            <button
              type="button"
              @click="multiplier = 1.0"
              class="min-h-10 px-3 py-2 rounded-xl text-xs font-mono font-semibold transition-colors border"
              :class="
                multiplier === 1.0
                  ? 'bg-cyan-500/20 text-cyan-300 border-cyan-500/50'
                  : 'bg-surface/80 hover:bg-surface2 text-slate-300 border-slate-800'
              "
            >
              {{ t("calc.multProportional") }}
            </button>
            <button
              type="button"
              @click="multiplier = 1.2"
              class="min-h-10 px-3 py-2 rounded-xl text-xs font-mono font-semibold transition-colors border"
              :class="
                multiplier === 1.2
                  ? 'bg-rose-500/20 text-rose-300 border-rose-500/50'
                  : 'bg-surface/80 hover:bg-surface2 text-slate-300 border-slate-800'
              "
            >
              {{ t("calc.multAggressive") }}
            </button>
          </div>
        </div>
      </div>

      <!-- Empty / Flat State Notice -->
      <div
        v-if="calculatedRows.length === 0"
        class="p-6 rounded-xl bg-background/50 border border-dashed border-slate-800 text-center space-y-2"
      >
        <ShieldCheck class="w-8 h-8 text-slate-400 mx-auto" />
        <div class="text-sm font-semibold text-slate-300">{{ t("tel.flatStatus") }}</div>
        <p class="text-sm text-slate-300 leading-relaxed max-w-md mx-auto">
          {{ t("calc.flatNotice") }}
        </p>
      </div>

      <!-- Active Positions Calculation Table & Cards -->
      <div v-else class="space-y-3">
        <!-- Desktop Table View (md and up) -->
        <div class="hidden md:block rounded-xl bg-background/70 border border-slate-800/90 overflow-hidden">
          <div class="overflow-x-auto">
            <table class="w-full min-w-[1100px] text-left text-sm border-collapse">
              <thead>
                <tr class="border-b border-slate-800 bg-surface/60 text-xs font-mono font-semibold uppercase text-slate-300 [&>th]:font-semibold">
                  <th class="py-2.5 px-3.5">{{ t("calc.colSymbol") }}</th>
                  <th class="py-2.5 px-3.5">{{ t("calc.colPrice") }}</th>
                  <th class="py-2.5 px-3.5 text-center">{{ t("calc.colRatio") }}</th>
                  <th class="py-2.5 px-3.5 text-right">{{ t("calc.colTargetNotional") }}</th>
                  <th class="py-2.5 px-3.5 text-right">{{ t("calc.colTargetQty") }}</th>
                  <th class="py-2.5 px-3.5 text-center">{{ t("calc.colOptLev") }}</th>
                  <th class="py-2.5 px-3.5 text-right">{{ t("calc.colMarginReq") }}</th>
                  <th class="py-2.5 px-3.5 text-right">{{ t("calc.colLiqBuffer") }}</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-slate-800/70 font-mono">
                <tr
                  v-for="row in calculatedRows"
                  :key="row.symbol + '_' + row.positionSide"
                  class="hover:bg-slate-800/40 transition-colors"
                >
                  <!-- Symbol & Side -->
                  <td class="py-3 px-3.5">
                    <div class="flex items-center gap-2">
                      <span class="text-base font-bold text-white tracking-tight">{{ row.symbol }}</span>
                      <span
                        class="px-2.5 py-1 rounded text-xs font-bold inline-flex items-center gap-1 whitespace-nowrap"
                        :class="
                          row.isShort
                            ? 'bg-rose-500/20 text-rose-400 border border-rose-500/30'
                            : 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'
                        "
                      >
                        <ArrowDownRight v-if="row.isShort" class="w-3 h-3" />
                        <ArrowUpRight v-else class="w-3 h-3" />
                        {{ row.isShort ? t("pos.short") : t("pos.long") }}
                      </span>
                    </div>
                  </td>

                  <!-- Entry / Current Price -->
                  <td class="py-3 px-3.5">
                    <div class="text-sm font-bold text-slate-100 whitespace-nowrap">{{ formatPrice(row.entryPrice) }}</div>
                    <div class="text-sm text-slate-300 whitespace-nowrap mt-1">现 {{ formatPrice(row.currentPrice) }}</div>
                  </td>

                  <!-- Position Ratio r -->
                  <td class="py-3 px-3.5 text-center">
                    <span
                      class="px-2.5 py-1.5 rounded-md bg-surface border border-slate-800 text-slate-100 font-bold text-sm"
                      :title="t('calc.ratioFormula')"
                    >
                      {{ (row.scaledR * 100).toFixed(1) }}%
                    </span>
                    <div class="text-xs text-slate-300 mt-0.5">r={{ row.scaledR.toFixed(4) }}</div>
                  </td>

                  <!-- Follower Target Notional -->
                  <td class="py-3 px-3.5 text-right text-base font-bold font-mono text-white whitespace-nowrap">
                    {{ formatUsd(row.targetNotional, 2) }}
                  </td>

                  <!-- Target Follower Quantity -->
                  <td class="py-3 px-3.5 text-right">
                    <div class="text-base font-bold font-mono text-cyan-300 whitespace-nowrap">
                      {{ row.targetQtyFormatted }}
                    </div>
                    <div class="text-xs text-slate-300">{{ row.symbol.replace(/USDT$/, "") }}</div>
                  </td>

                  <!-- Dynamic Optimal Leverage L_opt -->
                  <td class="py-3 px-3.5 text-center">
                    <div class="inline-flex flex-col items-center">
                      <span
                        class="px-3 py-1 rounded-full text-base font-bold font-mono bg-cyan-500/20 text-cyan-300 border border-cyan-500/40 shadow-sm shadow-cyan-500/10"
                        :title="t('calc.dynLevFormula')"
                      >
                        {{ row.optLev }}x
                      </span>
                      <span class="text-xs text-slate-300 mt-0.5">L_opt</span>
                    </div>
                  </td>

                  <!-- Initial Margin Required -->
                  <td class="py-3 px-3.5 text-right">
                    <div class="text-sm font-bold text-slate-100 whitespace-nowrap">{{ formatUsd(row.initialMargin, 2) }}</div>
                    <div class="text-xs text-slate-300">
                      {{ capital > 0 ? ((row.initialMargin / capital) * 100).toFixed(1) + "%" : "0%" }}
                    </div>
                  </td>

                  <!-- Liquidation Safety Buffer -->
                  <td class="py-3 px-3.5 text-right">
                    <div
                      class="inline-flex items-center gap-1.5 text-base font-bold font-mono whitespace-nowrap"
                      :class="row.isShort ? 'text-rose-400' : 'text-emerald-400'"
                    >
                      <ShieldCheck class="w-4 h-4 shrink-0" />
                      <span>{{ row.bufferDisplay }}</span>
                    </div>
                    <div class="text-xs text-slate-400 mt-0.5">强平 {{ formatPrice(row.estLiqPrice) }}</div>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- Mobile Card View (< md) -->
        <div class="md:hidden space-y-2.5">
          <div
            v-for="row in calculatedRows"
            :key="row.symbol + '_' + row.positionSide"
            class="p-3.5 rounded-xl bg-background/80 border border-slate-800/90 space-y-3"
          >
            <!-- Card Header -->
            <div class="flex flex-wrap items-center justify-between gap-3 pb-3 border-b border-slate-800/60">
              <div class="flex flex-wrap items-center gap-2 min-w-0">
                <span class="text-base font-bold text-white break-all">{{ row.symbol }}</span>
                <span
                  class="px-2.5 py-1 rounded text-xs font-bold inline-flex items-center gap-1 whitespace-nowrap"
                  :class="
                    row.isShort
                      ? 'bg-rose-500/20 text-rose-400 border border-rose-500/30'
                      : 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'
                  "
                >
                  <ArrowDownRight v-if="row.isShort" class="w-3 h-3" />
                  <ArrowUpRight v-else class="w-3 h-3" />
                  {{ row.isShort ? t("pos.short") : t("pos.long") }}
                </span>
              </div>
              <div class="flex items-center gap-1.5">
                <span class="text-xs text-slate-300">建议杠杆:</span>
                <span
                  class="px-3 py-1 rounded-full text-base font-bold font-mono bg-cyan-500/20 text-cyan-300 border border-cyan-500/40"
                >
                  {{ row.optLev }}x
                </span>
              </div>
            </div>

            <div class="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 text-sm font-mono">
              <span class="font-bold text-slate-100">均价 {{ formatPrice(row.entryPrice) }}</span>
              <span class="text-slate-300">现价 {{ formatPrice(row.currentPrice) }}</span>
            </div>

            <!-- Card Metric Grid adapts to the available width -->
            <div class="grid grid-cols-[repeat(auto-fit,minmax(min(100%,10rem),1fr))] gap-2.5 text-xs font-mono">
              <div class="min-w-0 p-3 rounded-lg bg-surface/70 border border-slate-800">
                <div class="text-xs text-slate-300">{{ t("calc.colTargetNotional") }}</div>
                <div class="text-lg font-bold font-mono text-white break-words mt-1">{{ formatUsd(row.targetNotional, 2) }}</div>
                <div class="text-xs text-slate-300 mt-1">r = {{ (row.scaledR * 100).toFixed(1) }}%</div>
              </div>

              <div class="min-w-0 p-3 rounded-lg bg-surface/70 border border-slate-800">
                <div class="text-xs text-slate-300">{{ t("calc.colTargetQty") }}</div>
                <div class="text-lg font-bold font-mono text-cyan-300 break-words mt-1">{{ row.targetQtyFormatted }}</div>
                <div class="text-xs text-slate-300 mt-1">{{ row.symbol.replace(/USDT$/, "") }}</div>
              </div>

              <div class="min-w-0 p-3 rounded-lg bg-surface/70 border border-slate-800">
                <div class="text-xs text-slate-300">{{ t("calc.colMarginReq") }}</div>
                <div class="text-base font-bold font-mono text-slate-100 break-words mt-1">{{ formatUsd(row.initialMargin, 2) }}</div>
                <div class="text-xs text-slate-300 mt-1">
                  {{ capital > 0 ? ((row.initialMargin / capital) * 100).toFixed(1) + "% 本金" : "—" }}
                </div>
              </div>

              <div class="min-w-0 p-3 rounded-lg bg-surface/70 border border-slate-800">
                <div class="text-xs text-slate-300">{{ t("calc.colLiqBuffer") }}</div>
                <div
                  class="text-lg font-bold font-mono mt-1 flex items-center gap-1.5"
                  :class="row.isShort ? 'text-rose-400' : 'text-emerald-400'"
                >
                  <ShieldCheck class="w-4 h-4 shrink-0" />
                  <span>{{ row.bufferDisplay }}</span>
                </div>
                <div class="text-xs text-slate-300 break-words mt-1">强平 {{ formatPrice(row.estLiqPrice) }}</div>
              </div>
            </div>
          </div>
        </div>

        <!-- 4. Follower Portfolio Summary Row -->
        <div class="rounded-xl bg-gradient-to-br from-surface via-surface2/60 to-surface border border-slate-800/90 p-4">
          <div class="flex flex-wrap items-center justify-between gap-2 mb-3">
            <span class="text-xs font-mono font-bold uppercase tracking-wider text-slate-300 flex items-center gap-1.5">
              <Sparkles class="w-3.5 h-3.5 text-cyan-400" />
              <span>{{ t("calc.summaryTitle") }}</span>
            </span>
            <span class="text-sm font-mono text-cyan-300">
              跟单本金: {{ formatUsd(capital) }}
            </span>
          </div>

          <!-- 4-KPI Grid -->
          <div class="grid grid-cols-[repeat(auto-fit,minmax(min(100%,14rem),1fr))] gap-3">
            <!-- 1. Total Notional -->
            <div class="min-w-0 p-4 rounded-xl bg-background/80 border border-slate-800/90">
              <div class="text-xs font-medium text-slate-300">{{ t("calc.totalNotional") }}</div>
              <div class="text-xl sm:text-2xl font-bold font-mono text-white tracking-tight break-words mt-1.5">
                {{ formatUsd(portfolio.totalNotional, 2) }}
              </div>
              <div class="text-xs font-mono text-slate-300 mt-1.5">
                {{ calculatedRows.length }} 个开仓品种
              </div>
            </div>

            <!-- 2. Portfolio Leverage -->
            <div class="min-w-0 p-4 rounded-xl bg-background/80 border border-slate-800/90">
              <div class="text-xs font-medium text-slate-300">{{ t("calc.portfolioLev") }}</div>
              <div class="text-xl sm:text-2xl font-bold font-mono text-cyan-300 tracking-tight break-words mt-1.5">
                {{ portfolio.portfolioLeverage.toFixed(2) }}x
              </div>
              <div class="text-xs font-mono text-slate-300 mt-1.5">
                带单净杠杆 {{ summary.totalNotionalLeverage ? Number(summary.totalNotionalLeverage).toFixed(2) + "x" : "—" }}
              </div>
            </div>

            <!-- 3. Margin Occupied -->
            <div class="min-w-0 p-4 rounded-xl bg-background/80 border border-slate-800/90">
              <div class="text-xs font-medium text-slate-300">{{ t("calc.marginOccupied") }}</div>
              <div class="text-xl sm:text-2xl font-bold font-mono text-white tracking-tight break-words mt-1.5">
                {{ formatUsd(portfolio.totalInitialMargin, 2) }}
              </div>
              <div class="text-xs font-mono text-slate-300 mt-1.5">
                占本金 {{ portfolio.marginOccupiedPct.toFixed(2) }}%
              </div>
            </div>

            <!-- 4. Free Cash Cushion -->
            <div class="min-w-0 p-4 rounded-xl bg-background/80 border border-slate-800/90">
              <div class="text-xs font-medium text-slate-300">{{ t("calc.freeCashCushion") }}</div>
              <div class="text-xl sm:text-2xl font-bold font-mono text-emerald-300 tracking-tight break-words mt-1.5">
                {{ formatUsd(portfolio.freeCashCushion, 2) }}
              </div>
              <div class="text-xs font-mono text-slate-300 mt-1.5">
                防爆安全垫 ±{{ portfolio.crossBufferPct.toFixed(1) }}%
              </div>
            </div>
          </div>

          <!-- Cushion Visual Bar -->
          <div class="mt-3.5 space-y-1.5">
            <div class="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 text-xs font-mono text-slate-300">
              <span>保证金占用 {{ portfolio.marginOccupiedPct.toFixed(1) }}%</span>
              <span class="text-emerald-300">空闲现金防爆缓冲 {{ portfolio.freeCashPct.toFixed(1) }}%</span>
            </div>
            <div class="h-2 rounded-full bg-slate-800 overflow-hidden flex">
              <div
                class="bg-gradient-to-r from-amber-500 to-amber-400 transition-all duration-300"
                :style="{ width: `${Math.min(100, portfolio.marginOccupiedPct)}%` }"
              ></div>
              <div
                class="bg-gradient-to-r from-emerald-500 to-cyan-400 transition-all duration-300"
                :style="{ width: `${Math.max(0, 100 - portfolio.marginOccupiedPct)}%` }"
              ></div>
            </div>
          </div>

          <!-- Bottom Operational Note -->
          <div class="mt-3 text-sm text-slate-300 flex items-start gap-2 leading-relaxed">
            <Info class="w-3.5 h-3.5 text-cyan-400 shrink-0 mt-0.5" />
            <span>{{ t("calc.crossMarginNotice") }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
