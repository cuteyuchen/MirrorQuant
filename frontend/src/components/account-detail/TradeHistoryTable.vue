<script setup lang="ts">
import { useI18n } from "@/i18n";
import type { AccountDetailBindingSummary, AccountTradesResponse } from "@/types/api";
import { formatDateTime, formatPct, formatPrice, formatQty, formatUsd, signedUsd } from "@/utils/format";
import { pnlClass, sideLabel, statusLabel, statusTone } from "@/utils/status";
import EmptyState from "@/components/EmptyState.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import DataQualityBadge from "./DataQualityBadge.vue";

const props = defineProps<{
  trades: AccountTradesResponse | null;
  bindings: AccountDetailBindingSummary[];
  filters: { recordType: string; source: string; symbol: string; bindingId: string; cursor: string | null; cursorStack: (string | null)[] };
  loading?: boolean;
}>();

const emit = defineEmits<{ reload: [] }>();
const { t } = useI18n();

function applyFilter() {
  props.filters.cursor = null;
  props.filters.cursorStack = [];
  emit("reload");
}

function nextPage() {
  if (!props.trades?.nextCursor) return;
  props.filters.cursorStack.push(props.filters.cursor);
  props.filters.cursor = props.trades.nextCursor;
  emit("reload");
}

function prevPage() {
  if (!props.filters.cursorStack.length) return;
  props.filters.cursor = props.filters.cursorStack.pop() ?? null;
  emit("reload");
}
</script>

