<script setup lang="ts">
import { computed, ref } from "vue";
import { Calculator, Gauge, History, Layers, LineChart, PieChart, Zap } from "lucide-vue-next";
import type { LeadSnapshot } from "@/types/api";
import { useI18n } from "@/i18n";
import { useAgo } from "@/composables/useAgo";
import { useNow } from "@/composables/useNow";
import { formatDateTime, timestampMs } from "@/utils/format";
import LeadAvatar from "./LeadAvatar.vue";
import StatTile from "./StatTile.vue";
import PositionCalculatorCard from "./PositionCalculatorCard.vue";
import AllocationCard from "./telemetry/AllocationCard.vue";
import OrderHistory from "./telemetry/OrderHistory.vue";
import PerformancePanel from "./telemetry/PerformancePanel.vue";
import PositionAnalysis from "./telemetry/PositionAnalysis.vue";
import RoiChart from "./telemetry/RoiChart.vue";

// 带单员遥测面板：所有区块从上到下一次排开，不用页签把信息藏起来。
// embedded 表示外层页面已经展示了带单员的名称与头像（详情页），这里不再重复。
const props = defineProps<{
  snapshot: LeadSnapshot; portfolioId?: string; displayName?: string; countdownSec?: number; embedded?: boolean;
}>();
const { t } = useI18n();
const ago = useAgo();
const now = useNow();
const profile = computed(() => props.snapshot.profile);
const meta = computed(() => props.snapshot.meta);
const name = computed(() => props.displayName || profile.value?.nickname || props.portfolioId || t("common.unknown"));
const portfolio = computed(() => meta.value?.portfolioId || props.portfolioId || t("common.unknown"));
const tags = computed(() => (profile.value?.tags ?? []).slice(0, 5));

// 时间戳 → 距今秒数；未知保持 null，不当成“0 秒前”。
function ageSec(value: Parameters<typeof timestampMs>[0]) {
  const time = timestampMs(value);
  return time == null ? null : Math.max(0, Math.floor((now.value - time) / 1000));
}
// 行情时效优先取价格刷新时间，其次是持仓汇总的定价时刻，最后才是快照生成时间。
const priceAge = computed(() => ageSec(meta.value?.priceUpdatedAt ?? props.snapshot.openPositionsSummary?.priceAsOf ?? meta.value?.generatedAt));
const syncAge = computed(() => ageSec(meta.value?.generatedAt));
// 与新鲜度徽标同一组阈值：30 秒提示，90 秒或后端标记过期则告警。
const ageTone = computed(() => meta.value?.stale === true || (priceAge.value != null && priceAge.value >= 90) ? "text-danger"
  : priceAge.value == null || priceAge.value >= 30 ? "text-warning" : "text-success");
const syncText = computed(() => {
  if (syncAge.value == null) return "";
  const ms = meta.value?.fetchMs;
  return ms == null ? t("tel.syncAgeShort", { s: syncAge.value }) : t("tel.syncAge", { s: syncAge.value, ms });
});
const lastTradeText = computed(() => timestampMs(profile.value?.lastTradeTime) == null ? "" : formatDateTime(profile.value?.lastTradeTime));

