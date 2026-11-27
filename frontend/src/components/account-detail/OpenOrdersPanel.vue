<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "@/i18n";
import type { AccountDetailOpenOrder, AccountOrdersResponse } from "@/types/api";
import { formatDateTime, formatPrice, formatQty } from "@/utils/format";
import { sideLabel } from "@/utils/status";
import EmptyState from "@/components/EmptyState.vue";
import DataQualityBadge from "./DataQualityBadge.vue";

const props = defineProps<{
  orders: AccountOrdersResponse | null;
  category: string;
  loading?: boolean;
}>();

const emit = defineEmits<{ "update:category": [val: string] }>();
const { t } = useI18n();

const rows = computed(() => (props.orders?.items ?? []) as AccountDetailOpenOrder[]);

const catLabels: Record<string, string> = {
  "": "accountDetail.openOrders.cat.all",
  ordinary: "accountDetail.openOrders.cat.ordinary",
  conditional: "accountDetail.openOrders.cat.conditional",
  take_profit: "accountDetail.openOrders.cat.take_profit",
  stop_loss: "accountDetail.openOrders.cat.stop_loss",
  trailing_stop: "accountDetail.openOrders.cat.trailing_stop",
};
</script>

<template>
  <section class="card" aria-labelledby="open-orders-title">
    <header class="card-header">
      <div class="flex flex-wrap items-center gap-2">
        <h2 id="open-orders-title" class="section-title">{{ t("accountDetail.openOrders.title") }}</h2>
        <span v-if="orders?.openOrdersKnown || orders?.openOrdersStale" class="pill font-mono text-xs tabular-nums">{{ rows.length }}</span>
        <DataQualityBadge :quality="orders?.dataQuality" />
      </div>

      <select
        :value="category"
        class="input w-auto py-1 text-xs"
        :aria-label="t('accountDetail.openOrders.title')"
        @change="emit('update:category', ($event.target as HTMLSelectElement).value)"
      >
        <option v-for="(lblKey, key) in catLabels" :key="key" :value="key">{{ t(lblKey) }}</option>
      </select>
    </header>

    <div class="card-body space-y-3" :aria-busy="loading">
      <p v-if="orders?.openOrdersStale" class="rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
        {{ t("accountDetail.openOrders.staleNotice") }}
      </p>

      <EmptyState v-if="!orders || (!orders.openOrdersKnown && !orders.openOrdersStale)" :title="t('accountDetail.openOrders.unavailable')" />
      <EmptyState v-else-if="!rows.length" :title="t('accountDetail.openOrders.emptyVerified')" />

      <div v-else class="overflow-x-auto">
        <table class="data-table text-xs">
          <thead>
            <tr>
              <th class="text-left">Symbol</th>
              <th class="text-left">{{ t("accountDetail.openOrders.colType") }}</th>
              <th class="number">{{ t("accountDetail.openOrders.colPrice") }}</th>
              <th class="number">{{ t("common.quantity") }}</th>
              <th class="text-left">{{ t("accountDetail.openOrders.colFlags") }}</th>
              <th class="text-left">{{ t("accountDetail.cf.colTime") }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="o in rows" :key="`${o.orderId}:${o.algoId}:${o.category}`">
              <td class="font-semibold">{{ o.symbol }}</td>
              <td>
                <span class="pill text-[11px] tone-neutral">{{ t(catLabels[o.category] ?? "accountDetail.openOrders.cat.ordinary") }}</span>
                <span class="ml-1.5">{{ o.side.toUpperCase() }} / {{ sideLabel(o.positionSide) }}</span>
              </td>
              <td class="number font-mono tabular-nums">
                <div>{{ formatPrice(o.limitPrice) }}</div>
                <div v-if="o.triggerPrice != null" class="text-[11px] text-warning">Trig {{ formatPrice(o.triggerPrice) }}</div>
              </td>
              <td class="number font-mono tabular-nums">{{ formatQty(o.qty) }}</td>
              <td>
                <span v-if="o.closePosition" class="pill tone-warning text-[11px]">ClosePos</span>
                <span v-else-if="o.reduceOnly" class="pill tone-neutral text-[11px]">ReduceOnly</span>
                <span class="ml-1.5 text-muted">{{ o.bindingName || o.sourceClass }}</span>
              </td>
              <td class="font-mono text-muted tabular-nums">{{ formatDateTime(o.updatedAtMs ?? o.createdAtMs, { short: true }) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>
