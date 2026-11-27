<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "@/i18n";
import type { AccountPerformanceResponse } from "@/types/api";
import { formatPct, formatUsd, signedUsd } from "@/utils/format";
import { pnlClass } from "@/utils/status";
import StatTile from "@/components/StatTile.vue";
import DataQualityBadge from "./DataQualityBadge.vue";

const props = defineProps<{
  performance: AccountPerformanceResponse | null;
  loading?: boolean;
}>();

const { t } = useI18n();

function formatDurationMs(ms: number | null | undefined) {
  if (ms == null || !Number.isFinite(ms)) return "—";
  const mins = Math.round(ms / 60_000);
  if (mins < 60) return `${mins}m`;
  const hrs = Math.floor(mins / 60);
  const rem = mins % 60;
  return `${hrs}h ${rem}m`;
}

const slippageLine = computed(() => {
  const eq = props.performance?.executionQuality;
  if (!eq || eq.avgSlippagePct == null) return null;
  return `${formatPct(eq.avgSlippagePct, 3)} / ${formatPct(eq.medianSlippagePct, 3)} / ${formatPct(eq.p95SlippagePct, 3)}`;
});

const latencyLine = computed(() => {
  const eq = props.performance?.executionQuality;
  if (!eq || eq.avgLatencyMs == null) return null;
  return `${eq.avgLatencyMs}ms / ${eq.p95LatencyMs ?? "—"}ms`;
});
</script>

<template>
  <section class="card" aria-labelledby="performance-panel-title">
    <header class="card-header">
      <div class="flex flex-wrap items-center gap-2">
        <h2 id="performance-panel-title" class="section-title">{{ t("accountDetail.perf.title") }}</h2>
        <DataQualityBadge :quality="performance?.dataQuality" />
      </div>
    </header>

    <div class="card-body space-y-4" :aria-busy="loading">
      <dl class="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
        <StatTile
          :label="t('accountDetail.perf.closedCycles')"
          :value="performance?.tradeSummary?.closedCycleCount ?? null"
          :sub="performance?.tradeSummary?.incompleteCycleCount ? t('accountDetail.perf.incompleteCycles', { n: performance.tradeSummary.incompleteCycleCount }) : null"
        />
        <StatTile
          :label="t('accountDetail.perf.winRate')"
          :value="formatPct(performance?.tradeSummary?.winRatePct, 2, false)"
        />
        <StatTile
          :label="t('accountDetail.perf.payoff')"
          :value="performance?.tradeSummary?.payoffRatio != null ? performance.tradeSummary.payoffRatio.toFixed(2) : null"
          :sub="performance?.tradeSummary?.profitFactor != null ? `${t('accountDetail.perf.profitFactor')}: ${performance.tradeSummary.profitFactor.toFixed(2)}` : null"
        />
        <StatTile
          :label="t('accountDetail.perf.expectancy')"
          :value="signedUsd(performance?.tradeSummary?.expectancyUsdt)"
          :value-class="pnlClass(performance?.tradeSummary?.expectancyUsdt)"
          :sub="`${t('accountDetail.perf.avgHold')}: ${formatDurationMs(performance?.tradeSummary?.avgHoldingDurationMs)}`"
        />

        <StatTile
          :label="t('accountDetail.perf.makerRatio')"
          :value="formatPct(performance?.tradeSummary?.makerRatioPct, 1, false)"
        />
        <StatTile
          :label="t('accountDetail.perf.execSuccess')"
          :value="formatPct(performance?.executionQuality?.successRatePct, 1, false)"
          :sub="performance?.executionQuality
            ? t('accountDetail.perf.blockedStats', {
              blocked: performance.executionQuality.blockedCount,
              skipped: performance.executionQuality.skippedCount,
              failed: performance.executionQuality.failedCount,
            })
            : null"
        />
        <StatTile
          :label="t('accountDetail.perf.slippageStats')"
          :value="slippageLine"
        />
        <StatTile
          :label="t('accountDetail.perf.latencyStats')"
          :value="latencyLine"
          :sub="performance?.executionQuality
            ? t('accountDetail.perf.latencyCounts', { exact: performance.executionQuality.exactLatencyCount, est: performance.executionQuality.estimatedLatencyCount })
            : null"
        />
      </dl>

      <div v-if="performance?.executionQuality" class="flex flex-wrap gap-2 text-xs text-muted">
        <span class="pill font-mono tabular-nums">
          {{ t("accountDetail.perf.spreadStats", { n: performance.executionQuality.spreadSampleCount }) }}:
          <strong class="ml-1 text-foreground">{{ formatPct(performance.executionQuality.avgSpreadPct, 3, false) }}</strong>
        </span>
        <span v-for="rate in (performance.commissionRates ?? [])" :key="rate.symbol" class="pill font-mono tabular-nums">
          {{ rate.symbol }}: M {{ formatPct((rate.makerRate ?? 0) * 100, 3, false) }} / T {{ formatPct((rate.takerRate ?? 0) * 100, 3, false) }}
        </span>
      </div>

      <div v-if="performance?.bySymbol?.length" class="overflow-x-auto">
        <table class="data-table text-xs">
          <thead>
            <tr>
              <th class="text-left">Symbol</th>
              <th class="number">Volume</th>
              <th class="number">{{ t("accountDetail.pnl.realized") }}</th>
              <th class="number">{{ t("accountDetail.pnl.commission") }}</th>
              <th class="number">{{ t("accountDetail.perf.winRate") }}</th>
              <th class="number">{{ t("accountDetail.perf.makerRatio") }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in performance.bySymbol" :key="row.symbol">
              <td class="font-semibold">{{ row.symbol }}</td>
              <td class="number font-mono tabular-nums">{{ formatUsd(row.volumeUsd) }}</td>
              <td class="number font-mono tabular-nums" :class="pnlClass(row.realizedPnlUsdt)">{{ signedUsd(row.realizedPnlUsdt) }}</td>
              <td class="number font-mono tabular-nums">{{ formatUsd(row.commissionUsdt) }}</td>
              <td class="number font-mono tabular-nums">{{ formatPct(row.winRatePct, 1, false) }}</td>
              <td class="number font-mono tabular-nums">{{ formatPct(row.makerRatioPct, 1, false) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>
