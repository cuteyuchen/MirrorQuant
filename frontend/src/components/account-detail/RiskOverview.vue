<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "@/i18n";
import type { AccountDetailRisk } from "@/types/api";
import { formatLeverage, formatPct, formatUsd, signedUsd } from "@/utils/format";
import { pnlClass } from "@/utils/status";
import StatTile from "@/components/StatTile.vue";
import DataQualityBadge from "./DataQualityBadge.vue";

const props = defineProps<{ risk: AccountDetailRisk }>();
const { t } = useI18n();

const riskBadgeKey = computed(() => {
  switch (props.risk.riskLevel) {
    case "critical":
      return "accountDetail.risk.level.critical";
    case "warning":
      return "accountDetail.risk.level.warning";
    case "normal":
      return "accountDetail.risk.level.normal";
    default:
      return "accountDetail.risk.level.unknown";
  }
});

const riskToneClass = computed(() => {
  switch (props.risk.riskLevel) {
    case "critical":
      return "tone-danger";
    case "warning":
      return "tone-warning";
    case "normal":
      return "tone-success";
    default:
      return "tone-neutral";
  }
});

const mrSub = computed(() => {
  const parts: string[] = [];
  if (props.risk.initialMarginRatioPct != null) {
    parts.push(t("accountDetail.risk.imRatio", { val: formatPct(props.risk.initialMarginRatioPct, 2, false) }));
  }
  if (props.risk.marginRatioSource === "exchange") {
    parts.push(t("accountDetail.risk.mrSource.exchange"));
  } else if (props.risk.marginRatioSource === "derived") {
    parts.push(t("accountDetail.risk.mrSource.derived"));
  }
  return parts.join(" · ") || null;
});

const liqSub = computed(() => {
  const parts: string[] = [];
  if (props.risk.closestLiquidationSymbol) parts.push(props.risk.closestLiquidationSymbol);
  if (props.risk.maxSinglePositionSharePct != null) {
    parts.push(t("accountDetail.risk.maxShare", { val: formatPct(props.risk.maxSinglePositionSharePct, 1, false) }));
  }
  return parts.join(" · ") || null;
});
</script>

<template>
  <section class="card" aria-labelledby="risk-overview-title">
    <header class="card-header">
      <div class="flex flex-wrap items-center gap-2">
        <h2 id="risk-overview-title" class="section-title">{{ t("accountDetail.risk.title") }}</h2>
        <span class="pill text-xs font-semibold" :class="riskToneClass">{{ t(riskBadgeKey) }}</span>
        <span v-if="risk.canTrade === false" class="pill tone-danger text-xs">{{ t("accountDetail.risk.canTradeFalse") }}</span>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <span v-if="risk.multiAssetsMargin != null" class="pill tone-neutral text-[11px]">
          {{ t("accountDetail.risk.multiAsset") }}: {{ risk.multiAssetsMargin ? t("common.yes") : t("common.no") }}
        </span>
        <DataQualityBadge :quality="risk.dataQuality" />
      </div>
    </header>

    <div class="card-body space-y-4">
      <dl class="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
        <StatTile
          :label="t('accountDetail.risk.equity')"
          :value="risk.equity != null ? formatUsd(risk.equity) : null"
          :sub="risk.walletBalance != null ? `${t('accountDetail.risk.walletBalance')}: ${formatUsd(risk.walletBalance)}` : null"
        />
        <StatTile
          :label="t('accountDetail.risk.available')"
          :value="risk.availableBalance != null ? formatUsd(risk.availableBalance) : null"
        />
        <StatTile
          :label="t('accountDetail.risk.upl')"
          :value="risk.unrealizedPnl != null ? signedUsd(risk.unrealizedPnl) : null"
          :value-class="pnlClass(risk.unrealizedPnl)"
        />
        <StatTile
          :label="t('accountDetail.risk.todayPnl')"
          :value="risk.todayPnl != null ? signedUsd(risk.todayPnl) : null"
          :value-class="pnlClass(risk.todayPnl)"
          :sub="risk.todayNetFlow != null ? t('accountDetail.risk.todayFlow', { val: signedUsd(risk.todayNetFlow) }) : null"
        />
        <StatTile
          :label="t('accountDetail.risk.totalNotional')"
          :value="risk.totalNotional != null ? formatUsd(risk.totalNotional) : null"
          :sub="risk.longNotional != null && risk.shortNotional != null
            ? t('accountDetail.risk.longShortNet', { long: formatUsd(risk.longNotional, 0), short: formatUsd(risk.shortNotional, 0), net: signedUsd(risk.netNotional) })
            : null"
        />
        <StatTile
          :label="t('accountDetail.risk.leverage')"
          :value="risk.effectiveLeverage != null ? formatLeverage(risk.effectiveLeverage) : null"
          :sub="risk.allocatedNotional != null
            ? t('accountDetail.risk.allocSplit', { alloc: formatUsd(risk.allocatedNotional, 0), unalloc: formatUsd(risk.unallocatedNotional, 0) })
            : null"
        />
        <StatTile
          :label="t('accountDetail.risk.marginRatio')"
          :value="risk.marginRatioPct != null ? formatPct(risk.marginRatioPct, 2, false) : null"
          :value-class="(risk.marginRatioPct ?? 0) >= 80 ? 'text-danger' : (risk.marginRatioPct ?? 0) >= 50 ? 'text-warning' : ''"
          :sub="mrSub"
        />
        <StatTile
          :label="t('accountDetail.risk.closestLiq')"
          :value="risk.closestLiquidationDistancePct != null ? formatPct(risk.closestLiquidationDistancePct, 2, false) : null"
          :value-class="risk.closestLiquidationDistancePct != null && risk.closestLiquidationDistancePct < 5 ? 'text-danger' : risk.closestLiquidationDistancePct != null && risk.closestLiquidationDistancePct < 10 ? 'text-warning' : ''"
          :sub="liqSub || (risk.hwmEquity != null ? `${t('accountDetail.risk.drawdown')}: ${formatPct(risk.drawdownPct, 2, false)}` : null)"
        />
      </dl>

      <div v-if="risk.otherAssets?.length" class="rounded-xl border border-border/60 bg-background/40 p-3">
        <p class="mb-2 text-xs font-medium text-muted">{{ t("accountDetail.risk.otherAssetsTitle") }}</p>
        <div class="flex flex-wrap gap-3">
          <div v-for="asset in risk.otherAssets" :key="asset.currency" class="rounded-lg bg-surface px-3 py-1.5 text-xs shadow-sm">
            <strong class="font-semibold">{{ asset.currency }}</strong>
            <span class="ml-2 font-mono tabular-nums">{{ asset.equity != null ? asset.equity.toFixed(4) : t("common.unknown") }}</span>
            <span v-if="asset.available != null" class="ml-2 text-muted">({{ t("accountDetail.risk.available") }} {{ asset.available.toFixed(4) }})</span>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>