<template>
  <section class="card" aria-labelledby="trades-history-title">
    <header class="card-header">
      <div class="flex flex-wrap items-center gap-2">
        <h2 id="trades-history-title" class="section-title">{{ t("accountDetail.trades.title") }}</h2>
        <DataQualityBadge :quality="trades?.dataQuality" label="Fills" />
        <DataQualityBadge :quality="trades?.ordersQuality" label="Orders" />
      </div>

      <div class="flex flex-wrap items-center gap-2">
        <select v-model="filters.recordType" class="input w-auto py-1 text-xs" @change="applyFilter">
          <option value="">{{ t("accountDetail.trades.type.all") }}</option>
          <option value="fill">{{ t("accountDetail.trades.type.fill") }}</option>
          <option value="execution">{{ t("accountDetail.trades.type.execution") }}</option>
          <option value="order">{{ t("accountDetail.trades.type.order") }}</option>
        </select>

        <select v-model="filters.source" class="input w-auto py-1 text-xs" @change="applyFilter">
          <option value="">{{ t("accountDetail.trades.source.all") }}</option>
          <option value="system">{{ t("accountDetail.trades.source.system") }}</option>
          <option value="manual">{{ t("accountDetail.trades.source.manual") }}</option>
          <option value="unknown">{{ t("accountDetail.trades.source.unknown") }}</option>
        </select>

        <select v-model="filters.bindingId" class="input w-auto py-1 text-xs" @change="applyFilter">
          <option value="">{{ t("accountDetail.trades.allBindings") }}</option>
          <option v-for="b in bindings" :key="b.id" :value="String(b.id)">{{ b.name }}</option>
        </select>

        <input
          v-model="filters.symbol"
          type="text"
          class="input w-32 py-1 text-xs"
          :placeholder="t('accountDetail.cf.symbolPlaceholder')"
          @change="applyFilter"
        />
      </div>
    </header>

    <div class="card-body space-y-3" :aria-busy="loading">
      <EmptyState v-if="!trades?.items?.length" :title="t('accountDetail.trades.empty')" />

      <template v-else>
        <div class="hidden overflow-x-auto md:block">
          <table class="data-table text-xs">
            <thead>
              <tr>
                <th class="text-left">{{ t("accountDetail.cf.colTime") }}</th>
                <th class="text-left">{{ t("accountDetail.trades.colRecord") }}</th>
                <th class="text-left">Symbol</th>
                <th class="number">{{ t("common.quantity") }}</th>
                <th class="number">{{ t("accountDetail.trades.colExec") }}</th>
                <th class="number">{{ t("common.pnl") }}</th>
                <th class="text-left">{{ t("accountDetail.trades.colAttribution") }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in trades.items" :key="`${item.recordType}-${item.id}`">
                <td class="font-mono text-muted tabular-nums">{{ formatDateTime(item.occurredAtMs, { short: true }) }}</td>
                <td>
                  <span class="pill text-[11px]" :class="item.recordType === 'fill' ? 'tone-success' : item.recordType === 'execution' && statusTone(item.status) === 'danger' ? 'tone-danger' : 'tone-neutral'">
                    {{ item.recordType.toUpperCase() }}
                  </span>
                  <StatusBadge class="ml-1" :state="item.status" :label="item.recordType === 'execution' ? statusLabel(item.status) : item.status" />
                </td>
                <td>
                  <strong>{{ item.symbol }}</strong>
                  <span class="ml-1 text-muted">{{ item.side.toUpperCase() }} {{ sideLabel(item.positionSide) }}</span>
                </td>
                <td class="number font-mono tabular-nums">{{ formatQty(item.qty) }}</td>
                <td class="number font-mono tabular-nums">
                  <div>{{ formatPrice(item.price) }}</div>
                  <div class="text-[11px] text-muted">
                    <span v-if="item.fee != null">Fee {{ formatUsd(item.fee) }} {{ item.feeAsset || "" }}</span>
                    <span v-if="item.slippagePct != null"> · Slip {{ formatPct(item.slippagePct) }}</span>
                    <span v-if="item.spreadPct != null"> · Spr {{ formatPct(item.spreadPct) }}</span>
                  </div>
                </td>
                <td class="number font-mono font-semibold tabular-nums" :class="pnlClass(item.realizedPnl)">
                  {{ signedUsd(item.realizedPnl) }}
                </td>
                <td>
                  <span class="pill text-[11px]" :class="item.sourceClass === 'system' ? 'tone-success' : item.sourceClass === 'manual' ? 'tone-warning' : 'tone-neutral'">
                    {{ item.sourceClass }}
                  </span>
                  <span v-if="item.bindingName" class="ml-1.5">{{ item.bindingName }}</span>
                  <div v-if="item.details" class="max-w-xs truncate text-[11px] text-muted" :title="item.details">{{ item.details }}</div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="space-y-2 md:hidden">
          <article v-for="item in trades.items" :key="`m-tr-${item.recordType}-${item.id}`" class="rounded-xl border border-border/50 bg-background/40 p-2.5 text-xs space-y-1">
            <div class="flex items-center justify-between gap-2">
              <div class="flex items-center gap-1.5">
                <strong>{{ item.symbol }}</strong>
                <span class="pill text-[11px] tone-neutral">{{ item.recordType.toUpperCase() }}</span>
              </div>
              <StatusBadge :state="item.status" :label="item.recordType === 'execution' ? statusLabel(item.status) : item.status" />
            </div>
            <div class="flex items-center justify-between font-mono tabular-nums">
              <span>{{ formatQty(item.qty) }} @ {{ formatPrice(item.price) }}</span>
              <span class="font-semibold" :class="pnlClass(item.realizedPnl)">{{ signedUsd(item.realizedPnl) }}</span>
            </div>
            <div class="flex justify-between text-[11px] text-muted">
              <span>{{ item.sourceClass }} {{ item.bindingName ? `· ${item.bindingName}` : "" }}</span>
              <span class="font-mono tabular-nums">{{ formatDateTime(item.occurredAtMs, { short: true }) }}</span>
            </div>
          </article>
        </div>

        <div class="flex items-center justify-end gap-2 pt-2 text-xs">
          <button type="button" class="button-secondary min-h-8 px-2.5 py-1" :disabled="!filters.cursorStack.length" @click="prevPage">{{ t("accountDetail.pagination.prev") }}</button>
          <button type="button" class="button-secondary min-h-8 px-2.5 py-1" :disabled="!trades.hasMore" @click="nextPage">{{ t("accountDetail.pagination.next") }}</button>
        </div>
      </template>
    </div>
  </section>
</template>
