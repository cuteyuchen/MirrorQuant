<script setup lang="ts">
import { useDataCacheStore } from "@/stores/dataCache";
import { useI18n } from "@/i18n";
import type { OrderIntent } from "@/types/api";
import { formatDateTime, formatQty } from "@/utils/format";
import { sideLabel } from "@/utils/status";
import DataTable from "@/components/DataTable.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import EnvBadge from "@/components/EnvBadge.vue";
import FreshnessStamp from "@/components/FreshnessStamp.vue";

// 未决订单只展示和刷新，不提供会造成重复成交的重提操作。
withDefaults(defineProps<{ items: OrderIntent[]; loading?: boolean; error?: string }>(), { loading: false, error: "" });
const emit = defineEmits<{ retry: [] }>();
const cache = useDataCacheStore();
const { t } = useI18n();
function rowKey(intent: OrderIntent) {
  return String(intent.id ?? `${intent.bindingId}:${intent.sourceEventId}:${intent.instId}:${intent.createdAt}`);
}
function environment(intent: OrderIntent) {
  return cache.accounts?.data.find(account => account.id === intent.accountId)?.environment
    ?? cache.bindings?.data.items?.find(binding => binding.id === intent.bindingId)?.accountEnvironment ?? null;
}
</script>

<template>
  <DataTable :rows="items" :row-key="rowKey" :loading="loading" :error="error" :empty="t('intents.empty')" :label="t('intents.title')"
    :row-tone="intent => intent.state?.toUpperCase() === 'UNKNOWN' ? 'bg-danger/5' : 'bg-warning/5'" @retry="emit('retry')" :columns="[
      { key: 'state', label: t('common.status'), width: '19%', mobileWide: true },
      { key: 'symbol', label: t('feed.colDirSym'), width: '14%' },
      { key: 'owner', label: t('executions.binding'), width: '21%', mobileWide: true },
      { key: 'quantity', label: t('intents.quantities'), width: '14%', numeric: true },
      { key: 'time', label: t('common.time'), width: '16%', numeric: true },
      { key: 'order', label: t('executions.orderId'), width: '16%', mobileWide: true },
    ]">
    <template #cell-state="{ row }">
      <StatusBadge :state="row.state" />
      <p class="mt-1.5 text-xs text-warning">{{ t(row.state?.toUpperCase() === 'UNKNOWN' ? 'intents.unknownReason' : row.state?.toUpperCase() === 'SUBMITTING' ? 'intents.submittingReason' : 'common.unknown') }}</p>
      <p class="mt-1 text-xs text-muted">{{ t("intents.reduceOnly") }}: {{ t(row.reduceOnly == null ? "common.unknown" : row.reduceOnly ? "common.yes" : "common.no") }}</p>
    </template>
    <template #cell-symbol="{ row }"><strong>{{ row.instId ?? t("common.unknown") }}</strong><p class="mt-1 text-xs text-muted">{{ sideLabel(row.posSide) }}</p></template>
    <template #cell-owner="{ row }">
      <strong>{{ row.bindingName ?? t("common.unknown") }}</strong>
      <div class="mt-1.5 flex flex-wrap items-center gap-2"><span class="text-xs text-muted">{{ row.accountName ?? t("common.unknown") }}</span><EnvBadge :environment="environment(row)" /></div>
    </template>
    <template #cell-quantity="{ row }"><strong>{{ formatQty(row.requestedQty) }}</strong><p class="mt-1 text-xs text-muted">{{ t("intents.filled") }} {{ formatQty(row.filledQty) }}</p></template>
    <template #cell-time="{ row }"><span class="text-xs tabular-nums">{{ formatDateTime(row.createdAt) }}</span><p class="mt-1"><FreshnessStamp :timestamp="row.createdAt" :warn-after="30" :critical-after="120" /></p></template>
    <template #cell-order="{ row }">
      <p class="break-all font-mono text-xs">{{ row.exchangeOrderId ?? t("common.unknown") }}</p>
      <p class="mt-1 break-all text-xs text-muted">{{ t("intents.source") }}: {{ row.sourceEventId ?? t("common.unknown") }}</p>
      <p class="mt-1 break-all text-xs text-muted">{{ t("intents.id") }}: {{ row.id ?? t("common.unknown") }}</p>
    </template>
    <template #mobile-card="{ row }">
      <div class="space-y-2.5">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div class="flex min-w-0 flex-wrap items-center gap-1.5">
            <strong class="text-sm">{{ row.instId ?? t("common.unknown") }}</strong>
            <span class="text-xs text-muted">{{ sideLabel(row.posSide) }}</span>
          </div>
          <StatusBadge :state="row.state" />
        </div>
        <div class="flex flex-wrap items-center gap-1.5 text-xs text-muted">
          <span class="font-medium text-foreground">{{ row.bindingName ?? t("common.unknown") }}</span>
          <span>·</span>
          <span>{{ row.accountName ?? t("common.unknown") }}</span>
          <EnvBadge :environment="environment(row)" />
          <FreshnessStamp class="ml-auto" :timestamp="row.createdAt" :warn-after="30" :critical-after="120" />
        </div>
        <div class="grid grid-cols-2 gap-3 rounded-lg bg-background/40 px-2.5 py-2 text-xs">
          <div>
            <span class="text-muted">{{ t("intents.quantities") }}</span>
            <p class="mt-0.5 font-mono font-semibold tabular-nums">{{ formatQty(row.requestedQty) }} / {{ formatQty(row.filledQty) }}</p>
          </div>
          <div class="text-right">
            <span class="text-muted">{{ t("intents.reduceOnly") }}</span>
            <p class="mt-0.5 font-semibold">{{ t(row.reduceOnly == null ? "common.unknown" : row.reduceOnly ? "common.yes" : "common.no") }}</p>
          </div>
        </div>
        <p class="text-xs text-warning">{{ t(row.state?.toUpperCase() === 'UNKNOWN' ? 'intents.unknownReason' : row.state?.toUpperCase() === 'SUBMITTING' ? 'intents.submittingReason' : 'common.unknown') }}</p>
        <p class="truncate font-mono text-[11px] text-muted">{{ row.exchangeOrderId ?? row.id ?? t("common.unknown") }}</p>
      </div>
    </template>
  </DataTable>
</template>
