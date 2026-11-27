<script setup lang="ts">
import { useI18n } from "@/i18n";
import type { AccountCashflowsResponse } from "@/types/api";
import { formatDateTime, formatQty, signedUsd } from "@/utils/format";
import { pnlClass } from "@/utils/status";
import EmptyState from "@/components/EmptyState.vue";
import DataQualityBadge from "./DataQualityBadge.vue";

const props = defineProps<{
  cashflows: AccountCashflowsResponse | null;
  filters: { category: string; asset: string; symbol: string; cursor: string | null; cursorStack: (string | null)[] };
  loading?: boolean;
}>();

const emit = defineEmits<{ reload: [] }>();
const { t } = useI18n();

const CATEGORIES = [
  "TRANSFER_IN", "TRANSFER_OUT", "REALIZED_PNL", "FUNDING_FEE", "SPECIAL_FUNDING_FEE",
  "COMMISSION", "COMMISSION_REBATE", "LIQUIDATION_FEE", "INSURANCE_CLEAR", "BONUS",
  "REFERRAL_REBATE", "OTHER_TRADING_INCOME", "OTHER_EXTERNAL_FLOW", "UNKNOWN",
];

const catKeyMap: Record<string, string> = {
  TRANSFER_IN: "accountDetail.cf.cat.TRANSFER_IN",
  TRANSFER_OUT: "accountDetail.cf.cat.TRANSFER_OUT",
  REALIZED_PNL: "accountDetail.cf.cat.REALIZED_PNL",
  FUNDING_FEE: "accountDetail.cf.cat.FUNDING_FEE",
  SPECIAL_FUNDING_FEE: "accountDetail.cf.cat.SPECIAL_FUNDING_FEE",
  COMMISSION: "accountDetail.cf.cat.COMMISSION",
  COMMISSION_REBATE: "accountDetail.cf.cat.COMMISSION_REBATE",
  LIQUIDATION_FEE: "accountDetail.cf.cat.LIQUIDATION_FEE",
  INSURANCE_CLEAR: "accountDetail.cf.cat.INSURANCE_CLEAR",
  BONUS: "accountDetail.cf.cat.BONUS",
  REFERRAL_REBATE: "accountDetail.cf.cat.REFERRAL_REBATE",
  OTHER_TRADING_INCOME: "accountDetail.cf.cat.OTHER_TRADING_INCOME",
  OTHER_EXTERNAL_FLOW: "accountDetail.cf.cat.OTHER_EXTERNAL_FLOW",
  UNKNOWN: "accountDetail.cf.cat.UNKNOWN",
};

function applyFilter() {
  props.filters.cursor = null;
  props.filters.cursorStack = [];
  emit("reload");
}

function nextPage() {
  if (!props.cashflows?.nextCursor) return;
  props.filters.cursorStack.push(props.filters.cursor);
  props.filters.cursor = props.cashflows.nextCursor;
  emit("reload");
}

function prevPage() {
  if (!props.filters.cursorStack.length) return;
  props.filters.cursor = props.filters.cursorStack.pop() ?? null;
  emit("reload");
}
</script>

<template>
  <section class="card" aria-labelledby="cashflow-table-title">
    <header class="card-header">
      <div class="flex flex-wrap items-center gap-2">
        <h2 id="cashflow-table-title" class="section-title">{{ t("accountDetail.cf.title") }}</h2>
        <DataQualityBadge :quality="cashflows?.dataQuality" />
      </div>

      <div class="flex flex-wrap items-center gap-2">
        <select v-model="filters.category" class="input w-auto py-1 text-xs" @change="applyFilter">
          <option value="">{{ t("accountDetail.cf.allCategories") }}</option>
          <option v-for="c in CATEGORIES" :key="c" :value="c">{{ t(catKeyMap[c]) }}</option>
        </select>
        <select v-model="filters.asset" class="input w-auto py-1 text-xs" @change="applyFilter">
          <option value="">{{ t("accountDetail.cf.allAssets") }}</option>
          <option value="USDT">USDT</option>
          <option value="USDC">USDC</option>
          <option value="BNB">BNB</option>
        </select>
        <input
          v-model="filters.symbol"
          type="text"
          class="input w-36 py-1 text-xs"
          :placeholder="t('accountDetail.cf.symbolPlaceholder')"
          @change="applyFilter"
        />
      </div>
    </header>

    <div class="card-body space-y-3" :aria-busy="loading">
      <EmptyState v-if="!cashflows?.items?.length" :title="t('accountDetail.cf.empty')" />

      <template v-else>
        <div class="hidden overflow-x-auto md:block">
          <table class="data-table text-xs">
            <thead>
              <tr>
                <th class="text-left">{{ t("accountDetail.cf.colTime") }}</th>
                <th class="text-left">{{ t("accountDetail.cf.colCategory") }}</th>
                <th class="text-left">Symbol</th>
                <th class="text-left">{{ t("accountDetail.cf.colAsset") }}</th>
                <th class="number">{{ t("accountDetail.cf.colAmount") }}</th>
                <th class="text-left">{{ t("accountDetail.cf.colExternalId") }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in cashflows.items" :key="item.id">
                <td class="font-mono text-muted tabular-nums">{{ formatDateTime(item.occurredAtMs, { short: true }) }}</td>
                <td>
                  <span class="pill text-[11px] tone-neutral">{{ t(catKeyMap[item.category] ?? "accountDetail.cf.cat.UNKNOWN") }}</span>
                  <span class="ml-1.5 text-[11px] text-muted">{{ item.rawType }}</span>
                </td>
                <td>{{ item.symbol || "—" }}</td>
                <td class="font-semibold">{{ item.asset }}</td>
                <td class="number font-mono font-semibold tabular-nums" :class="pnlClass(item.signedAmount)">
                  {{ item.asset === "USDT" ? signedUsd(item.signedAmount) : formatQty(item.signedAmount) }}
                </td>
                <td class="font-mono text-[11px] text-muted">{{ item.externalId || item.source }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="space-y-2 md:hidden">
          <article v-for="item in cashflows.items" :key="`m-cf-${item.id}`" class="rounded-xl border border-border/50 bg-background/40 p-2.5 text-xs">
            <div class="flex items-center justify-between">
              <span class="pill text-[11px] tone-neutral">{{ t(catKeyMap[item.category] ?? "accountDetail.cf.cat.UNKNOWN") }}</span>
              <span class="font-mono font-semibold tabular-nums" :class="pnlClass(item.signedAmount)">
                {{ item.asset === "USDT" ? signedUsd(item.signedAmount) : `${formatQty(item.signedAmount)} ${item.asset}` }}
              </span>
            </div>
            <div class="mt-1 flex justify-between text-[11px] text-muted">
              <span>{{ item.symbol || item.asset }}</span>
              <span class="font-mono tabular-nums">{{ formatDateTime(item.occurredAtMs, { short: true }) }}</span>
            </div>
          </article>
        </div>

        <div class="flex items-center justify-end gap-2 pt-2 text-xs">
          <button type="button" class="button-secondary min-h-8 px-2.5 py-1" :disabled="!filters.cursorStack.length" @click="prevPage">{{ t("accountDetail.pagination.prev") }}</button>
          <button type="button" class="button-secondary min-h-8 px-2.5 py-1" :disabled="!cashflows.hasMore" @click="nextPage">{{ t("accountDetail.pagination.next") }}</button>
        </div>
      </template>
    </div>
  </section>
</template>
