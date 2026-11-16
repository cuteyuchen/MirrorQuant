<script setup lang="ts">
import { computed } from "vue";
import { BrainCircuit } from "lucide-vue-next";
import type { PriceAnalysis } from "@/types/api";
import { useI18n } from "@/i18n";
import { finiteNumber, formatPct, formatPrice, formatQty } from "@/utils/format";
import StatTile from "@/components/StatTile.vue";

// PA / SMC 结构分析全部来自服务端，前端只负责排版；缺失的点位保留破折号，不做推算。
const props = defineProps<{ analysis: PriceAnalysis }>();
const { t, isZh } = useI18n();
type TextKey = "verdict" | "trend1h" | "trend4h" | "entryZone" | "scalingStyle" | "summary";
// 中文界面优先用服务端给出的中文结论，缺失时退回英文原文。
function localized(key: TextKey) {
  return (isZh.value ? props.analysis[`${key}Zh`] ?? props.analysis[key] : props.analysis[key]) ?? t("common.unknown");
}
// 结论等级：good 顺势占优、mid 结构良好、warn 中性博弈、bad 逆势高险。
const verdictTone = computed(() => {
  const level = props.analysis.verdictLevel;
  return level === "good" ? "tone-success" : level === "mid" ? "tone-accent" : level === "warn" ? "tone-warning" : level === "bad" ? "tone-danger" : "tone-neutral";
});
// 区间条上的落点限制在 0–100，文字保留真实分位；分位未知时既不画标记也不写数字。
function mark(value?: number | null) {
  const number = finiteNumber(value);
  return number == null ? null : { left: `${Math.max(0, Math.min(100, number))}%`, text: String(Math.round(number * 10) / 10) };
}
const entryMark = computed(() => mark(props.analysis.entryRangePct));
const curMark = computed(() => mark(props.analysis.curRangePct));
function gap(range?: number[] | null) {
  return range && range.length >= 2 ? `${formatPrice(range[0])} ~ ${formatPrice(range[1])}` : t("common.unknown");
}
const trends = computed(() => [
  { key: "1h", label: t("pa.trend1h"), value: localized("trend1h") },
  { key: "4h", label: t("pa.trend4h"), value: localized("trend4h") },
  { key: "zone", label: t("pa.entryZone"), value: entryMark.value ? `${localized("entryZone")} · ${t("pa.percentile", { pct: entryMark.value.text })}` : localized("entryZone") },
  { key: "scale", label: t("pa.scaling"), value: localized("scalingStyle") },
]);
const levels = computed(() => {
  const analysis = props.analysis;
  const ema = finiteNumber(analysis.entryVsEma21Atr);
  const reward = finiteNumber(analysis.rrFromEntry);
  const atr = finiteNumber(analysis.atr1hPct);
  return [
    { key: "resistance", label: t("pa.keyRes"), value: formatPrice(analysis.keyResistance), valueClass: "text-danger" },
    { key: "support", label: t("pa.keySup"), value: formatPrice(analysis.keySupport), valueClass: "text-success" },
    { key: "invalidation", label: t("pa.invalidation"), value: formatPrice(analysis.invalidationLevel), valueClass: "text-warning" },
    { key: "targets", label: t("pa.targets"), value: `${formatPrice(analysis.target1)} / ${formatPrice(analysis.target2)}`, sub: reward == null ? "" : `${formatQty(reward)}R` },
    { key: "rsi", label: t("telemetry.indicators"), value: `${formatQty(analysis.rsi1h)} / ${formatQty(analysis.rsi4h)}`, sub: atr == null ? "" : `ATR ${formatPct(atr, 2, false)}` },
    { key: "ema", label: t("pa.emaDist"), value: ema == null ? t("common.unknown") : `${ema > 0 ? "+" : ""}${formatQty(ema)} ATR` },
    { key: "bear", label: t("pa.bearFvg"), value: gap(analysis.nearestBearFvg), valueClass: "text-danger" },
    { key: "bull", label: t("pa.bullFvg"), value: gap(analysis.nearestBullFvg), valueClass: "text-success" },
  ];
});
</script>
<template>
  <div class="cq min-w-0 rounded-xl border border-border/50 bg-surface/70 p-3">
    <div class="flex flex-wrap items-center gap-x-2 gap-y-1.5">
      <BrainCircuit :size="16" class="shrink-0 text-accent" aria-hidden="true" />
      <h5 class="text-sm font-semibold">{{ t("pa.heading") }}</h5>
      <span class="pill font-semibold" :class="verdictTone">{{ localized("verdict") }}</span>
    </div>
    <!-- 趋势、开仓区位与加仓手法：一行读完结构判断。 -->
    <ul class="mt-2.5 flex flex-wrap gap-1.5">
      <li v-for="item in trends" :key="item.key" class="pill !shrink"><span>{{ item.label }}</span><strong class="font-semibold text-foreground">{{ item.value }}</strong></li>
    </ul>
    <!-- 72 小时区间条：方块是开仓均价，圆点是现价，中线是 50% 均衡位。 -->
    <div class="mt-3.5">
      <p class="mb-2 flex flex-wrap items-center justify-between gap-x-3 gap-y-1 text-xs text-muted">
        <span>{{ t("pa.rangeTitle") }}</span>
        <span class="flex flex-wrap gap-x-3 gap-y-1 font-mono tabular-nums">
          <span v-if="entryMark" class="inline-flex items-center gap-1 text-warning"><span class="h-2 w-2 rounded-sm bg-warning" aria-hidden="true" />{{ t("pa.entryMark", { pct: entryMark.text }) }}</span>
          <span v-if="curMark" class="inline-flex items-center gap-1 text-accent"><span class="h-2 w-2 rounded-full bg-accent" aria-hidden="true" />{{ t("pa.curMark", { pct: curMark.text }) }}</span>
        </span>
      </p>
      <div class="range-track" aria-hidden="true">
        <span class="absolute inset-y-0 left-1/2 w-px bg-muted/60" />
        <span v-if="entryMark" class="absolute top-1/2 h-4 w-1.5 -translate-x-1/2 -translate-y-1/2 rounded-sm bg-warning ring-2 ring-surface" :style="{ left: entryMark.left }" />
        <span v-if="curMark" class="absolute top-1/2 h-3.5 w-3.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-accent ring-2 ring-surface" :style="{ left: curMark.left }" />
      </div>
      <div class="mt-2 grid grid-cols-3 gap-2 font-mono text-xs tabular-nums text-muted">
        <span>{{ t("pa.low72") }} <span class="font-semibold text-foreground">{{ formatPrice(analysis.low72h) }}</span></span>
        <span class="text-center">{{ t("pa.eq50") }} <span class="font-semibold text-foreground">{{ formatPrice(analysis.eq72h) }}</span></span>
        <span class="text-right">{{ t("pa.high72") }} <span class="font-semibold text-foreground">{{ formatPrice(analysis.high72h) }}</span></span>
      </div>
    </div>
    <dl class="cq-tiles mt-3.5">
      <StatTile v-for="item in levels" :key="item.key" :label="item.label" :value="item.value" :sub="item.sub" :value-class="item.valueClass" />
    </dl>
    <p v-if="analysis.summary || analysis.summaryZh" class="mt-3 text-xs leading-relaxed text-muted">{{ localized("summary") }}</p>
  </div>
</template>
