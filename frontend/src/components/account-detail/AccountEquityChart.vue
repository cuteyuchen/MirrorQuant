<script setup lang="ts">
import { computed, useId } from "vue";
import { useI18n } from "@/i18n";
import { formatDateTime, formatPct, formatUsd, signedUsd } from "@/utils/format";
import type { AccountEquityCurveResponse, AccountEquityPoint } from "@/types/api";
import type { DetailRange, EquitySeriesKey } from "@/composables/useAccountDetail";
import EmptyState from "@/components/EmptyState.vue";
import DataQualityBadge from "./DataQualityBadge.vue";

const props = defineProps<{
  curve: AccountEquityCurveResponse | null;
  range: DetailRange;
  series: EquitySeriesKey;
  loading?: boolean;
}>();

const emit = defineEmits<{
  "update:range": [value: DetailRange];
  "update:series": [value: EquitySeriesKey];
}>();

const { t } = useI18n();
const gradId = `acct-eq-${useId().replace(/:/g, "")}`;

const RANGES: DetailRange[] = ["24h", "7d", "30d", "90d", "1y", "all"];

const rangeLabels: Record<DetailRange, string> = {
  "24h": "accountDetail.range.24h",
  "7d": "accountDetail.range.7d",
  "30d": "accountDetail.range.30d",
  "90d": "accountDetail.range.90d",
  "1y": "accountDetail.range.1y",
  "all": "accountDetail.range.all",
};

const seriesLabels: Record<EquitySeriesKey, string> = {
  equity: "accountDetail.curve.series.equity",
  cumulativeTradingPnl: "accountDetail.curve.series.cumulativeTradingPnl",
  twrPct: "accountDetail.curve.series.twrPct",
};

function pointValue(p: AccountEquityPoint, key: EquitySeriesKey): number | null {
  const v = p[key];
  return v != null && Number.isFinite(v) ? v : null;
}

const chart = computed(() => {
  const pts = props.curve?.points ?? [];
  const valid = pts.filter(p => pointValue(p, props.series) != null);
  if (valid.length < 2) return null;

  const vals = valid.map(p => pointValue(p, props.series)!);
  let minVal = Math.min(...vals);
  let maxVal = Math.max(...vals);
  if (minVal === maxVal) {
    minVal -= 1;
    maxVal += 1;
  }

  const firstTs = pts[0].tsMs;
  const lastTs = pts[pts.length - 1].tsMs;
  const xPos = (ts: number) => 56 + ((ts - firstTs) / (lastTs - firstTs || 1)) * 608;
  const yPos = (val: number) => 16 + ((maxVal - val) / (maxVal - minVal || 1)) * 164;

  // Gap breaks (`gapBefore`) split lines strictly without interpolating across missing periods
  const segments: string[] = [];
  const estimatedPoints: { x: number; y: number; source: string; tsMs: number }[] = [];
  let curPath = "";

  for (let i = 0; i < pts.length; i++) {
    const pt = pts[i];
    const v = pointValue(pt, props.series);
    if (v == null || pt.gapBefore) {
      if (curPath) segments.push(curPath);
      curPath = "";
    }
    if (v == null) continue;
    const cx = xPos(pt.tsMs);
    const cy = yPos(v);
    curPath = curPath ? `${curPath} L${cx.toFixed(1)},${cy.toFixed(1)}` : `M${cx.toFixed(1)},${cy.toFixed(1)}`;
    if (pt.source === "exchange_daily" || pt.source === "estimated_backfill" || pt.dataQuality !== "ok") {
      estimatedPoints.push({ x: cx, y: cy, source: pt.source, tsMs: pt.tsMs });
    }
  }
  if (curPath) segments.push(curPath);

  // Drawdown sub-series plotted gently in lower band (0 to maxDd)
  const ddPts = pts.filter(p => p.drawdownPct != null && Number.isFinite(p.drawdownPct));
  const maxDd = Math.max(1, ...ddPts.map(p => p.drawdownPct!));
  const ddY = (dd: number) => 184 + (dd / maxDd) * 32;
  const ddPath = ddPts.length >= 2
    ? ddPts.map((p, idx) => `${idx === 0 ? "M" : "L"}${xPos(p.tsMs).toFixed(1)},${ddY(p.drawdownPct!).toFixed(1)}`).join(" ")
    : "";

  const transferMarkers = (props.curve?.transfers ?? []).map(tr => ({
    ...tr,
    x: xPos(Math.max(firstTs, Math.min(lastTs, tr.tsMs))),
  }));

  const ticks = [maxVal, (maxVal + minVal) / 2, minVal].map(v => ({ value: v, y: yPos(v) }));
  return {
    segments,
    estimatedPoints,
    ddPath,
    transferMarkers,
    ticks,
    firstTs,
    lastTs,
  };
});

