<script setup lang="ts">
import { computed, inject, ref, useId, watch } from "vue";
import { routeLocationKey, routerKey } from "vue-router";
import {
  ArrowLeft,
  ArrowLeftRight,
  Gauge,
  History,
  Layers,
  LineChart,
  RefreshCw,
  ShieldCheck,
} from "lucide-vue-next";
import { useI18n } from "@/i18n";
import { useAccountDetail } from "@/composables/useAccountDetail";
import { formatDateTime } from "@/utils/format";
import EnvBadge from "@/components/EnvBadge.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import FreshnessStamp from "@/components/FreshnessStamp.vue";
import ErrorState from "@/components/ErrorState.vue";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";
import RiskOverview from "@/components/account-detail/RiskOverview.vue";
import AccountEquityChart from "@/components/account-detail/AccountEquityChart.vue";
import PnlBreakdown from "@/components/account-detail/PnlBreakdown.vue";
import PositionsPanel from "@/components/account-detail/PositionsPanel.vue";
import OpenOrdersPanel from "@/components/account-detail/OpenOrdersPanel.vue";
import CashflowTable from "@/components/account-detail/CashflowTable.vue";
import TradeHistoryTable from "@/components/account-detail/TradeHistoryTable.vue";
import PerformancePanel from "@/components/account-detail/PerformancePanel.vue";
import AlertsPanel from "@/components/account-detail/AlertsPanel.vue";
import DataQualityBadge from "@/components/account-detail/DataQualityBadge.vue";

defineOptions({ name: "AccountDetailView" });

const route = inject(routeLocationKey, null);
const router = inject(routerKey, null);
const { t } = useI18n();

const accountId = computed<number | null>(() => {
  const raw = route?.params?.id;
  const num = Number(Array.isArray(raw) ? raw[0] : raw);
  return Number.isInteger(num) && num > 0 ? num : null;
});

const {
  range,
  equitySeries,
  detail,
  equity,
  openOrders,
  cashflows,
  trades,
  performance,
  loading,
  errors,
  syncing,
  syncResult,
  syncError,
  hasFatalError,
  cashflowFilters,
  tradeFilters,
  openOrderCategory,
  fetchCashflows,
  fetchTrades,
  refreshAll,
  triggerManualSync,
} = useAccountDetail(accountId);

type TabKey = "positions" | "equity" | "cashflows" | "trades" | "performance";

const uid = useId();
const activeTab = ref<TabKey>("positions");
const visited = ref<Set<TabKey>>(new Set(["positions"]));

const tabs = computed<
  { key: TabKey; label: string; icon: typeof Layers; count: number | null; badgeTone?: string }[]
