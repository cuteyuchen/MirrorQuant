<script setup lang="ts">
import { computed, ref } from "vue";
import { AlertTriangle, ArrowRight, Calculator, ChevronDown, ChevronUp } from "lucide-vue-next";
import type { BoardAccount, BoardLead, BoardPosition } from "@/types/api";
import { useI18n } from "@/i18n";
import { useDisplayUnit } from "@/composables/useDisplayUnit";
import { finiteNumber, formatDateTime, formatPct, formatPrice, formatQty, formatUsd, signedUsd, type NumericValue } from "@/utils/format";
import { normalizeSide, pnlClass, sideLabel, statusTone } from "@/utils/status";
import { reasonText } from "@/utils/errors";
import { boardPositionsConfirmed } from "@/utils/positions";
import FreshnessStamp from "@/components/FreshnessStamp.vue";
import EnvBadge from "@/components/EnvBadge.vue";
import EmptyState from "@/components/EmptyState.vue";
import LeadAvatar from "@/components/LeadAvatar.vue";
import LevelTrack from "./LevelTrack.vue";
import MineCard from "./MineCard.vue";
import LeadTimeline from "./LeadTimeline.vue";
import LeadCalculator from "./LeadCalculator.vue";

const props = withDefaults(defineProps<{
  lead: BoardLead; receivedAt: number | null; accounts: BoardAccount[] | null;
  readOnly?: boolean; showDetails?: boolean; defaultCollapsed?: boolean;
}>(), { readOnly: false, showDetails: true, defaultCollapsed: false });
const { t } = useI18n();
const { showQuantity, showNotional } = useDisplayUnit();
// 看板里就地测算跟单仓位，不必跳到详情页；未展开时不请求快照。
const calcOpen = ref(false);
const collapsed = ref(props.defaultCollapsed);
// 打开测算器时同时展开折叠的卡片，避免测算结果被藏起来。
function toggleCalc() { calcOpen.value = !calcOpen.value; if (calcOpen.value) collapsed.value = false; }
// 移动端折叠次要持仓明细（均价/数量/名义价值/止盈止损），默认紧凑收起、可展开。
const expandedDetails = ref<Record<string, boolean>>({});
function toggleDetails(key: string) { expandedDetails.value[key] = !expandedDetails.value[key]; }
function isDetailsExpanded(key: string) { return !!expandedDetails.value[key]; }
// 今日新仓优先；未知开仓时间放在最后，不改变后端持仓状态。
const positions = computed(() => [...(props.lead.positions || [])].sort((a, b) =>
  Number(b.openedToday === true) - Number(a.openedToday === true) || (b.openedAt ?? -Infinity) - (a.openedAt ?? -Infinity)));
