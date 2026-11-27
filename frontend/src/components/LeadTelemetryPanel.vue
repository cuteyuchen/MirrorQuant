<script setup lang="ts">
import { computed, ref, useId } from "vue";
import { Gauge, History, Layers, LineChart } from "lucide-vue-next";
import type { LeadSnapshot } from "@/types/api";
import { useI18n } from "@/i18n";
import { useAgo } from "@/composables/useAgo";
import { useNow } from "@/composables/useNow";
import { formatDateTime, timestampMs } from "@/utils/format";
import { positionsConfirmed } from "@/utils/positions";
import LeadAvatar from "./LeadAvatar.vue";
import StatTile from "./StatTile.vue";
import AllocationCard from "./telemetry/AllocationCard.vue";
import OrderHistory from "./telemetry/OrderHistory.vue";
import PerformancePanel from "./telemetry/PerformancePanel.vue";
import PositionAnalysis from "./telemetry/PositionAnalysis.vue";
import RoiChart from "./telemetry/RoiChart.vue";

// 带单员遥测面板：按页签分栏展示持仓测算、绩效、收益分布与交易历史，避免整页过长滚动；懒挂载后保留各页签交互状态。
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

type TabKey = "positions" | "performance" | "returns" | "history";
const uid = useId();
const activeTab = ref<TabKey>("positions");
const visited = ref<Set<TabKey>>(new Set(["positions"]));
const tabs = computed<{ key: TabKey; label: string; icon: typeof Layers; count: number | null }[]>(() => {
  const posCount = positionsConfirmed(props.snapshot) ? (props.snapshot.openPositions?.length ?? 0) : null;
  const histCount = (props.snapshot.orderBatches?.length || null) ?? props.snapshot.orders?.length ?? null;
  return [
    { key: "positions", label: "tel.tabPositions", icon: Layers, count: posCount },
    { key: "performance", label: "telemetry.performance", icon: Gauge, count: null },
    { key: "returns", label: "tel.tabReturns", icon: LineChart, count: null },
    { key: "history", label: "telemetry.history", icon: History, count: histCount },
  ];
});
function selectTab(key: TabKey) {
  if (!visited.value.has(key)) visited.value = new Set(visited.value).add(key);
  activeTab.value = key;
}
function onTabKeydown(event: KeyboardEvent) {
  const keys = tabs.value.map(tab => tab.key);
  const current = keys.indexOf(activeTab.value);
  let next = -1;
  if (event.key === "ArrowRight") next = (current + 1) % keys.length;
  else if (event.key === "ArrowLeft") next = (current - 1 + keys.length) % keys.length;
  else if (event.key === "Home") next = 0;
  else if (event.key === "End") next = keys.length - 1;
  if (next < 0) return;
  event.preventDefault();
  const key = keys[next];
  selectTab(key);
  document.getElementById(`tel-tab-${uid}-${key}`)?.focus();
}
</script>
<template>
  <div class="min-w-0 space-y-4">
    <!-- 头部：身份与标签在左，最后交易与数据时效在右，底部切换四个深度分析页签。 -->
    <section class="card card-accent">
      <div class="card-body flex flex-col gap-4 !p-3.5 sm:!p-4 lg:flex-row lg:items-start lg:justify-between">
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
      <!-- 页签栏：手机横向滑动不折行，桌面端单行对齐，方向键可切换。 -->
      <div role="tablist" :aria-label="t('tel.analysisTabs')" data-testid="telemetry-tablist"
        class="flex flex-nowrap items-center gap-1.5 overflow-x-auto border-t border-border/50 bg-surface2/30 px-3 py-2 no-scrollbar sm:flex-wrap sm:px-4"
        @keydown="onTabKeydown">
        <button v-for="tab in tabs" :key="tab.key" :id="`tel-tab-${uid}-${tab.key}`" type="button" role="tab"
          :data-testid="`telemetry-tab-${tab.key}`"
          :aria-selected="activeTab === tab.key" :aria-controls="`tel-panel-${uid}-${tab.key}`"
          :tabindex="activeTab === tab.key ? 0 : -1"
          class="tab-button !min-h-9 !px-3 !py-1.5 text-xs sm:text-sm"
          @click="selectTab(tab.key)">
          <component :is="tab.icon" :size="15" aria-hidden="true" />
          <span>{{ t(tab.label) }}</span>
          <span v-if="tab.count != null" class="pill !px-1.5 !py-0 font-mono text-[11px] tabular-nums" :class="activeTab === tab.key ? 'tone-accent' : ''">{{ tab.count }}</span>
        </button>
      </div>
    </section>
    <!-- 持仓与测算默认首屏展示；公开看板右侧仍保留等高同步的今日动态时间线。 -->
    <div v-if="visited.has('positions')" v-show="activeTab === 'positions'" :id="`tel-panel-${uid}-positions`" role="tabpanel"
      :aria-labelledby="`tel-tab-${uid}-positions`" data-testid="telemetry-panel-positions">
      <div v-if="$slots.aside" class="grid min-w-0 gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
        <PositionAnalysis :snapshot="snapshot" :embedded="embedded" with-calculator :display-name="displayName" data-section="positions" />
        <div class="min-w-0 xl:relative">
          <section data-section="timeline" class="card xl:absolute xl:inset-0 xl:flex xl:flex-col xl:overflow-hidden">
            <div class="card-body flex min-h-0 flex-1 flex-col !p-3 sm:!p-4"><slot name="aside" /></div>
          </section>
        </div>
      </div>
      <PositionAnalysis v-else :snapshot="snapshot" :embedded="embedded" with-calculator :display-name="displayName" data-section="positions" />
    </div>
    <div v-if="visited.has('performance')" v-show="activeTab === 'performance'" :id="`tel-panel-${uid}-performance`" role="tabpanel"
      :aria-labelledby="`tel-tab-${uid}-performance`" data-testid="telemetry-panel-performance">
      <PerformancePanel :snapshot="snapshot" data-section="performance" />
    </div>
    <div v-if="visited.has('returns')" v-show="activeTab === 'returns'" :id="`tel-panel-${uid}-returns`" role="tabpanel"
      :aria-labelledby="`tel-tab-${uid}-returns`" data-testid="telemetry-panel-returns" class="grid min-w-0 gap-4 xl:grid-cols-3">
      <RoiChart :points="snapshot.roiChart365D" data-section="returns" class="xl:col-span-2" />
      <AllocationCard :allocation="snapshot.allocation" data-section="allocation" />
    </div>
    <div v-if="visited.has('history')" v-show="activeTab === 'history'" :id="`tel-panel-${uid}-history`" role="tabpanel"
      :aria-labelledby="`tel-tab-${uid}-history`" data-testid="telemetry-panel-history">
      <section class="card" data-section="history"><div class="card-body"><OrderHistory :snapshot="snapshot" /></div></section>
    </div>
  </div>
</template>