function formatSeriesValue(val: number | null | undefined) {
  if (val == null || !Number.isFinite(val)) return "—";
  return props.series === "twrPct" ? formatPct(val, 2, true) : props.series === "cumulativeTradingPnl" ? signedUsd(val) : formatUsd(val);
}
</script>

<template>
  <section class="card" aria-labelledby="equity-curve-title">
    <header class="card-header">
      <div class="flex flex-wrap items-center gap-2">
        <h2 id="equity-curve-title" class="section-title">{{ t("accountDetail.curve.title") }}</h2>
        <DataQualityBadge :quality="curve?.dataQuality" />
      </div>

      <div class="flex flex-wrap items-center gap-2">
        <div class="inline-flex rounded-lg border border-border/60 bg-background/60 p-0.5 text-xs" role="group">
          <button
            v-for="sKey in (['equity', 'cumulativeTradingPnl', 'twrPct'] as EquitySeriesKey[])"
            :key="sKey"
            type="button"
            class="rounded-md px-2.5 py-1 transition"
            :class="series === sKey ? 'bg-accent text-[rgb(var(--c-on-accent))] font-semibold' : 'text-muted hover:text-foreground'"
            @click="emit('update:series', sKey)"
          >
            {{ t(seriesLabels[sKey]) }}
          </button>
        </div>

        <div class="inline-flex rounded-lg border border-border/60 bg-background/60 p-0.5 text-xs" role="group">
          <button
            v-for="r in RANGES"
            :key="r"
            type="button"
            class="rounded-md px-2 py-1 transition"
            :class="range === r ? 'bg-surface font-semibold text-foreground shadow-sm' : 'text-muted hover:text-foreground'"
            @click="emit('update:range', r)"
          >
            {{ t(rangeLabels[r]) }}
          </button>
        </div>
      </div>
    </header>

    <div class="card-body space-y-3" :aria-busy="loading">
      <div class="flex flex-wrap items-center justify-between gap-2 text-xs">
        <div class="flex flex-wrap items-center gap-2">
          <span class="pill font-mono tabular-nums">{{ t("accountDetail.curve.pointCount", { n: curve?.summary?.pointCount ?? 0 }) }}</span>
          <span class="pill font-mono tabular-nums">{{ t("accountDetail.curve.curDd", { val: formatPct(curve?.currentDrawdownPct, 2, false) }) }}</span>
          <span class="pill font-mono tabular-nums tone-warning">{{ t("accountDetail.curve.maxDd", { val: formatPct(curve?.maxDrawdownPct, 2, false) }) }}</span>
          <span v-if="curve?.transfers?.length" class="pill font-mono tabular-nums">{{ t("accountDetail.curve.transfersCount", { n: curve.transfers.length }) }}</span>
        </div>
      </div>

      <p v-if="curve && !curve.cashflowCovered && curve.points.length" class="rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
        {{ t("accountDetail.curve.cashflowUncovered") }}
      </p>

      <EmptyState v-if="!chart" :title="t('accountDetail.curve.noData')" />

      <template v-else>
        <div class="grid grid-cols-[4.75rem_minmax(0,1fr)] gap-2">
          <div class="relative h-56 select-none font-mono text-[11px] tabular-nums text-muted">
            <span
              v-for="(tk, i) in chart.ticks"
              :key="i"
              class="absolute right-0 -translate-y-1/2"
              :style="{ top: `${(tk.y / 224) * 100}%` }"
            >
              {{ formatSeriesValue(tk.value) }}
            </span>
          </div>

          <div class="min-w-0">
            <svg viewBox="0 0 680 224" class="h-56 w-full overflow-visible" role="img" :aria-label="t('accountDetail.curve.title')">
              <defs>
                <linearGradient :id="gradId" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stop-color="rgb(var(--c-accent))" stop-opacity="0.22" />
                  <stop offset="100%" stop-color="rgb(var(--c-accent))" stop-opacity="0" />
                </linearGradient>
              </defs>

              <line
                v-for="(tk, i) in chart.ticks"
                :key="`grid-${i}`"
                x1="56"
                x2="664"
                :y1="tk.y"
                :y2="tk.y"
                stroke="rgb(var(--c-border))"
                stroke-opacity="0.45"
                stroke-dasharray="3 3"
              />

              <!-- Transfer event vertical markers -->
              <g v-for="tr in chart.transferMarkers" :key="`tr-${tr.id}`">
                <line
                  :x1="tr.x"
                  :x2="tr.x"
                  y1="14"
                  y2="180"
                  :stroke="(tr.signedAmount ?? 0) >= 0 ? 'rgb(var(--c-success))' : 'rgb(var(--c-warning))'"
                  stroke-width="1.5"
                  stroke-dasharray="4 3"
                />
                <polygon
                  :points="`${tr.x},6 ${tr.x - 4},13 ${tr.x + 4},13`"
                  :fill="(tr.signedAmount ?? 0) >= 0 ? 'rgb(var(--c-success))' : 'rgb(var(--c-warning))'"
                >
                  <title>{{ `${tr.category} ${signedUsd(tr.signedAmount)} @ ${formatDateTime(tr.tsMs)}` }}</title>
                </polygon>
              </g>

              <!-- Continuous line segments broken at gapBefore -->
              <path
                v-for="(seg, idx) in chart.segments"
                :key="`seg-${idx}`"
                :d="seg"
                fill="none"
                stroke="rgb(var(--c-accent))"
                stroke-width="2.2"
                stroke-linecap="round"
                stroke-linejoin="round"
              />

              <!-- Diamond markers for exchange_daily / estimated / partial points -->
              <polygon
                v-for="(ep, idx) in chart.estimatedPoints"
                :key="`ep-${idx}`"
                :points="`${ep.x},${ep.y - 4.5} ${ep.x + 4.5},${ep.y} ${ep.x},${ep.y + 4.5} ${ep.x - 4.5},${ep.y}`"
                fill="rgb(var(--c-warning))"
                stroke="rgb(var(--c-surface))"
                stroke-width="1"
              >
                <title>{{ `${ep.source} @ ${formatDateTime(ep.tsMs)}` }}</title>
              </polygon>

              <!-- TWR drawdown band -->
              <path v-if="chart.ddPath" :d="chart.ddPath" fill="none" stroke="rgb(var(--c-danger))" stroke-width="1.4" stroke-opacity="0.85" />
            </svg>

            <div class="mt-1 flex items-center justify-between text-[11px] text-muted font-mono tabular-nums">
              <span>{{ formatDateTime(chart.firstTs, { short: true }) }}</span>
              <span>{{ formatDateTime(chart.lastTs, { short: true }) }}</span>
            </div>
          </div>
        </div>

        <p class="flex flex-wrap gap-3 text-[11px] text-muted">
          <span>{{ t("accountDetail.curve.gapNotice") }}</span>
          <span v-if="chart.estimatedPoints.length">· {{ t("accountDetail.curve.dailyNote") }}</span>
          <span>· {{ t("accountDetail.curve.twrDrawdown") }}</span>
        </p>

        <details v-if="curve?.points?.length" class="rounded-lg border border-border/50 bg-background/40 p-2.5 text-xs">
          <summary class="cursor-pointer font-medium text-muted hover:text-foreground">
            {{ t("accountDetail.curve.dataTableSummary") }}
          </summary>
          <div class="mt-2 max-h-60 overflow-auto">
            <table class="data-table text-xs">
              <thead>
                <tr>
                  <th class="text-left">{{ t("accountDetail.curve.colTime") }}</th>
                  <th class="text-left">{{ t("accountDetail.curve.colSource") }}</th>
                  <th class="number">{{ t("accountDetail.risk.equity") }}</th>
                  <th class="number">{{ t("accountDetail.curve.series.cumulativeTradingPnl") }}</th>
                  <th class="number">{{ t("accountDetail.curve.series.twrPct") }}</th>
                  <th class="number">{{ t("accountDetail.curve.colDd") }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="pt in curve.points" :key="pt.tsMs">
                  <td class="font-mono tabular-nums">{{ formatDateTime(pt.tsMs, { short: true }) }}</td>
                  <td>{{ pt.source }} ({{ pt.dataQuality }}){{ pt.gapBefore ? " · gap" : "" }}</td>
                  <td class="number font-mono tabular-nums">{{ formatUsd(pt.equity) }}</td>
                  <td class="number font-mono tabular-nums">{{ signedUsd(pt.cumulativeTradingPnl) }}</td>
                  <td class="number font-mono tabular-nums">{{ formatPct(pt.twrPct) }}</td>
                  <td class="number font-mono tabular-nums">{{ formatPct(pt.drawdownPct, 2, false) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </details>
      </template>
    </div>
  </section>
</template>
