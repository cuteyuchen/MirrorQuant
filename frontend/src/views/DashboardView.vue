<script setup lang="ts">
import { computed, onActivated, onMounted } from "vue";
import { Activity, AlertTriangle, CheckCircle2, Layers, RefreshCw, TrendingDown, TrendingUp, Wallet } from "lucide-vue-next";
import { useBoardStore } from "@/stores/board";
import { useI18n } from "@/i18n";
import type { BoardAccount } from "@/types/api";
import { finiteNumber, formatPct, formatUsd, signedUsd, type NumericValue } from "@/utils/format";
import { pnlClass } from "@/utils/status";
import AccountSummary from "@/components/board/AccountSummary.vue";
import LeadComparison from "@/components/board/LeadComparison.vue";
import KpiCard from "@/components/KpiCard.vue";
import PageHeader from "@/components/PageHeader.vue";
import EmptyState from "@/components/EmptyState.vue";
import ErrorState from "@/components/ErrorState.vue";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";
defineOptions({ name: "DashboardView" });
const board = useBoardStore();
const { t } = useI18n();
const subtitle = computed(() => t("board.subtitle", {
  leads: board.data?.leads?.length ?? "—", accounts: board.data?.accounts?.length ?? "—",
}));

// 只累加已知数值：全部未知时返回 null（显示破折号），并记下有几项未知，界面上明确提示“未计入”。
function sumKnown(values: NumericValue[]) {
  let total: number | null = null;
  let missing = 0;
  for (const value of values) {
    const known = finiteNumber(value);
    if (known == null) missing += 1; else total = (total ?? 0) + known;
  }
  return { total, missing };
}
function tally(accounts: BoardAccount[]) {
  return {
    count: accounts.length,
    equity: sumKnown(accounts.map(account => account.equity)),
    todayPnl: sumKnown(accounts.map(account => account.todayPnl)),
    unrealized: sumKnown(accounts.map(account => account.unrealizedPnl)),
  };
}
// 实盘与模拟盘的钱不相加：有实盘账户时首屏只汇总实盘，其余账户作为补充说明。
const money = computed(() => {
  const all = board.data?.accounts;
  if (!all?.length) return null;
  const live = all.filter(account => account.environment === "live");
  const rest = all.filter(account => account.environment !== "live");
  if (!live.length) {
    // 未知环境不冒充模拟盘：全部确认是模拟盘才用“模拟盘”标签。
    return { scope: all.every(account => account.environment === "demo") ? "demo" : "all", accounts: all, main: tally(all), rest: null, restKey: "" };
  }
  return {
    scope: "live", accounts: live, main: tally(live), rest: rest.length ? tally(rest) : null,
    restKey: rest.every(account => account.environment === "demo") ? "kpi.demoShare" : "kpi.otherShare",
  };
});
const scope = computed(() => money.value?.scope ?? "all");
// 次要分组（模拟盘 / 其他账户）的一行补充；其中有未知值时照实标出。
function restText(sum: { total: number | null; missing: number } | undefined, format: (value: NumericValue) => string) {
  if (!money.value?.rest || !sum) return "";
  return t(money.value.restKey, { value: format(sum.total) }) + (sum.missing ? t("kpi.restMissing", { n: sum.missing }) : "");
}
function joined(...parts: string[]) { return parts.filter(Boolean).join(" · "); }
const equitySub = computed(() => money.value
  ? joined(t("kpi.accountCount", { n: money.value.main.count }), restText(money.value.rest?.equity, formatUsd))
  : t(board.data?.accounts == null ? "common.unknown" : "board.noAccounts"));
// 不在浏览器里合并收益率：只有恰好一个主账户时直接采用后端给出的百分比。
const todayPct = computed(() => money.value?.accounts.length === 1 ? formatPct(money.value.accounts[0].todayPnlPct) : "");
const todaySub = computed(() => joined(todayPct.value, restText(money.value?.rest?.todayPnl, signedUsd)));
const unrealizedSub = computed(() => restText(money.value?.rest?.unrealized, signedUsd));
type Tone = "success" | "danger" | "warning" | "neutral";
function pnlTone(value: number | null | undefined): Tone { return value == null || value === 0 ? "neutral" : value > 0 ? "success" : "danger"; }