function positionKey(position: BoardPosition) { return position.key || `${position.symbol}:${position.side}:${position.openedAt}`; }
// 未绑定时仅做市场监控，不在每个持仓下重复显示“尚未绑定”；有绑定时显示各跟单账户对齐情况。
const isUnbound = computed(() => !props.readOnly && props.lead.boundCount === 0);
const showMineSection = computed(() => !props.readOnly && !isUnbound.value);
const showTimeline = computed(() => props.readOnly || (props.lead.todayEvents?.length ?? 0) > 0);
const idleSummary = computed(() => {
  if (!boardPositionsConfirmed(props.lead)) return t("board.unknownSummary");
  return t("board.idleSummary", { open: props.lead.todayOpenCount ?? 0, close: props.lead.todayCloseCount ?? 0 });
});
function mineRows(position: BoardPosition) {
  if (props.readOnly || !position.mine?.length) return [null];
  return [...position.mine].sort((a, b) => Number(b.bindingEnabled !== false) - Number(a.bindingEnabled !== false));
}
// 没有任何绑定跟这位带单员时只是“未绑定”，不算异常；有绑定但没跟齐才需要关注。
function attention(position: BoardPosition) { return !props.readOnly && !!position.mine?.some(mine => mine.state !== "aligned"); }
// 持仓卡的底色跟随“我跟得怎么样”：被拦截最醒目，其次是没跟齐，跟上了保持安静。
function cardTone(position: BoardPosition) {
  if (!props.readOnly && position.mine?.some(mine => statusTone(mine.state) === "danger")) return "border-danger/40 bg-danger/[0.05]";
  if (attention(position)) return "border-warning/40 bg-warning/[0.05]";
  return "border-border/50 bg-background/40";
}
function sideClass(side?: string | null, qty?: NumericValue) {
  const normalized = normalizeSide(side, qty);
  return normalized === "LONG" ? "side-long" : normalized === "SHORT" ? "side-short" : "side-unknown";
}
// 止盈 / 止损价距离现价多少个百分点；任一价格未知就返回未知，不用零顶替。
function distancePct(level: NumericValue, mark: NumericValue) {
  const price = finiteNumber(level);
  const base = finiteNumber(mark);
  return price == null || base == null || base === 0 ? null : (price - base) / base * 100;
}
// 止盈或止损至少有一项已知时才展示；全部未知时隐藏，不在移动端留出一整块破折号占位。
function showLevels(position: BoardPosition) {
  return position.takeProfitPrice != null || position.stopLossPrice != null;
}
// 公开页的“今日开 / 平 / 已实现”没有可靠来源时不渲染，避免一排破折号。
const showToday = computed(() => !props.readOnly
  || props.lead.todayOpenCount != null || props.lead.todayCloseCount != null || props.lead.todayRealizedPnl != null);
