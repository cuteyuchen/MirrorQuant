<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "@/i18n";
import { useDisplayUnit } from "@/composables/useDisplayUnit";
import type { AccountDetailPosition, DatasetQualityInfo } from "@/types/api";
import { formatLeverage, formatPct, formatPrice, formatQty, formatUsd, signedUsd } from "@/utils/format";
import { pnlClass, sideLabel } from "@/utils/status";
import EmptyState from "@/components/EmptyState.vue";
import DataQualityBadge from "./DataQualityBadge.vue";

const props = defineProps<{
  positionsKnown: boolean;
  items: AccountDetailPosition[];
  orphanAllocations: {
    allocationId: number;
    bindingId: number;
    bindingName: string;
    instId: string;
    posSide: string;
    ownedQty: number | null;
  }[];
  dataQuality: DatasetQualityInfo;
}>();

const { t } = useI18n();
const { sizeText } = useDisplayUnit();

const rows = computed(() => props.items ?? []);

function ownershipKey(own: AccountDetailPosition["ownership"]) {
  if (own === "system") return "accountDetail.pos.ownership.system";
  if (own === "manual") return "accountDetail.pos.ownership.manual";
  if (own === "mixed") return "accountDetail.pos.ownership.mixed";
  return "accountDetail.pos.ownership.drift";
}

function unallocKey(cls: AccountDetailPosition["unallocatedClass"]) {
  if (cls === "orphan_risk") return "accountDetail.pos.unalloc.orphan_risk";
  if (cls === "manual") return "accountDetail.pos.unalloc.manual";
  if (cls === "unallocated") return "accountDetail.pos.unalloc.unallocated";
  return "";
}

function protectionKey(st: AccountDetailPosition["protection"]["status"]) {
  if (st === "protected") return "accountDetail.pos.prot.protected";
  if (st === "partial") return "accountDetail.pos.prot.partial";
  if (st === "software_only") return "accountDetail.pos.prot.software_only";
  if (st === "unprotected") return "accountDetail.pos.prot.unprotected";
  return "accountDetail.pos.prot.unknown";
}

function protectionTone(st: AccountDetailPosition["protection"]["status"]) {
  if (st === "protected") return "tone-success";
  if (st === "unprotected") return "tone-danger";
  return "tone-warning";
}
</script>

