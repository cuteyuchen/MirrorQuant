<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "@/i18n";
import type { AccountDetailPnl } from "@/types/api";
import { formatUsd, signedUsd } from "@/utils/format";
import { pnlClass } from "@/utils/status";
import StatTile from "@/components/StatTile.vue";
import DataQualityBadge from "./DataQualityBadge.vue";

const props = defineProps<{ pnl: AccountDetailPnl }>();
const { t } = useI18n();

const reconcileLabelKey = computed(() => {
  if (props.pnl.reconciled === true) return "accountDetail.pnl.reconciledOk";
  if (props.pnl.reconciled === false) return "accountDetail.pnl.reconciledMismatch";
  return "accountDetail.pnl.reconciledUnknown";
});

const reconcileToneClass = computed(() => {
  if (props.pnl.reconciled === true) return "tone-success";
  if (props.pnl.reconciled === false) return "tone-danger";
  return "tone-warning";
});

const totalRebates = computed(() => {
  const c = props.pnl.ledger?.commissionRebate;
  const r = props.pnl.ledger?.referralRebate;
  if (c == null && r == null) return null;
  return (c ?? 0) + (r ?? 0);
});

const bonusAndExt = computed(() => {
  const b = props.pnl.ledger?.bonus;
  const e = props.pnl.ledger?.otherExternalFlow;
  if (b == null && e == null) return null;
  return (b ?? 0) + (e ?? 0);
});
</script>

