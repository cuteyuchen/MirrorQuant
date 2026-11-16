<script setup lang="ts">
import { computed, useId } from "vue";
import { useI18n } from "@/i18n";
import { formatDateTime, formatPct } from "@/utils/format";
import type { LeadSnapshot } from "@/types/api";
import EmptyState from "@/components/EmptyState.vue";
const props = defineProps<{ points: LeadSnapshot["roiChart365D"] }>();
const { t } = useI18n();
const gradientId = `roi-${useId().replace(/:/g, "")}`;
// 每个实例独立渐变 ID；按实际时间定位，未知点断开而不是补零。
const points = computed(() => [...(props.points ?? [])].filter(point => point.t != null && Number.isFinite(point.t)).sort((a, b) => a.t! - b.t!));
const chart = computed(() => {
  const valid = points.value.filter(point => point.v != null && Number.isFinite(point.v));
  if (valid.length < 2) return null;
  const min = Math.min(0, ...valid.map(point => point.v!));
  const max = Math.max(0.01, ...valid.map(point => point.v!));
  if (!Number.isFinite(max - min)) return null;
  const first = points.value[0].t!;
  const last = points.value[points.value.length - 1].t!;
  const x = (time: number) => 58 + (time - first) / (last - first || 1) * 608;
  const y = (value: number) => 16 + (max - value) / (max - min || 1) * 180;
  let connected = false;
  // 每段连续的已知点各自成线，同一批坐标再用来画线下的面积。
  const segments: { x: number; y: number }[][] = [];
  const line = points.value.map(point => {
    if (point.v == null || !Number.isFinite(point.v)) { connected = false; return ""; }
    const coord = { x: x(point.t!), y: y(point.v) };
    if (!connected) segments.push([]);
    segments[segments.length - 1].push(coord);
    const command = connected ? "L" : "M"; connected = true;
    return `${command}${coord.x},${coord.y}`;
  }).join(" ");
  const zero = y(0);
  // 面积从零轴画到曲线，只铺在连续线段下方，数据缺口处留白。
  const areas = segments.filter(segment => segment.length > 1).map(segment =>
    [`${segment[0].x},${zero}`, ...segment.map(coord => `${coord.x},${coord.y}`), `${segment[segment.length - 1].x},${zero}`].join(" "));
  const latest = valid[valid.length - 1];
  return { line, areas, zero, last: latest.v!, ticks: [max, (max + min) / 2, min].map(value => ({ value, y: y(value) })),
    // 末点圆点用 HTML 定位：SVG 被横向拉伸时圆不会变成椭圆。
    dot: { left: `${x(latest.t!) / 680 * 100}%`, top: `${y(latest.v!) / 212 * 100}%` },
    first, end: last, peak: Math.max(...valid.map(point => point.v!)) };
});
function axisLabel(value: number) {
  return Math.abs(value) >= 100_000 ? `${value.toExponential(1)}%` : formatPct(value, 1, false);
}
</script>
<template>
  <section class="card">
    <header class="card-header">
      <h3 class="section-title">{{ t("chart.title") }}</h3>
      <p v-if="chart" class="flex flex-wrap items-center gap-1.5">
        <span class="pill font-mono tabular-nums">{{ t("chart.peak", { val: formatPct(chart.peak) }) }}</span>
        <span class="pill font-mono font-semibold tabular-nums" :class="chart.last >= 0 ? 'tone-success' : 'tone-danger'">{{ t("chart.current", { val: formatPct(chart.last) }) }}</span>
      </p>
    </header>
    <div class="card-body">
      <EmptyState v-if="!chart" :title="t('chart.noData')" />
      <template v-else>
        <!-- 轴文案用普通 HTML 保持至少 12px，不随手机上的 SVG 缩小。 -->
        <div class="grid grid-cols-[4.5rem_minmax(0,1fr)] gap-x-2 gap-y-2">
          <div class="number relative h-52 text-xs text-muted md:h-60" data-testid="roi-axis">
            <span v-for="tick in chart.ticks" :key="tick.value" class="absolute right-0 -translate-y-1/2" :style="{ top: `${tick.y / 212 * 100}%` }">{{ axisLabel(tick.value) }}</span>
          </div>
          <div class="relative min-w-0" :class="chart.last >= 0 ? 'text-success' : 'text-danger'">
            <svg viewBox="0 0 680 212" preserveAspectRatio="none" class="block h-52 w-full md:h-60" role="img" :aria-label="t('chart.title')">
              <defs><linearGradient :id="gradientId" x1="0" x2="1"><stop offset="0%" stop-color="currentColor" stop-opacity=".65" /><stop offset="100%" stop-color="currentColor" /></linearGradient></defs>
              <line v-for="tick in chart.ticks" :key="tick.value" x1="58" x2="666" :y1="tick.y" :y2="tick.y" class="stroke-border" stroke-dasharray="3 5" vector-effect="non-scaling-stroke" />
              <!-- 零轴用实线：曲线在它上方是盈利，下方是亏损。 -->
              <line x1="58" x2="666" :y1="chart.zero" :y2="chart.zero" class="stroke-muted" stroke-opacity="0.55" vector-effect="non-scaling-stroke" />
              <path :d="chart.line" fill="none" :stroke="`url(#${gradientId})`" stroke-width="2.5" stroke-linejoin="round" vector-effect="non-scaling-stroke" />
              <polygon v-for="(area, index) in chart.areas" :key="index" :points="area" fill="currentColor" fill-opacity="0.1" />
            </svg>
            <span class="pointer-events-none absolute h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-current ring-2 ring-surface" :style="chart.dot" aria-hidden="true" />
          </div>
          <div class="col-start-2 flex flex-wrap justify-between gap-2 text-xs tabular-nums text-muted"><time>{{ formatDateTime(chart.first, { short: true }).split(" ")[0] }}</time><time>{{ formatDateTime(chart.end, { short: true }).split(" ")[0] }}</time></div>
        </div>
        <details class="mt-3 border-t border-border/50 pt-3"><summary class="cursor-pointer text-sm text-muted">{{ t("telemetry.chartTable") }}</summary><dl class="mt-3 grid grid-cols-2 gap-2 md:grid-cols-4"><div v-for="point in points" :key="point.t!" class="number text-xs"><dt class="text-muted">{{ formatDateTime(point.t, { short: true, withSeconds: false }) }}</dt><dd>{{ formatPct(point.v) }}</dd></div></dl></details>
      </template>
    </div>
  </section>
</template>
