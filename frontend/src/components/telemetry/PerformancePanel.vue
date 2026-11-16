<script setup lang="ts">
import { computed, ref } from "vue";
import { Banknote, Target, TrendingDown, TrendingUp, Users, Wallet } from "lucide-vue-next";
import type { LeadSnapshot } from "@/types/api";
import { useI18n } from "@/i18n";
import { finiteNumber, formatPct, formatQty, formatUsd, signedUsd, type NumericValue } from "@/utils/format";
import { pnlClass } from "@/utils/status";
import KpiCard from "@/components/KpiCard.vue";
const props = defineProps<{ snapshot: LeadSnapshot }>();
const { t } = useI18n();
const KNOWN = ["7D", "30D", "90D", "180D", "365D"];
// 绩效全部来自服务端，未知胜场/收益不能默认为零。
const rows = computed(() => {
  const performance = props.snapshot.performance ?? {};
  // 已知周期按从短到长排列；服务端新增的周期排在后面，不丢弃。
  const keys = [...KNOWN.filter(key => key in performance), ...Object.keys(performance).filter(key => !KNOWN.includes(key))];
  return keys.map(timeframe => ({ timeframe, ...performance[timeframe] }));
});
const chips = computed(() => rows.value.map(row => row.timeframe).filter(key => KNOWN.includes(key)));
const picked = ref("365D");
// 默认看最长周期；该周期没有数据时退到现有的最长已知周期。
const selected = computed(() => {
  const keys = rows.value.map(row => row.timeframe);
  if (keys.includes(picked.value)) return picked.value;
  return [...chips.value].reverse()[0] ?? keys[0] ?? null;
});
const current = computed(() => rows.value.find(row => row.timeframe === selected.value) ?? null);
const profile = computed(() => props.snapshot.profile ?? null);
// 新增或未知周期不直接露出内部翻译 key。
function timeframeLabel(value: string | null) {
  if (value == null) return t("common.unknown");
  const key = `timeframe.${value}`;
  const label = t(key);
  return label === key ? t("common.unknown") : label;
}
type Tone = "success" | "danger" | "neutral";
function pnlTone(value: NumericValue): Tone {
  const number = finiteNumber(value);
  return number == null || number === 0 ? "neutral" : number > 0 ? "success" : "danger";
}
const copyFull = computed(() => {
  const count = profile.value?.currentCopyCount;
  const limit = profile.value?.maxCopyCount;
  return count != null && limit != null && count >= limit;
});
const copierSub = computed(() => `${t("tel.kpiCopiers")} ${formatQty(profile.value?.currentCopyCount)} / ${formatQty(profile.value?.maxCopyCount)}${copyFull.value ? ` · ${t("tel.kpiFull")}` : ""}`);
// 分润比例未知时不拼出“分润 —%”这种半截文案。
const marginSub = computed(() => profile.value?.profitSharingRate == null ? "" : t("tel.kpiProfitShare", { rate: formatQty(profile.value.profitSharingRate) }));
</script>
<template>
  <section class="card">
    <header class="card-header">
      <h3 class="section-title">{{ t("tel.perfMatrixTitle") }}</h3>
      <div v-if="chips.length > 1" class="flex flex-wrap gap-1.5" role="group" :aria-label="t('tel.tfCol')">
        <button v-for="key in chips" :key="key" type="button" class="chip-button" :aria-pressed="selected === key" @click="picked = key">{{ timeframeLabel(key) }}</button>
      </div>
    </header>
    <div class="card-body space-y-4">
      <!-- 选中周期的六个核心数：收益、规模、本金、跟单者盈亏、胜率、回撤。 -->
      <div class="grid grid-cols-2 gap-3 md:grid-cols-3 2xl:grid-cols-6">
        <KpiCard :tone="pnlTone(current?.roi)" :icon="(finiteNumber(current?.roi) ?? 0) < 0 ? TrendingDown : TrendingUp" :label="t('tel.kpiRoi', { tf: timeframeLabel(selected) })"
          :value="formatPct(current?.roi)" :value-class="pnlClass(current?.roi)" :sub="`${t('tel.pnlCol')} ${signedUsd(current?.pnl)}`" />
        <KpiCard tone="accent" :icon="Banknote" :label="t('tel.kpiAum')" :value="formatUsd(profile?.aumAmount, 0)"
          :sub="profile?.copyAum == null ? '' : t('tel.kpiCopyAum', { val: formatUsd(profile.copyAum, 0) })" />
        <KpiCard tone="brand" :icon="Wallet" :label="t('tel.kpiMarginBal')" :value="formatUsd(profile?.marginBalance)" :sub="marginSub" />
        <KpiCard :tone="pnlTone(profile?.copierPnl)" :icon="Users" :label="t('tel.kpiCopierPnl')" :value="signedUsd(profile?.copierPnl)" :value-class="pnlClass(profile?.copierPnl)" :sub="copierSub" />
        <KpiCard :icon="Target" :label="`${t('tel.kpiWinRate')} · ${timeframeLabel(selected)}`" :value="formatPct(current?.winRate, 2, false)"
          :sub="t('tel.kpiWinSub', { win: formatQty(current?.winOrders), total: formatQty(current?.totalOrder) })" />
        <KpiCard tone="warning" :icon="TrendingDown" :label="`${t('tel.kpiMdd')} · ${timeframeLabel(selected)}`" :value="formatPct(current?.mdd, 2, false)"
          :sub="`${t('tel.sharpeCol')} ${formatQty(current?.sharpRatio ?? profile?.sharpRatio)}`" />
      </div>
      <template v-if="rows.length">
        <!-- 多周期对比：点任意一行切换上方卡片的周期。 -->
        <div class="hidden md:block">
          <table class="data-table">
            <caption class="sr-only">{{ t("tel.perfMatrixTitle") }}</caption>
            <thead><tr>
              <th scope="col" class="text-left">{{ t("tel.tfCol") }}</th>
              <th scope="col" class="number">{{ t("tel.roiCol") }}</th>
              <th scope="col" class="number">{{ t("tel.pnlCol") }}</th>
              <th scope="col" class="number">{{ t("tel.copierPnlCol") }}</th>
              <th scope="col" class="number">{{ t("tel.mddCol") }}</th>
              <th scope="col" class="number">{{ t("tel.winRateCol") }}</th>
              <th scope="col" class="number">{{ t("tel.sharpeCol") }}</th>
            </tr></thead>
            <tbody>
              <tr v-for="row in rows" :key="row.timeframe" class="cursor-pointer" :class="selected === row.timeframe ? 'bg-accent/[0.08]' : 'hover:bg-surface2/40'" @click="picked = row.timeframe">
                <td><button type="button" class="rounded font-semibold" :class="selected === row.timeframe ? 'text-accent' : ''" :aria-pressed="selected === row.timeframe">{{ timeframeLabel(row.timeframe) }}</button></td>
                <td class="number"><strong :class="pnlClass(row.roi)">{{ formatPct(row.roi) }}</strong></td>
                <td class="number"><span :class="pnlClass(row.pnl)">{{ signedUsd(row.pnl) }}</span></td>
                <td class="number"><span :class="pnlClass(row.copierPnl)">{{ signedUsd(row.copierPnl) }}</span></td>
                <td class="number">{{ formatPct(row.mdd, 2, false) }}</td>
                <td class="number">{{ formatPct(row.winRate, 2, false) }}<span class="ml-1.5 text-xs text-muted">{{ formatQty(row.winOrders) }} / {{ formatQty(row.totalOrder) }}</span></td>
                <td class="number">{{ formatQty(row.sharpRatio) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <!-- 手机上每个周期一行小卡：收益率最显眼，其余三个数排在下面。 -->
        <div class="grid gap-2 sm:grid-cols-2 md:hidden">
          <button v-for="row in rows" :key="row.timeframe" type="button" class="tile text-left transition" :class="selected === row.timeframe ? '!border-accent/40 !bg-accent/[0.08]' : ''" :aria-pressed="selected === row.timeframe" @click="picked = row.timeframe">
            <span class="flex items-center justify-between gap-2"><span class="text-sm font-semibold">{{ timeframeLabel(row.timeframe) }}</span><strong class="font-mono tabular-nums" :class="pnlClass(row.roi)">{{ formatPct(row.roi) }}</strong></span>
            <span class="mt-1.5 flex flex-wrap justify-between gap-x-3 gap-y-0.5 font-mono text-xs tabular-nums text-muted">
              <span :class="pnlClass(row.pnl)">{{ signedUsd(row.pnl) }}</span>
              <span>{{ t("tel.mddShort") }} {{ formatPct(row.mdd, 2, false) }}</span>
              <span>{{ t("tel.winShort") }} {{ formatPct(row.winRate, 1, false) }}</span>
            </span>
          </button>
        </div>
      </template>
    </div>
  </section>
</template>