// 导航顺序就是页面上区块的顺序；“今日动态”只有外层传入时间线时才出现。
const SECTIONS = [
  { key: "positions", label: "telemetry.positions", icon: Layers },
  { key: "timeline", label: "board.timeline", icon: Zap },
  { key: "calculator", label: "telemetry.calculator", icon: Calculator },
  { key: "performance", label: "telemetry.performance", icon: Gauge },
  { key: "returns", label: "telemetry.returns", icon: LineChart },
  { key: "allocation", label: "telemetry.allocation", icon: PieChart },
  { key: "history", label: "telemetry.history", icon: History },
];
const root = ref<HTMLElement | null>(null);
// 滚动到指定区块；系统要求减少动效时直接定位，不做平滑滚动。
function jump(key: string) {
  root.value?.querySelector(`[data-section="${key}"]`)?.scrollIntoView({
    behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start",
  });
}
</script>
<template>
  <div ref="root" class="min-w-0 space-y-4">
    <!-- 头部：身份与标签在左，最后交易与数据时效在右，底部一排按钮直达各区块。 -->
    <section class="card card-accent">
      <div class="card-body flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div class="flex min-w-0 flex-1 items-start gap-3.5">
          <LeadAvatar v-if="!embedded" :name="name" :url="profile?.avatarUrl" :size="56" />
          <div class="flex min-w-0 flex-1 flex-col gap-2">
            <!-- 嵌入详情页且没有任何标签时这一行为空，直接隐藏，不留空白。 -->
            <div class="flex flex-wrap items-center gap-2 empty:hidden">
              <h2 v-if="!embedded" class="text-xl font-bold tracking-tight">{{ name }}</h2>
              <span v-if="profile?.badgeName" class="pill tone-warning font-semibold">{{ profile.badgeName }}</span>
              <span v-if="profile?.positionShow === true" class="pill tone-success">{{ t("tel.directPosTag") }}</span>
              <span v-else-if="profile?.positionShow === false" class="pill tone-accent">{{ t("tel.hiddenPosTag") }}</span>
              <span v-if="profile?.signTradFi" class="pill tone-brand">{{ t("tel.tradFiTag") }}</span>
            </div>
            <p class="flex flex-wrap items-center gap-x-2 gap-y-1.5 text-xs text-muted">
              <span>{{ t("tel.portfolioId") }} <span class="font-mono font-semibold text-foreground">{{ portfolio }}</span></span>
              <span v-for="tag in tags" :key="tag" class="pill">{{ tag }}</span>
            </p>
            <p v-if="profile?.description" class="max-w-3xl text-sm leading-relaxed text-muted">{{ profile.description }}</p>
          </div>
        </div>
        <div class="min-w-0 shrink-0 space-y-2 lg:w-[400px]">
          <dl class="grid grid-cols-2 gap-2">
            <StatTile :label="t('tel.lastTrade')" :value="ago(profile?.lastTradeTime) || t('common.unknown')" :sub="lastTradeText" value-class="font-sans text-base" />
            <StatTile :label="t('tel.dataAge')" :value="priceAge == null ? t('common.unknown') : t('tel.priceAge', { s: priceAge })" :value-class="`font-sans text-base ${ageTone}`" :sub="syncText" />
          </dl>
          <p v-if="meta?.orderStoreSize != null || countdownSec != null" class="tile flex flex-wrap items-center justify-between gap-x-3 gap-y-1 !py-2 text-xs text-muted">
            <span v-if="meta?.orderStoreSize != null">{{ t("tel.storeOrders", { n: meta.orderStoreSize }) }}</span>
            <span v-if="countdownSec != null" class="ml-auto">{{ t("tel.nextRefresh") }} <strong class="font-mono tabular-nums text-accent">{{ countdownSec }}s</strong></span>
          </p>
        </div>
      </div>
      <nav class="flex flex-wrap items-center gap-1.5 border-t border-border/50 px-4 py-2.5 sm:px-5" :aria-label="t('tel.jumpNav')">
        <template v-for="item in SECTIONS" :key="item.key">
          <button v-if="item.key !== 'timeline' || $slots.aside" type="button" class="chip-button gap-1.5 font-sans" @click="jump(item.key)">
            <component :is="item.icon" :size="14" aria-hidden="true" />{{ t(item.label) }}
          </button>
        </template>
      </nav>
    </section>
    <!-- 持仓分析放最前；外层给了时间线（公开页的今日动态）时并排在右侧，滚动时跟随停留。 -->
    <div v-if="$slots.aside" class="grid min-w-0 items-start gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
      <PositionAnalysis :snapshot="snapshot" data-section="positions" />
      <section data-section="timeline" class="card xl:sticky xl:top-[var(--sticky-top,1rem)] xl:max-h-[calc(100dvh-var(--sticky-top,1rem)-1rem)] xl:overflow-y-auto">
        <div class="card-body"><slot name="aside" /></div>
      </section>
    </div>
    <PositionAnalysis v-else :snapshot="snapshot" data-section="positions" />
    <PositionCalculatorCard :snapshot="snapshot" :display-name="displayName" data-section="calculator" />
    <PerformancePanel :snapshot="snapshot" data-section="performance" />
    <div class="grid min-w-0 gap-4 xl:grid-cols-3">
      <RoiChart :points="snapshot.roiChart365D" data-section="returns" class="xl:col-span-2" />
      <AllocationCard :allocation="snapshot.allocation" data-section="allocation" />
    </div>
    <section class="card" data-section="history"><div class="card-body"><OrderHistory :snapshot="snapshot" /></div></section>
  </div>
</template>