>(() => {
  const posCount = detail.value?.positions.positionsKnown ? detail.value.positions.items.length : null;
  const alerts = detail.value?.alerts.active ?? [];
  const alertCount = alerts.length > 0 ? alerts.length : null;
  const hasCritical = alerts.some(item => item.severity === "critical");
  return [
    { key: "positions", label: "accountDetail.tab.positions", icon: Layers, count: posCount },
    { key: "equity", label: "accountDetail.tab.equity", icon: LineChart, count: null },
    { key: "cashflows", label: "accountDetail.tab.cashflows", icon: ArrowLeftRight, count: null },
    { key: "trades", label: "accountDetail.tab.trades", icon: History, count: null },
    {
      key: "performance",
      label: "accountDetail.tab.performance",
      icon: Gauge,
      count: alertCount,
      badgeTone: alertCount != null ? (hasCritical ? "tone-danger" : "tone-warning") : undefined,
    },
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
  document.getElementById(`acct-tab-${uid}-${key}`)?.focus();
}

watch(accountId, () => {
  activeTab.value = "positions";
  visited.value = new Set(["positions"]);
});

function goAccounts() {
  if (router) void router.push("/accounts");
}
</script>

<template>
  <div class="min-w-0 space-y-5">
    <!-- Top bar + tablist (matches LeadDetail / LeadTelemetryPanel style) -->
    <header class="card card-accent">
      <div class="card-body !p-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div class="space-y-1.5 min-w-0">
            <div class="flex flex-wrap items-center gap-2">
              <button type="button" class="button-secondary min-h-8 px-2.5 py-1 text-xs" @click="goAccounts">
                <ArrowLeft :size="14" />
                {{ t("accountDetail.backToList") }}
              </button>
              <span class="pill tone-neutral text-[11px]">
                <ShieldCheck :size="13" class="mr-1" />
                {{ t("accountDetail.readOnlyBadge") }}
              </span>
            </div>

            <div v-if="detail" class="flex flex-wrap items-center gap-2 pt-1">
              <h1 class="text-xl font-bold tracking-tight [overflow-wrap:anywhere]">{{ detail.account.name }}</h1>
              <span class="pill tone-neutral text-xs">{{ detail.account.exchange.toUpperCase() }}</span>
              <EnvBadge :environment="detail.account.environment" />
              <StatusBadge :state="detail.account.isEnabled ? detail.account.status : 'disabled'" />
              <DataQualityBadge :status="detail.dataQuality.overallStatus" />
            </div>

            <p v-if="detail" class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
              <span>{{ detail.account.apiKeyMasked }}</span>
              <span>· {{ detail.account.marginMode }} / {{ detail.account.positionMode }}</span>
              <span>· {{ t("accts.lastSync") }}: <FreshnessStamp :timestamp="detail.account.lastSyncAtMs" :warn-after="90" :critical-after="300" /> ({{ formatDateTime(detail.account.lastSyncAtMs, { short: true }) }})</span>
            </p>
          </div>

          <div class="flex flex-wrap items-center gap-2">
            <button
              type="button"
              class="button-primary min-h-9 px-3 py-1.5 text-xs"
              :disabled="syncing || accountId == null"
              data-testid="sync-detail-btn"
              @click="triggerManualSync"
            >
              <RefreshCw :size="15" :class="{ 'animate-spin': syncing }" />
              {{ syncing ? t("accountDetail.syncing") : t("accountDetail.syncDetail") }}
            </button>
          </div>
        </div>

        <p v-if="syncError" class="error-banner mt-3" role="alert">{{ syncError }}</p>
        <p v-else-if="syncResult" class="mt-3 rounded-lg border border-border/50 bg-background/50 px-3 py-1.5 text-xs text-muted">
          {{ Object.values(syncResult.results).some(r => r.status !== "ok") ? t("accountDetail.syncPartial") : t("accountDetail.syncDone") }}
        </p>
      </div>

      <!-- 页签栏：默认首屏先展示风险总览与持仓挂单，其余维度按页签懒挂载切换。 -->
      <div
        v-if="detail"
        role="tablist"
        :aria-label="t('accountDetail.sectionNav')"
        data-testid="account-detail-tablist"
        class="flex flex-nowrap items-center gap-1.5 overflow-x-auto border-t border-border/50 bg-surface2/30 px-3 py-2 no-scrollbar sm:flex-wrap sm:px-4"
        @keydown="onTabKeydown"
      >
        <button
          v-for="tab in tabs"
          :key="tab.key"
          :id="`acct-tab-${uid}-${tab.key}`"
          type="button"
          role="tab"
          :data-testid="`account-tab-${tab.key}`"
          :aria-selected="activeTab === tab.key"
          :aria-controls="`acct-panel-${uid}-${tab.key}`"
          :tabindex="activeTab === tab.key ? 0 : -1"
          class="tab-button !min-h-9 !px-3 !py-1.5 text-xs sm:text-sm"
          @click="selectTab(tab.key)"
        >
          <component :is="tab.icon" :size="15" aria-hidden="true" />
          <span>{{ t(tab.label) }}</span>
          <span
            v-if="tab.count != null"
            class="pill !px-1.5 !py-0 font-mono text-[11px] tabular-nums"
            :class="tab.badgeTone || (activeTab === tab.key ? 'tone-accent' : '')"
          >{{ tab.count }}</span>
        </button>
      </div>
    </header>

    <ErrorState v-if="hasFatalError" :message="errors.detail || t('accountDetail.notFound')" :busy="loading.detail" @retry="refreshAll" />
    <LoadingSkeleton v-else-if="loading.detail && !detail" />

    <template v-else-if="detail">
      <!-- Tab 1 (默认): 风险总览 -> 持仓与归属 -> 当前挂单与保护 -->
      <div
        v-if="visited.has('positions')"
        v-show="activeTab === 'positions'"
        :id="`acct-panel-${uid}-positions`"
        role="tabpanel"
        :aria-labelledby="`acct-tab-${uid}-positions`"
        data-testid="account-panel-positions"
        class="space-y-5"
      >
        <RiskOverview :risk="detail.risk" />
        <PositionsPanel
          :positions-known="detail.positions.positionsKnown"
          :items="detail.positions.items"
          :orphan-allocations="detail.positions.orphanAllocations"
          :data-quality="detail.positions.dataQuality"
        />
        <OpenOrdersPanel
          :orders="openOrders"
          :category="openOrderCategory"
          :loading="loading.openOrders"
          @update:category="c => (openOrderCategory = c)"
        />
      </div>

      <!-- Tab 2: 资金曲线与周期盈亏对账 -->
      <div
        v-if="visited.has('equity')"
        v-show="activeTab === 'equity'"
        :id="`acct-panel-${uid}-equity`"
        role="tabpanel"
        :aria-labelledby="`acct-tab-${uid}-equity`"
        data-testid="account-panel-equity"
        class="space-y-5"
      >
        <AccountEquityChart
          :curve="equity"
          :range="range"
          :series="equitySeries"
          :loading="loading.equity"
          @update:range="r => (range = r)"
          @update:series="s => (equitySeries = s)"
        />
        <PnlBreakdown :pnl="detail.pnl" />
      </div>

      <!-- Tab 3: 期货合约资金流水 -->
      <div
        v-if="visited.has('cashflows')"
        v-show="activeTab === 'cashflows'"
        :id="`acct-panel-${uid}-cashflows`"
        role="tabpanel"
        :aria-labelledby="`acct-tab-${uid}-cashflows`"
        data-testid="account-panel-cashflows"
      >
        <CashflowTable
          :cashflows="cashflows"
          :filters="cashflowFilters"
          :loading="loading.cashflows"
          @reload="fetchCashflows"
        />
      </div>

      <!-- Tab 4: 成交、系统执行与订单历史 -->
      <div
        v-if="visited.has('trades')"
        v-show="activeTab === 'trades'"
        :id="`acct-panel-${uid}-trades`"
        role="tabpanel"
        :aria-labelledby="`acct-tab-${uid}-trades`"
        data-testid="account-panel-trades"
      >
        <TradeHistoryTable
          :trades="trades"
          :bindings="detail.bindings.items"
          :filters="tradeFilters"
          :loading="loading.trades"
          @reload="fetchTrades"
        />
      </div>

      <!-- Tab 5: 交易质量统计与异常/数据缺口提醒 -->
      <div
        v-if="visited.has('performance')"
        v-show="activeTab === 'performance'"
        :id="`acct-panel-${uid}-performance`"
        role="tabpanel"
        :aria-labelledby="`acct-tab-${uid}-performance`"
        data-testid="account-panel-performance"
        class="space-y-5"
      >
        <PerformancePanel :performance="performance" :loading="loading.performance" />
        <AlertsPanel
          :active-alerts="detail.alerts.active"
          :recent-resolved="detail.alerts.recentResolved"
          :datasets="detail.dataQuality.datasets"
        />
      </div>
    </template>
  </div>
</template>