// 带单员一侧：只统计持仓已知的带单员；隐藏或未知的仓位不等于空仓。
const leadStats = computed(() => {
  const leads = board.data?.leads;
  if (!leads) return null;
  let positions: number | null = null;
  let unknownLeads = 0;
  let orphans = 0;
  let misaligned = 0;
  let attentionKnown = false;
  for (const lead of leads) {
    if (lead.positions != null && (lead.positions.length > 0 || lead.positionShow === true)) positions = (positions ?? 0) + lead.positions.length;
    else unknownLeads += 1;
    if (lead.orphans != null) { orphans += lead.orphans.length; attentionKnown = true; }
    if (lead.positions != null) {
      // 有绑定但没跟齐才算需要关注；没有绑定的带单员只是观察对象。
      misaligned += lead.positions.filter(position => position.mine?.some(mine => mine.state !== "aligned")).length;
      attentionKnown = true;
    }
  }
  return {
    positions, unknownLeads, orphans, misaligned,
    attention: attentionKnown ? orphans + misaligned : null,
    opened: sumKnown(leads.map(lead => lead.todayOpenCount)).total,
    closed: sumKnown(leads.map(lead => lead.todayCloseCount)).total,
  };
});
const attentionTone = computed<Tone>(() => {
  const stats = leadStats.value;
  if (stats?.attention == null) return "neutral";
  return stats.orphans > 0 ? "danger" : stats.misaligned > 0 ? "warning" : "success";
});
// 按优先级排序：有残留/未对齐最先 > 有效绑定且有持仓 > 纯观察有持仓 > 空仓或停用放最后。
function leadPriority(lead: NonNullable<typeof board.data>["leads"] extends (infer T)[] | null | undefined ? T : never) {
  const hasOrphan = (lead.orphans?.length ?? 0) > 0;
  const hasMisaligned = !!lead.positions?.some(p => p.mine?.some(m => m.state !== "aligned"));
  if (hasOrphan || hasMisaligned) return 0;
  const hasPositions = (lead.positions?.length ?? 0) > 0;
  const isBound = (lead.activeBoundCount ?? lead.boundCount ?? 0) > 0;
  if (isBound && hasPositions) return 1;
  if (hasPositions) return 2;
  if (lead.enabled !== false && (lead.todayEvents?.length ?? 0) > 0) return 3;
  return 4;
}
const sortedLeads = computed(() => {
  const leads = board.data?.leads;
  if (!leads?.length) return [];
  return [...leads].sort((a, b) => leadPriority(a) - leadPriority(b));
});
function isIdleLead(lead: (typeof sortedLeads.value)[number]) {
  return lead.enabled === false || (!lead.positions?.length && !lead.orphans?.length && !lead.todayEvents?.length);
}
// 全局 App 监听事件序号并轮询；看板本身只消费同一份权威数据。
onMounted(() => { void board.refresh(); });
onActivated(() => { void board.refresh(); });
</script>
<template>
  <div class="space-y-4">
    <PageHeader :title="t('board.title')" :summary="subtitle"><template #actions><button class="icon-button" :disabled="board.loading" :title="t('common.refresh')" :aria-label="t('common.refresh')" @click="board.refresh(true)"><RefreshCw :size="18" :class="{ 'animate-spin': board.loading }" aria-hidden="true" /></button></template></PageHeader>
    <ErrorState v-if="board.error" :message="`${t('board.loadError')} · ${board.error}`" :busy="board.loading" @retry="board.refresh(true)" />
    <LoadingSkeleton v-if="board.loading && !board.data" />
    <template v-else-if="board.data">
      <!-- 首屏五个数：移动端 2 列栅格（总权益通栏 Hero + 2x2 核心指标），桌面端 3/5 列平铺，避免横向截断。 -->
      <section class="grid grid-cols-2 gap-2.5 md:grid-cols-3 xl:grid-cols-5" :aria-label="t('kpi.region')">
        <KpiCard class="col-span-2 md:col-span-1" tone="accent" :icon="Wallet" :label="t(`kpi.equity.${scope}`)" :value="formatUsd(money?.main.equity.total)" :sub="equitySub">
          <p v-if="money?.main.equity.missing" class="mt-1 text-xs font-medium text-warning">{{ t("kpi.partial", { n: money.main.equity.missing }) }}</p>
        </KpiCard>
        <KpiCard :tone="pnlTone(money?.main.todayPnl.total)" :icon="(money?.main.todayPnl.total ?? 0) < 0 ? TrendingDown : TrendingUp" :label="t(`kpi.todayPnl.${scope}`)"
          :value="signedUsd(money?.main.todayPnl.total)" :value-class="pnlClass(money?.main.todayPnl.total)" :sub="todaySub">
          <p v-if="money?.main.todayPnl.missing" class="mt-1 text-xs font-medium text-warning">{{ t("kpi.partial", { n: money.main.todayPnl.missing }) }}</p>
        </KpiCard>
        <KpiCard :tone="pnlTone(money?.main.unrealized.total)" :icon="Activity" :label="t(`kpi.unrealized.${scope}`)"
          :value="signedUsd(money?.main.unrealized.total)" :value-class="pnlClass(money?.main.unrealized.total)" :sub="unrealizedSub">
          <p v-if="money?.main.unrealized.missing" class="mt-1 text-xs font-medium text-warning">{{ t("kpi.partial", { n: money.main.unrealized.missing }) }}</p>
        </KpiCard>
        <KpiCard :tone="leadStats?.positions ? 'brand' : 'neutral'" :icon="Layers" :label="t('kpi.leadPositions')" :value="leadStats?.positions"
          :sub="t('kpi.todayFlow', { open: leadStats?.opened ?? '—', close: leadStats?.closed ?? '—' })">
          <p v-if="leadStats?.unknownLeads" class="mt-1 text-xs font-medium text-warning">{{ t("kpi.leadsUnknown", { n: leadStats.unknownLeads }) }}</p>
        </KpiCard>
        <KpiCard :tone="attentionTone" :icon="attentionTone === 'success' ? CheckCircle2 : AlertTriangle" :label="t('kpi.attention')" :value="leadStats?.attention"
          :value-class="attentionTone === 'danger' ? 'text-danger' : attentionTone === 'warning' ? 'text-warning' : attentionTone === 'success' ? 'text-success' : ''"
          :sub="leadStats?.attention === 0 ? t('kpi.allAligned') : leadStats?.attention == null ? '' : t('kpi.attentionDetail', { orphans: leadStats.orphans, misaligned: leadStats.misaligned })" />
      </section>
      <AccountSummary :accounts="board.data.accounts" />
      <EmptyState v-if="!board.data.leads?.length" class="card" :title="t(board.data.leads == null ? 'common.unknown' : 'board.noLeads')" />
      <LeadComparison v-for="lead in sortedLeads" :key="lead.id || lead.name || 'lead'" :lead="lead" :received-at="board.receivedAt" :accounts="board.data.accounts" :default-collapsed="isIdleLead(lead)" />
    </template>
  </div>
</template>
