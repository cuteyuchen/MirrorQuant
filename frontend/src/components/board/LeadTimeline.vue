<script setup lang="ts">
import { computed, ref } from "vue";
import { ChevronDown, ChevronUp } from "lucide-vue-next";
import type { BoardAccount, BoardEvent, BoardPosition } from "@/types/api";
import { useI18n } from "@/i18n";
import { formatClock, formatDateTime, formatPct, formatPrice, formatQty, signedUsd } from "@/utils/format";
import { actionLabel, normalizeSide, pnlClass, sideLabel } from "@/utils/status";
import { reasonText } from "@/utils/errors";
import StatusBadge from "@/components/StatusBadge.vue";
import EnvBadge from "@/components/EnvBadge.vue";
const props = withDefaults(defineProps<{
  events: BoardEvent[] | null; positions: BoardPosition[] | null; accounts: BoardAccount[] | null; readOnly?: boolean;
}>(), { readOnly: false });
const { t } = useI18n();
// 默认只展开最近几条，卡片高度不被一整天的动态撑开；其余一键展开。
const PREVIEW = 6;
const expanded = ref(false);
const sorted = computed(() => [...(props.events || [])].sort((a, b) => (b.time ?? -Infinity) - (a.time ?? -Infinity)));
const visible = computed(() => expanded.value ? sorted.value : sorted.value.slice(0, PREVIEW));
function environment(bindingId: number | null, accountName: string | null) {
  const binding = props.positions?.flatMap(position => position.mine || []).find(mine => mine.bindingId === bindingId);
  return binding?.environment ?? props.accounts?.find(account => account.name === accountName)?.environment ?? null;
}
// 开 / 加 / 减 / 平各用一种色调，扫一眼就能分出今天的动作类型。
function actionTone(action: string | null) {
  const key = action?.toUpperCase();
  return key === "OPEN" ? "tone-accent" : key === "ADD" ? "tone-brand" : key === "REDUCE" ? "tone-warning" : "tone-neutral";
}
function sideClass(side: string | null) {
  const normalized = normalizeSide(side);
  return normalized === "LONG" ? "side-long" : normalized === "SHORT" ? "side-short" : "side-unknown";
}
// 每条带单事件紧挨自己的执行结果，保留失败和跳过的详情。
</script>
<template>
  <aside class="min-w-0">
    <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
      <h3 class="eyebrow">{{ t("board.timeline") }}</h3>
      <span v-if="sorted.length" class="pill font-mono tabular-nums">{{ sorted.length }}</span>
    </div>
    <p v-if="readOnly" class="mb-3 text-xs text-muted">{{ t("pub.knownEvents") }}</p>
    <p v-if="!sorted.length" class="rounded-xl border border-dashed border-border/60 px-3 py-6 text-center text-xs text-muted">{{ events == null ? t("common.unknown") : t("board.noEvents") }}</p>
    <ol v-else class="relative space-y-4 before:absolute before:bottom-2 before:left-[5px] before:top-2 before:w-px before:bg-border/70">
      <li v-for="event in visible" :key="event.id || `${event.time}:${event.symbol}:${event.action}`" class="relative min-w-0 pl-6">
        <span class="absolute left-0 top-1.5 h-[11px] w-[11px] rounded-full border-2 border-surface bg-accent" aria-hidden="true" />
        <div class="flex flex-wrap items-center justify-between gap-x-2 gap-y-1">
          <span class="flex min-w-0 flex-wrap items-center gap-1.5">
            <span class="pill font-semibold" :class="actionTone(event.action)">{{ actionLabel(event.action) }}</span>
            <span class="font-semibold">{{ event.symbol ?? t("common.unknown") }}</span>
            <span :class="sideClass(event.side)">{{ sideLabel(event.side) }}</span>
          </span>
          <time class="font-mono text-xs tabular-nums text-muted" :title="formatDateTime(event.time)">{{ formatClock(event.time) }}</time>
        </div>
        <p class="mt-1 font-mono text-xs tabular-nums text-muted">
          {{ formatQty(event.qty) }} @ {{ formatPrice(event.price) }}
          <span v-if="event.realizedPnl != null" class="ml-1 font-semibold" :class="pnlClass(event.realizedPnl)">{{ signedUsd(event.realizedPnl) }}</span>
        </p>
        <p v-if="!readOnly && !event.mine?.length" class="mt-1.5 text-xs font-medium text-warning">{{ t("board.noExecution") }}</p>
        <!-- 我的执行结果只在私有看板出现，公开页不渲染账户与执行详情。 -->
        <template v-if="!readOnly">
          <div v-for="execution in event.mine || []" :key="execution.bindingId || execution.accountName || 'execution'" class="mt-2 min-w-0 rounded-lg border border-border/50 bg-background/50 p-2 text-xs">
            <div class="flex flex-wrap items-center gap-1.5">
              <span class="min-w-0 font-medium [overflow-wrap:anywhere]">{{ execution.accountName ?? t("common.unknown") }}</span>
              <EnvBadge :environment="environment(execution.bindingId, execution.accountName)" />
              <StatusBadge class="ml-auto" :state="execution.status" />
            </div>
            <p class="mt-1 font-mono tabular-nums text-muted">{{ formatQty(execution.qty) }} @ {{ formatPrice(execution.price) }} · {{ t("board.slippage") }} {{ formatPct(execution.slippagePct) }}</p>
            <p v-if="execution.details" class="mt-1 break-words font-medium text-warning">{{ reasonText(execution.details) }}</p>
          </div>
        </template>
      </li>
    </ol>
    <button v-if="sorted.length > PREVIEW" type="button" class="button-ghost mt-2 w-full text-xs" :aria-expanded="expanded" @click="expanded = !expanded">
      <component :is="expanded ? ChevronUp : ChevronDown" :size="15" aria-hidden="true" />
      {{ expanded ? t("feed.showLess") : t("feed.showMore", { shown: visible.length, total: sorted.length }) }}
    </button>
  </aside>
</template>
