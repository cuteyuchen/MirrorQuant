<script setup lang="ts">
import { computed } from "vue";
import { Info } from "lucide-vue-next";
import type { BoardMine } from "@/types/api";
import { useI18n } from "@/i18n";
import { useDisplayUnit } from "@/composables/useDisplayUnit";
import { formatPrice, signedUsd } from "@/utils/format";
import { pnlClass, statusTone } from "@/utils/status";
import { reasonText } from "@/utils/errors";
import EnvBadge from "@/components/EnvBadge.vue";
import StatusBadge from "@/components/StatusBadge.vue";

// 一个跟单账户一张小卡：账户、环境、跟单状态与我的数量 / 均价 / 盈亏放在同一视线内，
// 不需要在“我的持仓”和“状态”两列之间来回对照。
const props = defineProps<{ mine: BoardMine | null }>();
const { t } = useI18n();
const { unit, sizeText } = useDisplayUnit();
const tone = computed(() => statusTone(props.mine?.state));
// 没跟上时直接摆出“我的量 / 应有量”，差多少一眼可见。
const showExpected = computed(() => props.mine != null && props.mine.state !== "aligned" && props.mine.expectedQty != null);
const qtyLabel = computed(() => showExpected.value ? t("board.partialQty") : unit.value === "notional" ? t("board.notional") : unit.value === "quantity" ? t("common.quantity") : `${t("common.quantity")} / ${t("board.notional")}`);
// 后端给了原因就原样翻译；未跟或被拦截却没有原因时明确写“原因未知”，不留空白。
const reason = computed(() => {
  const mine = props.mine;
  if (!mine) return "";
  if (mine.reason) return reasonText(mine.reason);
  return mine.state === "missing" || mine.state === "blocked" ? t("board.reasonUnknown") : "";
});
</script>

<template>
  <!-- 没有对应绑定或跟单数据：虚线占位，状态仍明确标成未知。 -->
  <div v-if="!mine" class="flex min-w-0 flex-wrap items-center gap-2 rounded-xl border border-dashed border-border/70 p-2.5 sm:p-3 text-xs text-muted">
    <StatusBadge state="unknown" />
    <span class="min-w-0 flex-1">{{ t("board.noMine") }}</span>
  </div>
  <div
    v-else
    class="min-w-0 rounded-xl border p-2.5 sm:p-3"
    :class="tone === 'danger' ? 'border-danger/40 bg-danger/5' : tone === 'success' ? 'border-border/40 bg-surface/50' : 'border-warning/40 bg-warning/5'"
  >
    <!-- 顶行：账户名称、环境、跟单状态徽标；已跟上时视觉更安静，异常状态更突出。 -->
    <div class="flex flex-wrap items-center justify-between gap-1.5">
      <div class="flex min-w-0 flex-wrap items-center gap-1.5">
        <RouterLink v-if="mine.accountId != null" :to="`/accounts/${mine.accountId}`"
          class="min-w-0 text-sm font-semibold hover:text-accent hover:underline [overflow-wrap:anywhere]">{{ mine.accountName ?? t("common.unknown") }}</RouterLink>
        <span v-else class="min-w-0 text-sm font-semibold [overflow-wrap:anywhere]">{{ mine.accountName ?? t("common.unknown") }}</span>
        <EnvBadge :environment="mine.environment" />
      </div>
      <div class="flex shrink-0 items-center gap-2">
        <span class="font-mono text-xs font-semibold sm:hidden" :class="pnlClass(mine.pnl)">{{ signedUsd(mine.pnl) }}</span>
        <StatusBadge :state="mine.state" />
      </div>
    </div>
    <!-- 移动端紧凑一行呈现均价与数量摘要 -->
    <div class="mt-1 flex flex-wrap items-center justify-between gap-x-3 gap-y-1 text-xs sm:hidden">
      <span class="text-muted">{{ qtyLabel }}: <strong class="font-mono text-foreground tabular-nums"><template v-if="showExpected">{{ sizeText(mine.qty, null, mine.entryPrice) }} / {{ sizeText(mine.expectedQty, null, mine.entryPrice) }}</template><template v-else>{{ sizeText(mine.qty, null, mine.entryPrice) }}</template></strong></span>
      <span class="text-muted">{{ t("board.entry") }}: <strong class="font-mono text-foreground tabular-nums">{{ formatPrice(mine.entryPrice) }}</strong></span>
    </div>
    <!-- 桌面端保留 3 列指标 -->
    <dl class="mt-2 hidden grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_minmax(0,0.9fr)] gap-2 text-xs sm:grid">
      <div class="min-w-0">
        <dt class="text-muted">{{ qtyLabel }}</dt>
        <dd class="mt-0.5 font-mono font-semibold tabular-nums" :class="unit === 'both' ? '' : 'whitespace-nowrap'">
          <template v-if="showExpected">{{ sizeText(mine.qty, null, mine.entryPrice) }} / {{ sizeText(mine.expectedQty, null, mine.entryPrice) }}</template>
          <template v-else>{{ sizeText(mine.qty, null, mine.entryPrice) }}</template>
        </dd>
      </div>
      <div class="min-w-0">
        <dt class="text-muted">{{ t("board.entry") }}</dt>
        <dd class="mt-0.5 whitespace-nowrap font-mono font-semibold tabular-nums">{{ formatPrice(mine.entryPrice) }}</dd>
      </div>
      <div class="min-w-0 text-right">
        <dt class="text-muted">{{ t("common.pnl") }}</dt>
        <dd class="mt-0.5 whitespace-nowrap font-mono font-semibold tabular-nums" :class="pnlClass(mine.pnl)">{{ signedUsd(mine.pnl) }}</dd>
      </div>
    </dl>
    <p v-if="reason" class="mt-2 flex items-start gap-1.5 text-xs font-medium" :class="tone === 'danger' ? 'text-danger' : 'text-warning'">
      <Info class="mt-0.5 shrink-0" :size="13" aria-hidden="true" /><span class="min-w-0">{{ reason }}</span>
    </p>
  </div>
</template>