<template>
  <section class="card" aria-labelledby="pnl-breakdown-title">
    <header class="card-header">
      <div class="flex flex-wrap items-center gap-2">
        <h2 id="pnl-breakdown-title" class="section-title">{{ t("accountDetail.pnl.title") }}</h2>
        <span class="pill text-xs font-medium" :class="reconcileToneClass">{{ t(reconcileLabelKey) }}</span>
      </div>
      <DataQualityBadge :quality="pnl.dataQuality" />
    </header>

    <div class="card-body space-y-4">
      <dl class="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatTile :label="t('accountDetail.pnl.equityChange')" :value="signedUsd(pnl.totalEquityChange)" :value-class="pnlClass(pnl.totalEquityChange)" />
        <StatTile :label="t('accountDetail.pnl.tradingNet')" :value="signedUsd(pnl.tradingNetPnl)" :value-class="pnlClass(pnl.tradingNetPnl)" />
        <StatTile :label="t('accountDetail.pnl.ledgerNet')" :value="signedUsd(pnl.ledgerNetPnl)" :value-class="pnlClass(pnl.ledgerNetPnl)" />
        <StatTile :label="t('accountDetail.pnl.reconciliationDelta')" :value="signedUsd(pnl.reconciliationDelta)" :value-class="pnl.reconciled === false ? 'text-danger' : 'text-muted'" />

        <StatTile :label="t('accountDetail.pnl.deposits')" :value="formatUsd(pnl.deposits)" value-class="text-success" />
        <StatTile :label="t('accountDetail.pnl.withdrawals')" :value="formatUsd(pnl.withdrawals)" value-class="text-warning" />
        <StatTile :label="t('accountDetail.pnl.netTransfers')" :value="signedUsd(pnl.netTransfers)" />
        <StatTile :label="t('accountDetail.pnl.bonusExternal')" :value="signedUsd(bonusAndExt)" />

        <StatTile :label="t('accountDetail.pnl.realized')" :value="signedUsd(pnl.ledger?.realizedPnl)" :value-class="pnlClass(pnl.ledger?.realizedPnl)" />
        <StatTile :label="t('accountDetail.pnl.uplChange')" :value="signedUsd(pnl.unrealizedPnlChange)" :value-class="pnlClass(pnl.unrealizedPnlChange)" />
        <StatTile :label="t('accountDetail.pnl.funding')" :value="signedUsd(pnl.ledger?.fundingFee)" :value-class="pnlClass(pnl.ledger?.fundingFee)" />
        <StatTile
          :label="t('accountDetail.pnl.commission')"
          :value="signedUsd(pnl.ledger?.commission)"
          :sub="totalRebates != null ? `${t('accountDetail.pnl.rebate')}: ${signedUsd(totalRebates)}` : null"
        />
      </dl>

      <div v-if="pnl.attribution" class="space-y-2 rounded-xl border border-border/50 bg-background/40 p-3">
        <h3 class="text-xs font-semibold text-muted">{{ t("accountDetail.pnl.bySource") }}</h3>
        <div class="grid grid-cols-1 gap-2 sm:grid-cols-3 text-xs">
          <div
            v-for="src in (['system', 'manual', 'unknown'] as const)"
            :key="src"
            class="flex items-center justify-between rounded-lg bg-surface px-3 py-2"
          >
            <div>
              <strong class="font-medium">{{
                src === "system" ? t("accountDetail.trades.source.system")
                  : src === "manual" ? t("accountDetail.trades.source.manual")
                    : t("accountDetail.trades.source.unknown")
              }}</strong>
              <span class="ml-2 text-muted">({{ pnl.attribution[src]?.fillCount ?? 0 }})</span>
            </div>
            <span class="font-mono font-semibold tabular-nums" :class="pnlClass(pnl.attribution[src]?.netPnlUsdt)">
              {{ signedUsd(pnl.attribution[src]?.netPnlUsdt) }}
            </span>
          </div>
        </div>
      </div>

      <div v-if="pnl.bySymbol?.length" class="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <div class="rounded-xl border border-border/50 bg-background/40 p-3">
          <h3 class="mb-2 text-xs font-semibold text-muted">{{ t("accountDetail.pnl.bySymbol") }}</h3>
          <div class="overflow-x-auto">
            <table class="data-table text-xs">
              <thead>
                <tr>
                  <th class="text-left">Symbol</th>
                  <th class="number">{{ t("accountDetail.pnl.realized") }}</th>
                  <th class="number">{{ t("accountDetail.pnl.funding") }}</th>
                  <th class="number">{{ t("accountDetail.pnl.commission") }}</th>
                  <th class="number">{{ t("common.pnl") }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="item in pnl.bySymbol" :key="item.symbol">
                  <td class="font-semibold">{{ item.symbol }}</td>
                  <td class="number font-mono tabular-nums" :class="pnlClass(item.realizedPnlUsdt)">{{ signedUsd(item.realizedPnlUsdt) }}</td>
                  <td class="number font-mono tabular-nums" :class="pnlClass(item.fundingFeeUsdt)">{{ signedUsd(item.fundingFeeUsdt) }}</td>
                  <td class="number font-mono tabular-nums">{{ formatUsd(item.commissionUsdt) }}</td>
                  <td class="number font-mono font-semibold tabular-nums" :class="pnlClass(item.netPnlUsdt)">{{ signedUsd(item.netPnlUsdt) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div v-if="pnl.byBinding?.length" class="rounded-xl border border-border/50 bg-background/40 p-3">
          <h3 class="mb-2 text-xs font-semibold text-muted">{{ t("accountDetail.pnl.byBinding") }}</h3>
          <div class="overflow-x-auto">
            <table class="data-table text-xs">
              <thead>
                <tr>
                  <th class="text-left">{{ t("nav.bindings") }}</th>
                  <th class="number">{{ t("accountDetail.pnl.realized") }}</th>
                  <th class="number">{{ t("accountDetail.pnl.commission") }}</th>
                  <th class="number">{{ t("common.pnl") }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="item in pnl.byBinding" :key="item.bindingId">
                  <td class="font-semibold">{{ item.bindingName }}</td>
                  <td class="number font-mono tabular-nums" :class="pnlClass(item.realizedPnlUsdt)">{{ signedUsd(item.realizedPnlUsdt) }}</td>
                  <td class="number font-mono tabular-nums">{{ formatUsd(item.commissionUsdt) }}</td>
                  <td class="number font-mono font-semibold tabular-nums" :class="pnlClass(item.netPnlUsdt)">{{ signedUsd(item.netPnlUsdt) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>