</script>
<template>
  <section class="card card-accent" :data-testid="`lead-${lead.id}`" :class="readOnly ? 'public-comparison' : ''">
    <header class="card-header">
      <div class="flex min-w-0 flex-1 items-center gap-3">
        <LeadAvatar :name="lead.name" :url="lead.avatarUrl" :size="42" />
        <div class="min-w-0 flex-1">
          <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
            <h2 class="text-base font-bold tracking-tight sm:text-lg">{{ lead.name ?? t("common.unknown") }}</h2>
            <span v-if="isUnbound" class="pill border-border/60 bg-surface2 text-muted">{{ t("board.watchOnly") }}</span>
            <RouterLink v-if="isUnbound && showDetails" to="/bindings" class="inline-flex min-h-7 items-center gap-1 rounded-md px-1.5 text-xs font-semibold text-accent transition hover:bg-accent/10">{{ t("board.bindNow") }}<ArrowRight :size="13" aria-hidden="true" /></RouterLink>
            <RouterLink v-if="!readOnly && showDetails && lead.id != null" :to="`/leads/${lead.id}`" class="inline-flex min-h-7 items-center gap-1 rounded-md px-1.5 text-xs font-semibold text-accent transition hover:bg-accent/10">{{ t("common.details") }}<ArrowRight :size="13" aria-hidden="true" /></RouterLink>
            <button v-if="!readOnly && showDetails && lead.id != null" type="button" data-testid="calc-toggle" class="inline-flex min-h-7 items-center gap-1 rounded-md px-1.5 text-xs font-semibold text-accent transition hover:bg-accent/10" :aria-expanded="calcOpen" @click="toggleCalc"><Calculator :size="13" aria-hidden="true" />{{ t(calcOpen ? "board.calcClose" : "board.calcOpen") }}</button>
          </div>
          <!-- 折叠空仓/停用带单员时展示一行安静摘要；展开时展示今日关键开平与已实现盈亏。 -->
          <div v-if="collapsed" class="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted">
            <span>{{ idleSummary }}</span>
            <span v-if="lead.todayRealizedPnl" class="font-mono font-semibold tabular-nums" :class="pnlClass(lead.todayRealizedPnl)">{{ signedUsd(lead.todayRealizedPnl) }}</span>
          </div>
          <div v-else-if="showToday" class="mt-1.5 flex flex-wrap items-center gap-1.5 text-xs">
            <span class="pill" :class="(lead.todayOpenCount ?? 0) > 0 ? 'tone-accent font-semibold' : 'text-muted'">{{ t("board.todayOpen", { n: lead.todayOpenCount ?? "—" }) }}</span>
            <span class="pill" :class="(lead.todayCloseCount ?? 0) > 0 ? 'font-medium text-foreground' : 'text-muted'">{{ t("board.todayClose", { n: lead.todayCloseCount ?? "—" }) }}</span>
            <span class="pill" :class="lead.todayRealizedPnl ? '' : 'text-muted'">{{ t("board.realized") }} <strong class="font-mono tabular-nums" :class="lead.todayRealizedPnl ? pnlClass(lead.todayRealizedPnl) : 'text-muted'">{{ signedUsd(lead.todayRealizedPnl) }}</strong></span>
            <span v-if="!readOnly && !(lead.todayEvents?.length)" class="pill text-muted">{{ t("board.noEventsShort") }}</span>
          </div>
        </div>
      </div>
      <div class="flex shrink-0 items-center gap-2 text-xs text-muted">
        <span v-if="lead.stale === false" class="live-dot text-success" aria-hidden="true" />
        <span class="hidden sm:inline">{{ t("board.snapshot") }}</span><FreshnessStamp :seconds="lead.snapshotAgeSec" :stale="lead.stale" :received-at="receivedAt" />
        <button v-if="!readOnly" type="button" class="inline-flex min-h-8 items-center gap-1 rounded-lg border border-border/50 px-2 text-xs font-medium text-muted transition hover:bg-surface2 hover:text-foreground" :aria-expanded="!collapsed" @click="collapsed = !collapsed">
          <component :is="collapsed ? ChevronDown : ChevronUp" :size="14" aria-hidden="true" />
          <span class="hidden sm:inline">{{ t(collapsed ? "board.expandLead" : "board.collapseLead") }}</span>
        </button>
      </div>
    </header>
    <div v-if="!collapsed" class="card-body">
      <!-- 残留仓是需要立刻处理的风险，放在所有持仓之前。 -->
      <div v-if="!readOnly && lead.orphans?.length" class="mb-4 rounded-xl border border-danger/40 bg-danger/10 p-3 sm:p-4" data-testid="orphans" role="alert">
        <h3 class="flex items-center gap-2 text-sm font-bold text-danger"><AlertTriangle :size="17" aria-hidden="true" />{{ t("board.orphans") }}</h3>
        <div v-for="orphan in lead.orphans" :key="`${orphan.bindingId}:${orphan.symbol}:${orphan.side}`"
          class="mt-2.5 grid min-w-0 items-center gap-x-4 gap-y-2 rounded-lg border border-danger/25 bg-surface/80 p-2.5 md:grid-cols-[minmax(0,1.3fr)_minmax(0,1.5fr)_minmax(0,1.2fr)]">
          <div class="flex min-w-0 flex-wrap items-center gap-1.5">
            <span class="font-bold">{{ orphan.symbol ?? t("common.unknown") }}</span>
            <span :class="sideClass(orphan.side, orphan.qty)">{{ sideLabel(orphan.side, orphan.qty) }}</span>
            <span class="min-w-0 text-xs text-muted [overflow-wrap:anywhere]">{{ orphan.accountName ?? t("common.unknown") }}</span>
            <EnvBadge :environment="orphan.environment" />
          </div>
          <dl class="grid grid-cols-3 gap-2 text-xs">
            <div class="min-w-0"><dt class="text-muted">{{ t("common.quantity") }}</dt><dd class="font-mono font-semibold tabular-nums">{{ formatQty(orphan.qty) }}</dd></div>
            <div class="min-w-0"><dt class="text-muted">{{ t("board.entry") }}</dt><dd class="font-mono font-semibold tabular-nums">{{ formatPrice(orphan.entryPrice) }}</dd></div>
            <div class="min-w-0"><dt class="text-muted">{{ t("common.pnl") }}</dt><dd class="font-mono font-semibold tabular-nums" :class="pnlClass(orphan.pnl)">{{ signedUsd(orphan.pnl) }}</dd></div>
          </dl>
          <p class="text-xs font-semibold text-danger md:text-right">{{ orphan.reason ? reasonText(orphan.reason) : t("board.orphanWarning") }}</p>
        </div>
      </div>
      <div class="grid min-w-0 gap-5" :class="showTimeline ? 'xl:grid-cols-[minmax(0,1fr)_300px]' : ''">
        <div class="min-w-0">
          <!-- 隐藏或未知仓位不是已确认空仓，不能显示无持仓。 -->
          <EmptyState v-if="!positions.length" :title="t(boardPositionsConfirmed(lead) ? 'board.noPositions' : 'board.unknownPositions')" data-testid="empty-positions" />
          <!-- 一个持仓一张卡：未绑定模式双列平铺，已绑定模式左边为带单盈亏与价位、右边为我的对应跟单状态。 -->
          <div v-else class="grid min-w-0 gap-3" :class="!showMineSection ? 'lg:grid-cols-2' : ''">
            <article v-for="position in positions" :id="`pos-${position.symbol}`" :key="positionKey(position)" :data-position="position.key"
              class="min-w-0 rounded-xl border p-3 sm:p-4" :class="[cardTone(position), position.openedToday ? 'today-edge' : '']">
              <div class="grid min-w-0 gap-3 sm:gap-4" :class="showMineSection ? 'lg:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]' : ''">
                <div class="min-w-0">
                  <div class="flex flex-wrap items-center gap-1.5">
                    <h3 class="text-base font-bold tracking-tight">{{ position.symbol ?? t("common.unknown") }}</h3>
                    <span :class="sideClass(position.side, position.qty)">{{ sideLabel(position.side, position.qty) }}</span>
                    <span v-if="position.leverage != null" class="pill font-mono tabular-nums">{{ t("unit.leverage", { n: formatQty(position.leverage) }) }}</span>
                    <span v-if="position.openedToday" class="pill tone-accent font-semibold">{{ t("board.today") }}</span>
                    <time class="ml-auto font-mono text-xs tabular-nums text-muted" :title="t('board.openedAt')">{{ formatDateTime(position.openedAt, { short: true, withSeconds: false }) }}</time>
                  </div>
                  <!-- 焦点强化：现价与浮动盈亏（ROE% + 金额）一眼可见。 -->
                  <div class="mt-2.5 flex flex-wrap items-end justify-between gap-x-4 gap-y-1.5 sm:mt-3">
                    <div class="min-w-0">
                      <p class="text-xs text-muted">{{ t("board.mark") }}</p>
                      <p class="mt-0.5 font-mono font-bold leading-none tabular-nums tracking-tight [overflow-wrap:anywhere]" :class="readOnly ? 'text-xl sm:text-2xl' : 'text-lg sm:text-2xl'" data-testid="mark-price">{{ formatPrice(position.markPrice) }}</p>
                    </div>
                    <div class="min-w-0 text-right">
                      <p class="font-mono text-xl font-bold leading-none tabular-nums sm:text-2xl" :class="pnlClass(position.pnlPct)">{{ formatPct(position.pnlPct) }}</p>
                      <p class="mt-1 font-mono text-sm font-semibold tabular-nums" :class="pnlClass(position.pnl)">{{ signedUsd(position.pnl) }}</p>
                    </div>
                  </div>
                  <!-- 移动端紧凑摘要行：均价与数量一行呈现，次要信息默认收起、提供展开开关。 -->
                  <div class="mt-2 flex items-center justify-between text-xs sm:hidden">
                    <span class="font-mono tabular-nums text-muted">{{ t("board.entry") }} {{ formatPrice(position.entryPrice) }}<span v-if="showQuantity"> · {{ formatQty(position.qty) }}</span></span>
                    <button type="button" class="inline-flex items-center gap-0.5 rounded px-1.5 py-0.5 font-semibold text-accent hover:bg-accent/10" @click="toggleDetails(positionKey(position))">
                      {{ isDetailsExpanded(positionKey(position)) ? t("common.collapse") : t("common.details") }}
                    </button>
                  </div>
                  <div :class="isDetailsExpanded(positionKey(position)) ? '' : 'hidden sm:block'">
                    <dl class="mt-2.5 grid gap-2 text-xs sm:mt-3" :class="showQuantity && showNotional ? 'grid-cols-3' : 'grid-cols-2'">
                      <div class="min-w-0"><dt class="text-muted">{{ t("board.entry") }}</dt><dd class="mt-0.5 font-mono font-semibold tabular-nums">{{ formatPrice(position.entryPrice) }}</dd></div>
                      <div v-if="showQuantity" class="min-w-0"><dt class="text-muted">{{ t("common.quantity") }}</dt><dd class="mt-0.5 font-mono font-semibold tabular-nums">{{ formatQty(position.qty) }}</dd></div>
                      <div v-if="showNotional" class="min-w-0 text-right"><dt class="text-muted">{{ t("board.notional") }}</dt><dd class="mt-0.5 font-mono font-semibold tabular-nums">{{ formatUsd(position.notional, 0) }}</dd></div>
                    </dl>
                    <!-- 止损在左、止盈在右，中间的位置条标出现价落在两者之间的哪里；仅在有已知止损或止盈时渲染。 -->
                    <div v-if="showLevels(position)" class="mt-3 rounded-lg border border-border/40 bg-surface/70 p-2.5">
                      <div class="flex items-start justify-between gap-3 text-xs">
                        <div class="min-w-0">
                          <p class="font-semibold text-danger">{{ t("board.stopLoss") }}</p>
                          <p class="mt-0.5 font-mono font-semibold tabular-nums">{{ formatPrice(position.stopLossPrice) }}</p>
                          <p class="font-mono tabular-nums text-muted">{{ t("board.fromMark") }} {{ formatPct(distancePct(position.stopLossPrice, position.markPrice)) }}</p>
                        </div>
                        <div class="min-w-0 text-right">
                          <p class="font-semibold text-success">{{ t("board.takeProfit") }}</p>
                          <p class="mt-0.5 font-mono font-semibold tabular-nums">{{ formatPrice(position.takeProfitPrice) }}</p>
                          <p class="font-mono tabular-nums text-muted">{{ t("board.fromMark") }} {{ formatPct(distancePct(position.takeProfitPrice, position.markPrice)) }}</p>
                        </div>
                      </div>
                      <LevelTrack class="mt-2.5" :mark="position.markPrice" :entry="position.entryPrice" :take-profit="position.takeProfitPrice" :stop-loss="position.stopLossPrice" />
                    </div>
                  </div>
                </div>
                <div v-if="showMineSection" class="min-w-0 border-t border-border/50 pt-2.5 sm:pt-3 lg:border-l lg:border-t-0 lg:pl-4 lg:pt-0">
                  <p class="eyebrow mb-2">{{ t("board.mine") }}</p>
                  <div class="space-y-2">
                    <MineCard v-for="mine in mineRows(position)" :key="mine?.bindingId || mine?.accountName || 'unbound'" :mine="mine" :class="mine?.bindingEnabled === false ? 'opacity-65' : ''" />
                  </div>
                </div>
              </div>
            </article>
          </div>
        </div>
        <LeadTimeline v-if="showTimeline" class="border-t border-border/50 pt-4 xl:border-l xl:border-t-0 xl:pl-5 xl:pt-0" :events="lead.todayEvents" :positions="lead.positions" :accounts="readOnly ? null : accounts" :read-only="readOnly" />
      </div>
      <LeadCalculator v-if="calcOpen && !readOnly && lead.id != null" class="mt-5" :lead-id="lead.id" :name="lead.name" />
    </div>
  </section>
</template>