<template>
  <section class="card" aria-labelledby="positions-panel-title">
    <header class="card-header">
      <div class="flex flex-wrap items-center gap-2">
        <h2 id="positions-panel-title" class="section-title">{{ t("accountDetail.pos.title") }}</h2>
        <span v-if="positionsKnown" class="pill font-mono text-xs tabular-nums">{{ rows.length }}</span>
      </div>
      <DataQualityBadge :quality="dataQuality" />
    </header>

    <div class="card-body space-y-3">
      <div v-if="orphanAllocations?.length" class="rounded-xl border border-warning/40 bg-warning/10 p-3 text-xs text-warning" role="alert">
        <p class="font-semibold">{{ t("accountDetail.pos.orphanAllocWarning", { n: orphanAllocations.length }) }}</p>
        <ul class="mt-1 list-disc pl-4">
          <li v-for="o in orphanAllocations" :key="o.allocationId">
            {{ o.bindingName }} · {{ o.instId }} ({{ sideLabel(o.posSide) }}) · {{ formatQty(o.ownedQty) }}
          </li>
        </ul>
      </div>

      <EmptyState v-if="!positionsKnown" :title="t('accountDetail.pos.unknown')" />
      <EmptyState v-else-if="!rows.length" :title="t('accountDetail.pos.empty')" />

      <template v-else>
        <!-- Desktop horizontal-safe table -->
        <div class="hidden overflow-x-auto md:block">
          <table class="data-table text-xs">
            <thead>
              <tr>
                <th class="text-left">{{ t("accountDetail.pos.colSymbol") }}</th>
                <th class="number">{{ t("accountDetail.pos.colSize") }}</th>
                <th class="number">{{ t("accountDetail.pos.colPrices") }}</th>
                <th class="number">{{ t("accountDetail.pos.colMarginRisk") }}</th>
                <th class="number">{{ t("common.pnl") }}</th>
                <th class="text-left">{{ t("accountDetail.pos.colProtection") }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="pos in rows" :key="`${pos.instId}:${pos.posSide}`">
                <td>
                  <div class="flex flex-wrap items-center gap-1.5">
                    <strong class="text-sm font-bold">{{ pos.symbol }}</strong>
                    <span class="pill text-[11px]" :class="pos.posSide === 'long' ? 'tone-success' : 'tone-danger'">
                      {{ sideLabel(pos.posSide) }}
                    </span>
                    <span class="pill text-[11px]" :class="pos.ownership === 'system' ? 'tone-success' : pos.ownership === 'drift' ? 'tone-danger' : 'tone-warning'">
                      {{ t(ownershipKey(pos.ownership)) }}
                    </span>
                    <span v-if="pos.unallocatedClass !== 'none'" class="pill text-[11px]" :class="pos.unallocatedClass === 'orphan_risk' ? 'tone-danger' : 'tone-warning'">
                      {{ t(unallocKey(pos.unallocatedClass)) }}
                    </span>
                  </div>
                  <div v-if="pos.allocations.length" class="mt-1 flex flex-wrap gap-1.5 text-[11px] text-muted">
                    <span v-for="al in pos.allocations" :key="al.allocationId">
                      {{ al.bindingName }} ({{ formatQty(al.ownedQty) }})
                    </span>
                  </div>
                </td>
                <td class="number font-mono tabular-nums">
                  <div class="font-semibold">{{ sizeText(pos.qty, pos.notionalUsd, pos.curPx) }}</div>
                  <div class="text-[11px] text-muted">Qty {{ formatQty(pos.qty) }} · Unalloc {{ formatQty(pos.unallocatedQty) }}</div>
                </td>
                <td class="number font-mono tabular-nums">
                  <div>{{ formatPrice(pos.avgPx) }} / {{ formatPrice(pos.curPx) }}</div>
                  <div class="text-[11px]" :class="(pos.liqDistancePct ?? 999) < 10 ? 'text-warning font-semibold' : 'text-muted'">
                    Liq {{ formatPrice(pos.liquidationPx) }}
                    <span v-if="pos.liqDistancePct != null">({{ formatPct(pos.liqDistancePct, 1, false) }})</span>
                  </div>
                </td>
                <td class="number font-mono tabular-nums">
                  <div>{{ formatUsd(pos.initialMargin) }} · {{ formatLeverage(pos.lever, 1) }}</div>
                  <div class="text-[11px] text-muted">{{ pos.mgnMode }} · ADL {{ pos.adlQuantile ?? "—" }}</div>
                </td>
                <td class="number font-mono tabular-nums">
                  <div class="font-semibold" :class="pnlClass(pos.upl)">{{ signedUsd(pos.upl) }}</div>
                  <div class="text-[11px]" :class="pnlClass(pos.uplRatioPct)">{{ formatPct(pos.uplRatioPct) }}</div>
                </td>
                <td>
                  <span class="pill text-[11px]" :class="protectionTone(pos.protection.status)">
                    {{ t(protectionKey(pos.protection.status)) }}
                  </span>
                  <div class="mt-1 font-mono text-[11px] text-muted tabular-nums">
                    SL {{ formatPct(pos.protection.slCoveragePct, 0, false) }}
                    <span v-if="pos.protection.nearestSlPrice != null"> @ {{ formatPrice(pos.protection.nearestSlPrice) }}</span>
                    · TP {{ formatPct(pos.protection.tpCoveragePct, 0, false) }}
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- Mobile cards -->
        <div class="space-y-2.5 md:hidden">
          <article v-for="pos in rows" :key="`m-${pos.instId}:${pos.posSide}`" class="rounded-xl border border-border/60 bg-background/40 p-3 text-xs space-y-2">
            <div class="flex items-start justify-between gap-2">
              <div class="flex flex-wrap items-center gap-1.5">
                <strong class="text-sm font-bold">{{ pos.symbol }}</strong>
                <span class="pill text-[11px]" :class="pos.posSide === 'long' ? 'tone-success' : 'tone-danger'">{{ sideLabel(pos.posSide) }}</span>
                <span class="pill text-[11px]" :class="pos.ownership === 'system' ? 'tone-success' : 'tone-warning'">{{ t(ownershipKey(pos.ownership)) }}</span>
              </div>
              <span class="pill text-[11px]" :class="protectionTone(pos.protection.status)">{{ t(protectionKey(pos.protection.status)) }}</span>
            </div>
            <div class="grid grid-cols-2 gap-2 font-mono tabular-nums">
              <div><span class="text-muted">{{ t("accountDetail.pos.colSize") }}: </span>{{ sizeText(pos.qty, pos.notionalUsd, pos.curPx) }}</div>
              <div class="text-right font-semibold" :class="pnlClass(pos.upl)">{{ signedUsd(pos.upl) }}</div>
              <div><span class="text-muted">Liq: </span>{{ formatPrice(pos.liquidationPx) }}</div>
              <div class="text-right"><span class="text-muted">SL: </span>{{ formatPct(pos.protection.slCoveragePct, 0, false) }}</div>
            </div>
          </article>
        </div>
      </template>
    </div>
  </section>
</template>
