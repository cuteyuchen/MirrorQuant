<script setup lang="ts">
import { computed, ref } from "vue";
import { useI18n } from "@/i18n";
import { formatPrice as fmtPrice, formatQty, formatUsd, formatPct as fmtPct } from "@/utils/format";
import PositionCalculatorCard from "@/components/PositionCalculatorCard.vue";

const props = defineProps<{
  snapshot: any;
  portfolioId?: string;
  displayName?: string;
  pollSec?: number;
  countdownSec?: number;
}>();

const { t, isZh } = useI18n();

const activeTf = ref<string>("365D");
const activeTab = ref<"batches" | "cycles" | "closed" | "orders">("batches");
const expandedEntries = ref<Record<string, boolean>>({});
const expandedBatches = ref<Record<number, boolean>>({});
const closedLimit = ref(15);
const cyclesLimit = ref(15);

const profile = computed(() => props.snapshot?.profile || {});
const meta = computed(() => props.snapshot?.meta || {});
const perf = computed(() => props.snapshot?.performance || {});
const summary = computed(() => props.snapshot?.openPositionsSummary || {});
const openMeta = computed(() => props.snapshot?.openPositionsMeta || {});
const openPositions = computed<any[]>(() => props.snapshot?.openPositions || []);
const roiChart = computed<any[]>(() => props.snapshot?.roiChart365D || []);
const allocItems = computed<any[]>(() => props.snapshot?.allocation?.items || []);
const orderBatches = computed<any[]>(() => props.snapshot?.orderBatches || []);
const tradeCycles = computed<any[]>(() => props.snapshot?.tradeCycles || []);
const closedList = computed<any[]>(() => props.snapshot?.closed || []);
const ordersList = computed<any[]>(() => props.snapshot?.orders || []);

const availableTfs = computed(() => {
  const order = ["7D", "30D", "90D", "180D", "365D"];
  return order.filter((tf) => perf.value[tf]);
});

const selectedTf = computed(() => {
  if (perf.value[activeTf.value]) return activeTf.value;
  return availableTfs.value[availableTfs.value.length - 1] || "90D";
});

const currentPerf = computed(() => perf.value[selectedTf.value] || {});

const copyFull = computed(() => {
  const c = profile.value.currentCopyCount;
  const m = profile.value.maxCopyCount;
  return c != null && m != null && c >= m;
});

const riskInfo = computed(() => {
  const cnt = summary.value.positionCount || 0;
  const lev = summary.value.totalNotionalLeverage || 0;
  if (!cnt) return { key: "tel.riskFlat", cls: "bg-slate-800/80 text-slate-400 border-slate-700" };
  if (lev >= 12) return { key: "tel.riskExtreme", cls: "bg-rose-500/15 text-rose-400 border-rose-500/40" };
  if (lev >= 5) return { key: "tel.riskHigh", cls: "bg-rose-500/15 text-rose-400 border-rose-500/30" };
  if (lev >= 2) return { key: "tel.riskMid", cls: "bg-amber-500/15 text-amber-300 border-amber-500/30" };
  if (lev >= 0.8) return { key: "tel.riskLow", cls: "bg-emerald-500/15 text-emerald-400 border-emerald-500/30" };
  return { key: "tel.riskUltraLow", cls: "bg-cyan-500/15 text-cyan-300 border-cyan-500/30" };
});

const holdingStatusText = computed(() => {
  const sc = summary.value.shortCount || 0;
  const lc = summary.value.longCount || 0;
  if (!summary.value.positionCount) return t("tel.flatStatus");
  const parts: string[] = [];
  if (sc) parts.push(t("tel.shortCount", { n: sc }));
  if (lc) parts.push(t("tel.longCount", { n: lc }));
  return t("tel.holdingStatus", { parts: parts.join(" · ") });
});

const holdingExposureNote = computed(() => {
  const sc = summary.value.shortCount || 0;
  const lc = summary.value.longCount || 0;
  if (!summary.value.positionCount) return t("tel.flatDetail");
  if (sc && !lc) return t("tel.pureShort");
  if (lc && !sc) return t("tel.pureLong");
  return t("tel.hedged");
});

function fmtUsd(v: number | string | null | undefined, digits = 2, signed = false) {
  return (signed && Number(v) > 0 ? "+" : "") + formatUsd(v, digits);
}

function fmtLev(v: number | null | undefined) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  return `${Number(v).toFixed(2)}x`;
}

function fmtTime(ms: number | null | undefined) {
  if (!ms) return "—";
  return new Date(ms).toLocaleString(isZh.value ? "zh-CN" : "en-US", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

function fmtShortTime(ms: number | null | undefined) {
  if (!ms) return "—";
  return new Date(ms).toLocaleString(isZh.value ? "zh-CN" : "en-US", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function fmtAgo(ms: number | null | undefined) {
  if (!ms) return "—";
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (s < 60) return t("time.secAgo", { n: s });
  const m = Math.floor(s / 60);
  if (m < 60) return t("time.minAgo", { n: m });
  const h = Math.floor(m / 60);
  if (h < 24) return t("time.hourAgo", { n: h });
  return t("time.dayAgo", { n: Math.floor(h / 24) });
}

function fmtDur(ms: number | null | undefined) {
  if (ms === null || ms === undefined || ms < 0) return "—";
  const m = Math.floor(ms / 60000);
  const h = Math.floor(m / 60);
  const d = Math.floor(h / 24);
  if (d > 0) return t("dur.dayHour", { d, h: h % 24 });
  if (h > 0) return t("dur.hourMin", { h, m: m % 60 });
  return t("dur.min", { m });
}

function pnlColor(v: number | null | undefined) {
  if (v === null || v === undefined) return "text-slate-300";
  return v > 0 ? "text-emerald-400" : v < 0 ? "text-rose-400" : "text-slate-300";
}

function dirLabel(dir: string | undefined) {
  if (!dir) return "—";
  const key = `dir.${dir}`;
  const val = t(key);
  return val === key ? dir : val;
}

function dirBadgeClass(dir: string | undefined) {
  if (!dir) return "bg-slate-800 text-slate-300";
  if (dir.includes("OPEN_LONG") || dir.includes("CLOSE_SHORT") || dir.includes("REDUCE_SHORT")) {
    return "bg-emerald-500/15 text-emerald-400 border border-emerald-500/25";
  }
  return "bg-rose-500/15 text-rose-400 border border-rose-500/25";
}

function posKey(p: any) {
  return `${p.symbol}_${p.positionSide}`;
}

function toggleEntries(p: any) {
  const k = posKey(p);
  expandedEntries.value[k] = !expandedEntries.value[k];
}

function toggleBatch(idx: number) {
  expandedBatches.value[idx] = !expandedBatches.value[idx];
}

function paVerdict(a: any) {
  if (!a) return "—";
  return isZh.value && a.verdictZh ? a.verdictZh : a.verdict;
}

function paTrend1h(a: any) {
  if (!a) return "—";
  return isZh.value && a.trend1hZh ? a.trend1hZh : a.trend1h;
}

function paTrend4h(a: any) {
  if (!a) return "—";
  return isZh.value && a.trend4hZh ? a.trend4hZh : a.trend4h;
}

function paZone(a: any) {
  if (!a) return "—";
  return isZh.value && a.entryZoneZh ? a.entryZoneZh : a.entryZone;
}

function paScale(a: any) {
  if (!a) return "—";
  return isZh.value && a.scalingStyleZh ? a.scalingStyleZh : a.scalingStyle;
}

function paSummary(a: any) {
  if (!a) return "";
  return isZh.value && a.summaryZh ? a.summaryZh : a.summary;
}

function verdictBadgeClass(level: string | undefined) {
  if (level === "good") return "bg-emerald-500/15 text-emerald-400 border-emerald-500/30";
  if (level === "mid") return "bg-cyan-500/15 text-cyan-300 border-cyan-500/30";
  if (level === "warn") return "bg-amber-500/15 text-amber-300 border-amber-500/30";
  return "bg-rose-500/15 text-rose-400 border-rose-500/30";
}

const chartSvg = computed(() => {
  const pts = roiChart.value;
  if (!pts || pts.length < 2) return null;
  const W = 680;
  const H = 190;
  const P = { l: 50, r: 14, t: 16, b: 24 };
  const vals = pts.map((x: any) => Number(x.v || 0));
  const minV = Math.min(...vals, 0);
  const maxV = Math.max(...vals, 0.01);
  const span = maxV - minV || 1;
  const toX = (i: number) => P.l + (i / (pts.length - 1)) * (W - P.l - P.r);
  const toY = (v: number) => P.t + ((maxV - v) / span) * (H - P.t - P.b);

  const line = pts
    .map((p: any, i: number) => `${i === 0 ? "M" : "L"}${toX(i).toFixed(1)},${toY(Number(p.v || 0)).toFixed(1)}`)
    .join(" ");
  const area = `${line} L${toX(pts.length - 1).toFixed(1)},${(H - P.b).toFixed(1)} L${toX(0).toFixed(1)},${(H - P.b).toFixed(1)} Z`;
  const zeroY = toY(0);
  const ticks = [maxV, (maxV + minV) / 2, minV].map((v) => ({
    y: toY(v),
    label: `${v.toFixed(0)}%`,
  }));
  const peak = Math.max(...vals);
  const last = vals[vals.length - 1];
  const lastPt = pts[pts.length - 1];
  const firstPt = pts[0];
  return {
    W,
    H,
    P,
    line,
    area,
    zeroY,
    ticks,
    peak,
    last,
    lastX: toX(pts.length - 1),
    lastY: toY(last),
    firstDate: fmtShortTime(firstPt?.t).split(" ")[0],
    lastDate: fmtShortTime(lastPt?.t).split(" ")[0],
    positive: last >= 0,
  };
});

const allocColors = ["#22d3ee", "#10b981", "#3b82f6", "#f59e0b", "#a855f7", "#64748b"];
const allocSlices = computed(() => {
  const items = allocItems.value || [];
  if (!items.length) return [];
  const total = items.reduce((s: number, x: any) => s + Number(x.volume || 0), 0) || 1;
  return items.slice(0, 6).map((x: any, i: number) => ({
    asset: x.asset,
    pct: (Number(x.volume || 0) / total) * 100,
    color: allocColors[i % allocColors.length],
  }));
});

const dataAgeSec = computed(() => {
  if (!meta.value.generatedAt) return null;
  return Math.max(0, Math.round((Date.now() - meta.value.generatedAt) / 1000));
});

const priceAgeSec = computed(() => {
  const tms = meta.value.priceUpdatedAt || summary.value.priceAsOf || meta.value.generatedAt;
  if (!tms) return null;
  return Math.max(0, Math.round((Date.now() - tms) / 1000));
});
</script>

<template>
  <div class="space-y-5">
    <!-- 1. Hero Profile Header & KPI Matrix -->
    <div class="rounded-2xl bg-surface border border-slate-800/90 p-5">
      <div class="flex flex-col lg:flex-row lg:items-start justify-between gap-4">
        <div class="flex items-start gap-4 min-w-0">
          <img
            v-if="profile.avatarUrl"
            :src="profile.avatarUrl"
            class="w-14 h-14 rounded-2xl border border-slate-700 object-cover shrink-0 bg-background"
            alt="avatar"
          />
          <div
            v-else
            class="w-14 h-14 rounded-2xl border border-slate-700 bg-background flex items-center justify-center font-mono font-bold text-lg text-accent shrink-0"
          >
            {{ (displayName || profile.nickname || "MQ").slice(0, 2).toUpperCase() }}
          </div>
          <div class="min-w-0">
            <div class="flex items-center flex-wrap gap-2">
              <h2 class="text-xl font-bold tracking-tight text-white">
                {{ displayName || profile.nickname || portfolioId }}
              </h2>
              <span
                v-if="profile.badgeName"
                class="px-2 py-0.5 rounded-full text-xs font-mono font-bold uppercase tracking-wider bg-amber-500/15 text-amber-300 border border-amber-500/30"
              >
                {{ profile.badgeName }}
              </span>
              <span
                class="px-2 py-0.5 rounded-full text-xs font-mono border"
                :class="
                  profile.positionShow
                    ? 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30'
                    : 'bg-cyan-500/10 text-cyan-300 border-cyan-500/30'
                "
              >
                {{ profile.positionShow ? t("tel.directPosTag") : t("tel.hiddenPosTag") }}
              </span>
              <span
                v-if="profile.signTradFi"
                class="px-2 py-0.5 rounded-full text-xs font-mono bg-purple-500/15 text-purple-300 border border-purple-500/30"
              >
                {{ t("tel.tradFiTag") }}
              </span>
            </div>

            <div class="text-xs font-mono text-slate-400 mt-1 flex flex-wrap items-center gap-x-3 gap-y-1">
              <span>{{ t("tel.portfolioId") }}: {{ meta.portfolioId || portfolioId }}</span>
              <span v-if="profile.tags?.length" class="flex flex-wrap gap-1.5">
                <span
                  v-for="tag in profile.tags.slice(0, 5)"
                  :key="tag"
                  class="px-1.5 py-0.5 rounded bg-background border border-slate-800 text-xs text-slate-300"
                >
                  {{ tag }}
                </span>
              </span>
            </div>

            <p v-if="profile.description" class="text-xs text-slate-300 mt-2 leading-relaxed max-w-3xl break-words">
              {{ profile.description }}
            </p>
          </div>
        </div>

        <!-- Freshness & Timing Badges -->
        <div class="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-2 gap-2.5 shrink-0 text-xs">
          <div class="px-3 py-2 rounded-xl bg-background/80 border border-slate-800">
            <div class="text-xs font-mono text-slate-400 uppercase">{{ t("tel.lastTrade") }}</div>
            <div class="font-mono font-semibold text-slate-100 mt-0.5">
              {{ fmtAgo(profile.lastTradeTime) }}
            </div>
            <div class="text-xs font-mono text-slate-400">{{ fmtTime(profile.lastTradeTime) }}</div>
          </div>
          <div class="px-3 py-2 rounded-xl bg-background/80 border border-slate-800">
            <div class="text-xs font-mono text-slate-400 uppercase">{{ t("tel.dataAge") }}</div>
            <div class="font-mono font-semibold text-emerald-400 mt-0.5">
              {{ priceAgeSec != null ? t("tel.priceAge", { s: priceAgeSec }) : "—" }}
            </div>
            <div class="text-xs font-mono text-slate-400">
              {{ dataAgeSec != null ? t("tel.syncAge", { s: dataAgeSec, ms: meta.fetchMs ?? 0 }) : "—" }}
            </div>
          </div>
          <div
            v-if="meta.orderStoreSize != null"
            class="px-3 py-2 rounded-xl bg-background/80 border border-slate-800 col-span-2 flex items-center justify-between gap-2"
          >
            <span class="text-sm font-mono text-slate-300">
              {{ t("tel.storeOrders", { n: meta.orderStoreSize }) }}
            </span>
            <span v-if="countdownSec != null" class="text-sm font-mono text-accent">
              {{ t("tel.nextRefresh") }}: {{ countdownSec }}s
            </span>
          </div>
        </div>
      </div>

      <!-- Timeframe Selector -->
      <div class="flex items-center justify-between flex-wrap gap-2 mt-5 mb-2.5">
        <div class="text-sm font-mono uppercase tracking-wider text-slate-400">
          {{ t("tel.perfMatrixTitle") }}
        </div>
        <div v-if="availableTfs.length" class="flex items-center gap-1 bg-background p-1 rounded-lg border border-slate-800">
          <button
            v-for="tf in availableTfs"
            :key="tf"
            type="button"
            @click="activeTf = tf"
            class="px-2.5 py-1 rounded text-sm font-mono font-semibold transition-colors cursor-pointer"
            :class="
              selectedTf === tf
                ? 'bg-accent/20 text-accent border border-accent/30'
                : 'text-slate-400 hover:text-white'
            "
          >
            {{ tf }}
          </button>
        </div>
      </div>

      <!-- 6 Headline KPI Cards -->
      <div class="grid grid-cols-[repeat(auto-fit,minmax(min(100%,220px),1fr))] gap-4">
        <div class="min-w-0 p-4 rounded-xl bg-background/80 border border-slate-800">
          <div class="text-sm font-medium uppercase text-slate-300">{{ selectedTf }} ROI</div>
          <div class="text-2xl font-mono font-bold tracking-tight mt-1" :class="pnlColor(currentPerf.roi)">
            {{ fmtPct(currentPerf.roi) }}
          </div>
          <div class="text-sm font-mono text-slate-300 mt-2">
            PnL {{ fmtUsd(currentPerf.pnl, 2, true) }}
          </div>
        </div>

        <div class="min-w-0 p-4 rounded-xl bg-background/80 border border-slate-800">
          <div class="text-sm font-medium uppercase text-slate-300">{{ t("tel.kpiAum") }}</div>
          <div class="text-2xl font-mono font-bold tracking-tight text-white mt-1">
            {{ fmtUsd(profile.aumAmount, 2) }}
          </div>
          <div class="text-sm font-mono text-slate-300 mt-2">
            {{ t("tel.kpiCopyAum", { val: fmtUsd(profile.copyAum, 2) }) }}
          </div>
        </div>

        <div class="min-w-0 p-4 rounded-xl bg-background/80 border border-slate-800">
          <div class="text-sm font-medium uppercase text-slate-300">{{ t("tel.kpiMarginBal") }}</div>
          <div class="text-2xl font-mono font-bold tracking-tight text-accent mt-1">
            {{ fmtUsd(profile.marginBalance, 2) }}
          </div>
          <div class="text-sm font-mono text-slate-300 mt-2">
            {{ t("tel.kpiProfitShare", { rate: profile.profitSharingRate ?? "—" }) }}
          </div>
        </div>

        <div class="min-w-0 p-4 rounded-xl bg-background/80 border border-slate-800">
          <div class="text-sm font-medium uppercase text-slate-300">{{ t("tel.kpiCopierPnl") }}</div>
          <div class="text-2xl font-mono font-bold tracking-tight mt-1" :class="pnlColor(currentPerf.copierPnl ?? profile.copierPnl)">
            {{ fmtUsd(currentPerf.copierPnl ?? profile.copierPnl, 2, true) }}
          </div>
          <div class="text-sm font-mono text-slate-300 mt-2">
            {{ t("tel.kpiCopiers") }}: {{ profile.currentCopyCount ?? "—" }}/{{ profile.maxCopyCount ?? "—" }}
            <span v-if="copyFull" class="text-amber-400">({{ t("tel.kpiFull") }})</span>
          </div>
        </div>

        <div class="min-w-0 p-4 rounded-xl bg-background/80 border border-slate-800">
          <div class="text-sm font-medium uppercase text-slate-300">{{ t("tel.kpiWinRate") }} ({{ selectedTf }})</div>
          <div class="text-2xl font-mono font-bold tracking-tight text-white mt-1">
            {{ currentPerf.winRate != null ? fmtPct(currentPerf.winRate, 2, false) : "—" }}
          </div>
          <div class="text-sm font-mono text-slate-300 mt-2">
            {{ t("tel.kpiWinSub", { win: currentPerf.winOrders ?? 0, total: currentPerf.totalOrder ?? 0 }) }}
          </div>
        </div>

        <div class="min-w-0 p-4 rounded-xl bg-background/80 border border-slate-800">
          <div class="text-sm font-medium uppercase text-slate-300">{{ t("tel.kpiMdd") }} ({{ selectedTf }})</div>
          <div class="text-2xl font-mono font-bold tracking-tight text-amber-400 mt-1">
            {{ currentPerf.mdd != null ? fmtPct(currentPerf.mdd, 2, false) : "—" }}
          </div>
          <div class="text-sm font-mono text-slate-300 mt-2">
            Sharpe: {{ currentPerf.sharpRatio ?? profile.sharpRatio ?? "—" }}
          </div>
        </div>
      </div>

      <!-- Multi-Timeframe Comparison Table -->
      <div v-if="availableTfs.length > 1" class="mt-3 overflow-x-auto rounded-xl border border-slate-800 bg-background/50">
        <table class="w-full text-xs">
          <thead class="text-xs font-mono uppercase text-slate-400 border-b border-slate-800">
            <tr>
              <th class="px-3 py-2 text-left">{{ t("tel.tfCol") }}</th>
              <th class="px-3 py-2 text-right">{{ t("tel.roiCol") }}</th>
              <th class="px-3 py-2 text-right">{{ t("tel.pnlCol") }}</th>
              <th class="px-3 py-2 text-right">{{ t("tel.copierPnlCol") }}</th>
              <th class="px-3 py-2 text-right">{{ t("tel.mddCol") }}</th>
              <th class="px-3 py-2 text-right">{{ t("tel.winRateCol") }}</th>
              <th class="px-3 py-2 text-right">{{ t("tel.sharpeCol") }}</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-800/60 font-mono">
            <tr
              v-for="tf in availableTfs"
              :key="tf"
              @click="activeTf = tf"
              class="cursor-pointer transition-colors hover:bg-slate-800/30"
              :class="selectedTf === tf ? 'bg-accent/5' : ''"
            >
              <td class="px-3 py-1.5 font-semibold" :class="selectedTf === tf ? 'text-accent' : 'text-slate-200'">
                {{ tf }}
              </td>
              <td class="px-3 py-1.5 text-right" :class="pnlColor(perf[tf]?.roi)">
                {{ fmtPct(perf[tf]?.roi) }}
              </td>
              <td class="px-3 py-1.5 text-right" :class="pnlColor(perf[tf]?.pnl)">
                {{ fmtUsd(perf[tf]?.pnl, 2, true) }}
              </td>
              <td class="px-3 py-1.5 text-right" :class="pnlColor(perf[tf]?.copierPnl)">
                {{ fmtUsd(perf[tf]?.copierPnl, 2, true) }}
              </td>
              <td class="px-3 py-1.5 text-right text-amber-400">
                {{ perf[tf]?.mdd != null ? fmtPct(perf[tf]?.mdd, 2, false) : "—" }}
              </td>
              <td class="px-3 py-1.5 text-right text-slate-300">
                {{ perf[tf]?.winRate != null ? fmtPct(perf[tf]?.winRate, 1, false) : "—" }}
                <span class="text-slate-400 text-xs">({{ perf[tf]?.winOrders ?? 0 }}/{{ perf[tf]?.totalOrder ?? 0 }})</span>
              </td>
              <td class="px-3 py-1.5 text-right text-slate-200">
                {{ perf[tf]?.sharpRatio ?? "—" }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 2. Open Positions, Risk Bar & PA/SMC Cards -->
    <div class="rounded-2xl bg-surface border border-slate-800/90 p-5">
      <div class="flex flex-wrap items-center justify-between gap-2 mb-4">
        <div class="flex items-center gap-2.5">
          <h3 class="text-base font-semibold text-white">
            {{ t("tel.openSectionTitle") }}
          </h3>
          <span
            class="px-2 py-0.5 rounded-full text-xs font-mono border"
            :class="
              openMeta.source === 'api'
                ? 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30'
                : 'bg-cyan-500/10 text-cyan-300 border-cyan-500/30'
            "
          >
            {{ openMeta.source === "api" ? t("tel.srcApi") : t("tel.srcDerived") }}
          </span>
        </div>
        <span class="px-2.5 py-0.5 rounded-full text-sm font-mono font-semibold border" :class="riskInfo.cls">
          {{ t(riskInfo.key) }}
        </span>
      </div>

      <!-- 6-Tile Account Leverage & Margin Risk Summary -->
      <div class="grid grid-cols-[repeat(auto-fit,minmax(min(100%,220px),1fr))] gap-4 mb-5">
        <div class="min-w-0 p-4 rounded-xl bg-background/80 border border-slate-800">
          <div class="text-sm font-medium uppercase text-slate-300">{{ t("tel.posStatus") }}</div>
          <div class="text-sm font-semibold mt-1" :class="summary.positionCount ? 'text-white' : 'text-slate-400'">
            {{ holdingStatusText }}
          </div>
          <div class="text-sm text-slate-300 mt-0.5">{{ holdingExposureNote }}</div>
        </div>

        <div class="min-w-0 p-4 rounded-xl bg-background/80 border border-slate-800">
          <div class="text-sm font-medium uppercase text-slate-300">{{ t("tel.tileMarginBal") }}</div>
          <div class="text-xl font-mono font-bold text-white mt-1">
            {{ fmtUsd(summary.marginBalance ?? profile.marginBalance, 2) }}
          </div>
          <div class="text-sm font-mono text-slate-400 mt-0.5">
            {{ t("tel.tileAvail", { val: fmtUsd(summary.availableMargin, 2) }) }}
          </div>
        </div>

        <div class="min-w-0 p-4 rounded-xl bg-background/80 border border-slate-800">
          <div class="text-sm font-medium uppercase text-slate-300">{{ t("tel.tileTotalMark") }}</div>
          <div class="text-xl font-mono font-bold text-white mt-1">
            {{ fmtUsd(summary.totalMarkNotional, 2) }}
          </div>
          <div class="text-sm font-mono text-slate-400 mt-0.5">
            {{ t("tel.tileEntryNotional", { val: fmtUsd(summary.totalEntryNotional, 2) }) }}
          </div>
        </div>

        <div class="min-w-0 p-4 rounded-xl bg-background/80 border border-slate-800">
          <div class="text-sm font-medium uppercase text-slate-300">{{ t("tel.tileNotionalLev") }}</div>
          <div class="text-xl font-mono font-bold text-accent mt-1">
            {{ fmtLev(summary.totalNotionalLeverage) }}
          </div>
          <div class="text-sm font-mono text-slate-400 mt-0.5">
            {{ t("tel.tileMaxSingle", { val: fmtLev(summary.maxPositionLeverage) }) }}
          </div>
        </div>

        <div class="min-w-0 p-4 rounded-xl bg-background/80 border border-slate-800">
          <div class="text-sm font-medium uppercase text-slate-300">{{ t("tel.tileMmrBuffer") }}</div>
          <div class="text-xl font-mono font-bold text-white mt-1">
            {{ summary.estMaintenanceMarginRatioPct != null ? fmtPct(summary.estMaintenanceMarginRatioPct, 2, false) : "—" }}
          </div>
          <div class="text-sm font-mono text-slate-400 mt-0.5">
            {{
              t("tel.tileCovBuf", {
                cov: fmtPct(summary.marginCoveragePct, 1, false),
                buf: fmtPct(summary.adverseMoveBufferPct, 1, false),
              })
            }}
          </div>
        </div>

        <div class="min-w-0 p-4 rounded-xl bg-background/80 border border-slate-800">
          <div class="text-sm font-medium uppercase text-slate-300">{{ t("tel.tileLiveUpnl") }}</div>
          <div class="text-xl font-mono font-bold mt-1" :class="pnlColor(summary.totalUnrealizedPnl)">
            {{ fmtUsd(summary.totalUnrealizedPnl, 2, true) }}
          </div>
          <div class="text-sm font-mono mt-0.5" :class="pnlColor(summary.totalMarginRoePct)">
            {{ t("tel.tileMarginRoe", { val: fmtPct(summary.totalMarginRoePct, 2, true) }) }}
          </div>
        </div>
      </div>

      <div class="text-sm font-mono text-slate-400 mb-4 px-3 py-2 rounded-lg bg-background/60 border border-slate-800">
        {{
          t("tel.formulaNote", {
            mb: fmtUsd(summary.marginBalance ?? profile.marginBalance, 2),
            src: summary.mmrSource === "binance-official-brackets" ? t("tel.mmrOfficial") : t("tel.mmrFallback"),
          })
        }}
      </div>

      <div v-if="!openPositions.length" class="py-10 text-center text-sm text-slate-400">
        {{ t("tel.noOpenPositions") }}
      </div>

      <!-- Detailed Open Position Cards -->
      <div v-else class="space-y-4">
        <div
          v-for="p in openPositions"
          :key="posKey(p)"
          class="rounded-xl border border-slate-800 bg-background/70 p-4 sm:p-5 space-y-5"
        >
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div class="flex flex-wrap items-center gap-3">
              <span class="text-lg font-mono font-bold text-white">{{ p.symbol }}</span>
              <span
                class="px-3 py-1 rounded text-base font-mono font-bold"
                :class="
                  p.positionSide === 'SHORT'
                    ? 'bg-rose-500/20 text-rose-400 border border-rose-500/30'
                    : 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'
                "
              >
                {{ p.positionSide === "SHORT" ? t("pos.short") : t("pos.long") }}
              </span>
              <span class="text-xs font-mono text-slate-300">
                {{ formatQty(p.qty || 0) }}
              </span>
              <span class="px-1.5 py-0.5 rounded bg-surface border border-slate-800 text-sm font-medium text-slate-300">
                {{ t("pos.openedAt", { time: fmtShortTime(p.openTime) }) }}
              </span>
              <span class="px-1.5 py-0.5 rounded bg-surface border border-slate-800 text-sm font-medium text-slate-300">
                {{ t("pos.entryOrdersCount", { n: p.entryOrdersCount ?? (p.entryOrders?.length || 1) }) }}
              </span>
              <span
                v-if="p.fundingRate != null"
                class="px-1.5 py-0.5 rounded bg-surface border border-slate-800 text-sm font-medium text-slate-300"
              >
                {{ t("pos.fundingRate", { rate: (Number(p.fundingRate) * 100).toFixed(4) + "%" }) }}
              </span>
            </div>

            <button
              v-if="p.entryOrders?.length"
              type="button"
              @click="toggleEntries(p)"
              class="px-2.5 py-1 rounded-lg bg-surface hover:bg-surface2 border border-slate-700 text-xs font-mono text-accent cursor-pointer transition-colors"
            >
              {{ expandedEntries[posKey(p)] ? t("pos.collapseEntries") : t("pos.expandEntries") }}
            </button>
          </div>

          <!-- 8 Telemetry Metrics Grid -->
          <div class="grid grid-cols-[repeat(auto-fit,minmax(min(100%,240px),1fr))] gap-3 text-sm">
            <div class="min-w-0 p-3.5 rounded-xl bg-surface/90 border border-slate-800">
              <div class="text-sm font-medium text-slate-300 flex items-center gap-1">
                <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span>
                {{ t("pos.curPrice") }}
              </div>
              <div class="text-lg font-mono font-bold text-white mt-2">{{ fmtPrice(p.currentPrice ?? p.markPrice) }}</div>
              <div class="text-sm font-mono font-semibold mt-2" :class="pnlColor(p.priceEdgePct)">
                {{ t("pos.vsAvg") }} {{ fmtPct(p.priceDiffPct) }}
              </div>
            </div>

            <div class="min-w-0 p-3.5 rounded-xl bg-surface/90 border border-slate-800">
              <div class="text-sm font-medium text-slate-300">{{ t("pos.avgEntry") }}</div>
              <div class="text-lg font-mono font-bold text-white mt-2">{{ fmtPrice(p.avgEntry) }}</div>
              <div class="text-sm font-medium text-slate-300 mt-2">
                {{ t("pos.entryVal", { val: fmtUsd(p.entryValue, 2) }) }}
              </div>
            </div>

            <div class="min-w-0 p-3.5 rounded-xl bg-surface/90 border border-slate-800">
              <div class="text-sm font-medium text-slate-300">{{ t("pos.markVal") }}</div>
              <div class="text-lg font-mono font-bold text-white mt-2">{{ fmtUsd(p.markValue, 2) }}</div>
              <div class="text-sm font-medium text-slate-300 mt-2">
                {{ t("pos.share", { pct: fmtPct(p.sharePct, 1, false) }) }}
              </div>
            </div>

            <div class="min-w-0 p-3.5 rounded-xl bg-surface/90 border border-slate-800">
              <div class="text-sm font-medium text-slate-300">{{ t("pos.notionalLev") }}</div>
              <div class="text-lg font-mono font-bold text-accent mt-2">{{ fmtLev(p.notionalLeverage) }}</div>
              <div class="text-sm font-medium text-slate-300 mt-2">
                {{ t("pos.entryLev", { val: fmtLev(p.entryLeverage) }) }}
              </div>
            </div>

            <div class="min-w-0 p-3.5 rounded-xl bg-surface/90 border border-slate-800">
              <div class="text-sm font-medium text-slate-300">{{ t("pos.mmrRate") }}</div>
              <div class="text-lg font-mono font-bold text-white mt-2">
                {{ p.maintMarginPct != null ? fmtPct(p.maintMarginPct, 2, false) : "—" }}
              </div>
              <div class="text-sm font-medium text-slate-300 mt-2">
                {{ p.mmr?.tier ? t("pos.tier", { tier: p.mmr.tier }) : p.mmr?.exact === false ? t("tel.mmrFallback") : "—" }}
              </div>
            </div>

            <div class="min-w-0 p-3.5 rounded-xl bg-surface/90 border border-slate-800">
              <div class="text-sm font-medium text-slate-300">{{ t("pos.maintMargin") }}</div>
              <div class="text-lg font-mono font-bold text-white mt-2">{{ fmtUsd(p.maintMargin, 2) }}</div>
              <div class="text-sm font-medium text-slate-300 mt-2">
                {{ t("pos.pctOfMb", { pct: fmtPct(p.maintMarginRoePct, 2, false) }) }}
              </div>
            </div>

            <div class="min-w-0 p-3.5 rounded-xl border" :class="p.uPnL > 0 ? 'bg-emerald-500/10 border-emerald-500/30' : p.uPnL < 0 ? 'bg-rose-500/10 border-rose-500/30' : 'bg-surface/90 border-slate-800'">
              <div class="text-sm font-medium text-slate-300">{{ t("pos.floatPnl") }}</div>
              <div class="text-2xl font-mono font-bold mt-2" :class="pnlColor(p.uPnL)">
                {{ fmtUsd(p.uPnL, 2, true) }}
              </div>
              <div class="text-sm font-mono font-semibold mt-2" :class="pnlColor(p.uPnLRoePct)">
                ROE {{ fmtPct(p.uPnLRoePct, 2) }}
              </div>
            </div>

            <div class="min-w-0 p-3.5 rounded-xl bg-surface/90 border border-slate-800">
              <div class="text-sm font-medium text-slate-300">{{ t("pos.heldDur") }}</div>
              <div class="text-lg font-mono font-bold text-white mt-2">{{ fmtDur(p.heldMs) }}</div>
              <div class="text-sm font-medium text-slate-300 mt-2">
                {{ t("pos.lastAdd", { ago: fmtAgo(p.lastAddTime) }) }}
              </div>
            </div>
          </div>

          <!-- Expandable Entry Fills -->
          <div
            v-if="expandedEntries[posKey(p)] && p.entryOrders?.length"
            class="rounded-lg bg-surface border border-slate-800 p-3 space-y-1.5"
          >
            <div
              v-for="(eo, idx) in p.entryOrders"
              :key="idx"
              class="flex flex-wrap items-center justify-between gap-2 text-xs font-mono text-slate-300 py-1 border-b border-slate-800/60 last:border-b-0"
            >
              <span class="text-slate-400">{{ fmtTime(eo.t) }}</span>
              <span>{{ eo.dir ? dirLabel(eo.dir) : p.positionSide === "SHORT" ? t("dir.OPEN_SHORT") : t("dir.OPEN_LONG") }}</span>
              <span class="text-white font-semibold">{{ formatQty(eo.q || 0) }}</span>
              <span class="text-accent">@ {{ fmtPrice(eo.p) }}</span>
              <span class="text-slate-400">{{ fmtUsd((eo.q || 0) * (eo.p || 0), 2) }}</span>
            </div>
          </div>

          <!-- Full PA / SMC Deep Analysis Panel -->
          <div v-if="p.analysis && p.analysis.ok" class="rounded-xl bg-surface/80 border border-slate-800 p-3.5 space-y-3">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <span class="text-xs font-semibold text-white">{{ t("pa.title") }}</span>
              <div class="flex flex-wrap items-center gap-1.5 text-sm font-mono">
                <span class="px-2 py-0.5 rounded-full border font-semibold" :class="verdictBadgeClass(p.analysis.verdictLevel)">
                  {{ paVerdict(p.analysis) }}
                </span>
                <span class="px-2 py-0.5 rounded bg-background border border-slate-800 text-slate-300">
                  1H {{ paTrend1h(p.analysis) }}
                </span>
                <span class="px-2 py-0.5 rounded bg-background border border-slate-800 text-slate-300">
                  4H {{ paTrend4h(p.analysis) }}
                </span>
                <span class="px-2 py-0.5 rounded bg-background border border-slate-800 text-accent">
                  {{ paZone(p.analysis) }} · {{ t("pa.percentile", { pct: p.analysis.entryRangePct ?? 50 }) }}
                </span>
                <span class="px-2 py-0.5 rounded bg-background border border-slate-800 text-slate-300">
                  {{ paScale(p.analysis) }}
                </span>
              </div>
            </div>

            <!-- 72H Swing Percentile Bar -->
            <div class="space-y-1">
              <div class="relative h-3 rounded-full bg-background border border-slate-700 overflow-visible">
                <div
                  class="absolute inset-y-0 left-0 rounded-full bg-gradient-to-r from-emerald-500/25 via-cyan-500/20 to-rose-500/25 w-full"
                ></div>
                <div class="absolute top-0 bottom-0 left-1/2 w-px bg-slate-500/70"></div>
                <div
                  class="absolute -top-1 w-2.5 h-5 rounded-sm bg-amber-400 border border-background shadow"
                  :style="{ left: `calc(${Math.max(0, Math.min(100, p.analysis.entryRangePct ?? 50))}% - 5px)` }"
                  :title="t('pa.entryMark', { pct: p.analysis.entryRangePct })"
                ></div>
                <div
                  class="absolute -top-0.5 w-3.5 h-4 rounded-full bg-accent border-2 border-background shadow"
                  :style="{ left: `calc(${Math.max(0, Math.min(100, p.analysis.curRangePct ?? 50))}% - 7px)` }"
                  :title="t('pa.curMark', { pct: p.analysis.curRangePct })"
                ></div>
              </div>
              <div class="flex flex-wrap items-center justify-between gap-2 text-xs font-mono text-slate-400">
                <span>{{ t("pa.low72") }} {{ fmtPrice(p.analysis.low72h) }}</span>
                <span class="text-amber-400">● {{ t("pa.entryMark", { pct: p.analysis.entryRangePct }) }}</span>
                <span>{{ t("pa.eq50") }} {{ fmtPrice(p.analysis.eq72h) }}</span>
                <span class="text-accent">● {{ t("pa.curMark", { pct: p.analysis.curRangePct }) }}</span>
                <span>{{ t("pa.high72") }} {{ fmtPrice(p.analysis.high72h) }}</span>
              </div>
            </div>

            <!-- PA Key Levels & Indicators Grid -->
            <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2 text-xs font-mono">
              <div class="p-2 rounded bg-background/80 border border-slate-800/80">
                <div class="text-xs text-slate-400">{{ t("pa.keyRes") }}</div>
                <div class="text-rose-400 font-semibold mt-0.5">{{ fmtPrice(p.analysis.keyResistance) }}</div>
              </div>
              <div class="p-2 rounded bg-background/80 border border-slate-800/80">
                <div class="text-xs text-slate-400">{{ t("pa.keySup") }}</div>
                <div class="text-emerald-400 font-semibold mt-0.5">{{ fmtPrice(p.analysis.keySupport) }}</div>
              </div>
              <div class="p-2 rounded bg-background/80 border border-slate-800/80">
                <div class="text-xs text-slate-400">{{ t("pa.invalidation") }}</div>
                <div class="text-amber-300 font-semibold mt-0.5">{{ fmtPrice(p.analysis.invalidationLevel) }}</div>
              </div>
              <div class="p-2 rounded bg-background/80 border border-slate-800/80">
                <div class="text-xs text-slate-400">{{ t("pa.targets") }}</div>
                <div class="text-white font-semibold mt-0.5">
                  {{ fmtPrice(p.analysis.target1) }} / {{ fmtPrice(p.analysis.target2) }}
                  <span class="text-accent text-xs">({{ p.analysis.rrFromEntry }}R)</span>
                </div>
              </div>
              <div class="p-2 rounded bg-background/80 border border-slate-800/80">
                <div class="text-xs text-slate-400">RSI14 (1H/4H) · ATR</div>
                <div class="text-white font-semibold mt-0.5">
                  {{ p.analysis.rsi1h ?? "—" }} / {{ p.analysis.rsi4h ?? "—" }}
                  <span class="text-slate-400 text-xs">({{ p.analysis.atr1hPct }}%)</span>
                </div>
              </div>
              <div class="p-2 rounded bg-background/80 border border-slate-800/80">
                <div class="text-xs text-slate-400">{{ t("pa.emaDist") }}</div>
                <div class="text-white font-semibold mt-0.5">
                  {{ p.analysis.entryVsEma21Atr >= 0 ? "+" : "" }}{{ p.analysis.entryVsEma21Atr }} ATR
                </div>
              </div>
            </div>

            <div class="flex flex-wrap gap-2 text-sm font-mono">
              <span v-if="p.analysis.nearestBearFvg" class="px-2 py-0.5 rounded bg-rose-500/10 text-rose-400 border border-rose-500/20">
                {{ t("pa.bearFvg") }}: {{ fmtPrice(p.analysis.nearestBearFvg[0]) }} ~ {{ fmtPrice(p.analysis.nearestBearFvg[1]) }}
              </span>
              <span v-if="p.analysis.nearestBullFvg" class="px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                {{ t("pa.bullFvg") }}: {{ fmtPrice(p.analysis.nearestBullFvg[0]) }} ~ {{ fmtPrice(p.analysis.nearestBullFvg[1]) }}
              </span>
            </div>

            <p class="text-xs text-slate-300 leading-relaxed break-words">
              {{ paSummary(p.analysis) }}
            </p>
          </div>
          <div v-else class="text-xs text-slate-400 italic px-2">
            {{ t("pa.pending") }}
          </div>
        </div>
      </div>
    </div>

    <!-- 2.5 Position Sizing & Dynamic Leverage Calculator -->
    <PositionCalculatorCard
      :snapshot="snapshot"
      :open-positions="openPositions"
      :open-positions-summary="summary"
      :lead-margin-balance="summary.marginBalance ?? profile.marginBalance"
      :display-name="displayName || profile.nickname"
    />

    <!-- 3. 365D ROI Curve + 90D Asset Allocation -->
    <div class="grid grid-cols-1 lg:grid-cols-3 gap-5">
      <div class="lg:col-span-2 rounded-2xl bg-surface border border-slate-800/90 p-5">
        <div class="flex items-center justify-between mb-3">
          <h3 class="text-base font-semibold text-white">{{ t("chart.title") }}</h3>
          <div v-if="chartSvg" class="flex items-center gap-3 text-xs font-mono">
            <span class="text-slate-400">{{ t("chart.peak", { val: fmtPct(chartSvg.peak) }) }}</span>
            <span :class="chartSvg.positive ? 'text-emerald-400 font-semibold' : 'text-rose-400 font-semibold'">
              {{ t("chart.current", { val: fmtPct(chartSvg.last) }) }}
            </span>
          </div>
        </div>

        <div v-if="chartSvg" class="w-full overflow-hidden">
          <svg :viewBox="`0 0 ${chartSvg.W} ${chartSvg.H}`" class="w-full h-48">
            <defs>
              <linearGradient id="roiGrad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" :stop-color="chartSvg.positive ? '#10b981' : '#ef4444'" stop-opacity="0.35" />
                <stop offset="100%" :stop-color="chartSvg.positive ? '#10b981' : '#ef4444'" stop-opacity="0.0" />
              </linearGradient>
            </defs>
            <g v-for="(tk, i) in chartSvg.ticks" :key="i">
              <line
                :x1="chartSvg.P.l"
                :x2="chartSvg.W - chartSvg.P.r"
                :y1="tk.y"
                :y2="tk.y"
                stroke="#1e293b"
                stroke-dasharray="3,3"
              />
              <text :x="chartSvg.P.l - 6" :y="tk.y + 3" text-anchor="end" fill="#64748b" font-size="10" font-family="monospace">
                {{ tk.label }}
              </text>
            </g>
            <line
              v-if="chartSvg.zeroY >= chartSvg.P.t && chartSvg.zeroY <= chartSvg.H - chartSvg.P.b"
              :x1="chartSvg.P.l"
              :x2="chartSvg.W - chartSvg.P.r"
              :y1="chartSvg.zeroY"
              :y2="chartSvg.zeroY"
              stroke="#475569"
              stroke-width="1"
            />
            <path :d="chartSvg.area" fill="url(#roiGrad)" />
            <path
              :d="chartSvg.line"
              fill="none"
              :stroke="chartSvg.positive ? '#10b981' : '#ef4444'"
              stroke-width="2"
              stroke-linecap="round"
            />
            <circle
              :cx="chartSvg.lastX"
              :cy="chartSvg.lastY"
              r="3.5"
              :fill="chartSvg.positive ? '#10b981' : '#ef4444'"
            />
            <text :x="chartSvg.P.l" :y="chartSvg.H - 4" fill="#64748b" font-size="10" font-family="monospace">
              {{ chartSvg.firstDate }}
            </text>
            <text :x="chartSvg.W - chartSvg.P.r" :y="chartSvg.H - 4" text-anchor="end" fill="#64748b" font-size="10" font-family="monospace">
              {{ chartSvg.lastDate }}
            </text>
          </svg>
        </div>
        <div v-else class="py-12 text-center text-xs text-slate-400">{{ t("chart.noData") }}</div>
      </div>

      <div class="rounded-2xl bg-surface border border-slate-800/90 p-5">
        <div class="flex items-center justify-between mb-3">
          <h3 class="text-base font-semibold text-white">{{ t("alloc.title") }}</h3>
          <span v-if="snapshot?.allocation?.updateTime" class="text-xs font-mono text-slate-400">
            {{ t("alloc.updated", { time: fmtShortTime(snapshot.allocation.updateTime) }) }}
          </span>
        </div>
        <div v-if="allocSlices.length" class="space-y-3">
          <div class="h-3 rounded-full overflow-hidden flex bg-background">
            <div
              v-for="s in allocSlices"
              :key="s.asset"
              :style="{ width: `${s.pct}%`, backgroundColor: s.color }"
              :title="`${s.asset}: ${s.pct.toFixed(1)}%`"
            ></div>
          </div>
          <div class="space-y-2">
            <div v-for="s in allocSlices" :key="s.asset" class="flex items-center justify-between text-xs font-mono">
              <div class="flex items-center gap-2">
                <span class="w-2.5 h-2.5 rounded-sm" :style="{ backgroundColor: s.color }"></span>
                <span class="font-semibold text-slate-100">{{ s.asset }}</span>
              </div>
              <span class="text-slate-400">{{ s.pct.toFixed(1) }}%</span>
            </div>
          </div>
        </div>
        <div v-else class="py-12 text-center text-xs text-slate-400">{{ t("alloc.noData") }}</div>
      </div>
    </div>

    <!-- 4. Tabbed Operation Feeds (30m Batches / Trade Cycles / Closed Positions / Raw Orders) -->
    <div class="rounded-2xl bg-surface border border-slate-800/90 overflow-hidden">
      <div class="px-5 pt-4 pb-3 border-b border-slate-800 flex flex-wrap items-center justify-between gap-2">
        <div class="flex flex-wrap gap-1.5">
          <button
            v-for="tabId in (['batches', 'cycles', 'closed', 'orders'] as const)"
            :key="tabId"
            type="button"
            @click="activeTab = tabId"
            class="px-3 py-1.5 rounded-lg text-xs font-medium transition-colors cursor-pointer"
            :class="
              activeTab === tabId
                ? 'bg-accent/15 text-accent border border-accent/30'
                : 'text-slate-400 hover:text-white hover:bg-slate-800/50'
            "
          >
            {{ t(`tab.${tabId}`) }}
            <span class="ml-1 font-mono text-xs opacity-80">
              ({{
                tabId === "batches"
                  ? orderBatches.length
                  : tabId === "cycles"
                  ? tradeCycles.length
                  : tabId === "closed"
                  ? closedList.length
                  : ordersList.length
              }})
            </span>
          </button>
        </div>
        <slot name="tab-actions"></slot>
      </div>

      <!-- TAB 1: 30m Order Batches -->
      <div v-if="activeTab === 'batches'">
        <div class="px-5 py-2 text-sm text-slate-400 bg-background/40 border-b border-slate-800/70">
          {{ t("feed.batchNote") }}
        </div>
        <div v-if="!orderBatches.length" class="p-8 text-center text-sm text-slate-400">
          {{ t("feed.noBatches") }}
        </div>
        <div v-else class="overflow-x-auto">
          <table class="w-full text-xs">
            <thead class="text-xs font-mono uppercase text-slate-400 border-b border-slate-800 bg-background/30">
              <tr>
                <th class="px-4 py-2.5 text-left">{{ t("feed.colTime") }}</th>
                <th class="px-4 py-2.5 text-left">{{ t("feed.colDirSym") }}</th>
                <th class="px-4 py-2.5 text-right">{{ t("feed.colAvgPx") }}</th>
                <th class="px-4 py-2.5 text-right">{{ t("feed.colQty") }}</th>
                <th class="px-4 py-2.5 text-right">{{ t("feed.colNotional") }}</th>
                <th class="px-4 py-2.5 text-right">{{ t("feed.colOrders") }}</th>
                <th class="px-4 py-2.5 text-right">{{ t("feed.colRpnl") }}</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-800/60 font-mono">
              <template v-for="(b, idx) in orderBatches" :key="idx">
                <tr @click="toggleBatch(idx)" class="hover:bg-slate-800/30 cursor-pointer transition-colors">
                  <td class="px-4 py-2.5 text-slate-400 whitespace-nowrap">{{ fmtTime(b.endTime || b.startTime || b.time) }}</td>
                  <td class="px-4 py-2.5">
                    <span class="px-1.5 py-0.5 rounded text-xs font-semibold mr-1.5" :class="dirBadgeClass(b.dir)">
                      {{ dirLabel(b.dir) }}
                    </span>
                    <span class="font-bold text-white">{{ b.symbol }}</span>
                  </td>
                  <td class="px-4 py-2.5 text-right text-slate-200">{{ fmtPrice(b.avgPrice) }}</td>
                  <td class="px-4 py-2.5 text-right text-slate-300">
                    {{ formatQty(b.qty ?? b.totalQty ?? 0) }}
                  </td>
                  <td class="px-4 py-2.5 text-right text-slate-200">{{ fmtUsd(b.notional ?? b.totalValue, 2) }}</td>
                  <td class="px-4 py-2.5 text-right text-accent">
                    {{ b.orderCount ?? b.count ?? (Array.isArray(b.orders) ? b.orders.length : b.orders) ?? 1 }} {{ expandedBatches[idx] ? "▴" : "▾" }}
                  </td>
                  <td class="px-4 py-2.5 text-right font-semibold" :class="pnlColor(b.pnl ?? b.totalPnl)">
                    {{ (b.pnl ?? b.totalPnl) ? fmtUsd(b.pnl ?? b.totalPnl, 2, true) : "—" }}
                  </td>
                </tr>
                <tr v-if="expandedBatches[idx] && (b.items?.length || (Array.isArray(b.orders) && b.orders.length))" class="bg-background/80">
                  <td colspan="7" class="px-6 py-2">
                    <div class="space-y-1 text-sm">
                      <div
                        v-for="(co, cidx) in (b.items || b.orders)"
                        :key="cidx"
                        class="flex items-center justify-between text-slate-300 py-0.5 border-b border-slate-800/40 last:border-b-0"
                      >
                        <span class="text-slate-400">{{ fmtTime(co.time ?? co.t) }}</span>
                        <span>{{ co.ty || b.type || "MARKET" }}</span>
                        <span>{{ formatQty(co.qty ?? co.q ?? 0) }} @ {{ fmtPrice(co.price ?? co.p) }}</span>
                        <span :class="pnlColor(co.pnl)">{{ co.pnl ? fmtUsd(co.pnl, 2, true) : "—" }}</span>
                      </div>
                    </div>
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
        </div>
      </div>

      <!-- TAB 2: Historical Trade Cycles -->
      <div v-if="activeTab === 'cycles'">
        <div v-if="!tradeCycles.length" class="p-8 text-center text-sm text-slate-400">
          {{ t("feed.noCycles") }}
        </div>
        <div v-else class="overflow-x-auto">
          <table class="w-full text-xs">
            <thead class="text-xs font-mono uppercase text-slate-400 border-b border-slate-800 bg-background/30">
              <tr>
                <th class="px-4 py-2.5 text-left">{{ t("feed.cycleId") }}</th>
                <th class="px-4 py-2.5 text-left">{{ t("feed.colDirSym") }}</th>
                <th class="px-4 py-2.5 text-left">{{ t("feed.cycleStatus") }}</th>
                <th class="px-4 py-2.5 text-left">{{ t("feed.cycleEntryExit") }}</th>
                <th class="px-4 py-2.5 text-right">{{ t("feed.cycleCounts") }}</th>
                <th class="px-4 py-2.5 text-right">{{ t("feed.cycleDur") }}</th>
                <th class="px-4 py-2.5 text-right">{{ t("feed.cyclePnl") }}</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-800/60 font-mono">
              <tr v-for="c in tradeCycles.slice(0, cyclesLimit)" :key="c.cycleId" class="hover:bg-slate-800/30">
                <td class="px-4 py-2.5 text-slate-400">{{ c.cycleId }}</td>
                <td class="px-4 py-2.5">
                  <span
                    class="px-1.5 py-0.5 rounded text-xs font-bold mr-1.5"
                    :class="c.positionSide === 'SHORT' ? 'bg-rose-500/20 text-rose-400' : 'bg-emerald-500/20 text-emerald-400'"
                  >
                    {{ c.positionSide }}
                  </span>
                  <span class="font-bold text-white">{{ c.symbol }}</span>
                </td>
                <td class="px-4 py-2.5">
                  <span
                    class="px-2 py-0.5 rounded-full text-xs"
                    :class="c.status === 'OPEN' ? 'bg-accent/15 text-accent' : 'bg-slate-800 text-slate-300'"
                  >
                    {{ c.status === "OPEN" ? t("feed.openStatus") : t("feed.closedStatus") }}
                  </span>
                </td>
                <td class="px-4 py-2.5 text-slate-200">
                  {{ fmtPrice(c.avgEntryPrice ?? c.avgEntry) }} → {{ (c.avgExitPrice || c.avgClose) ? fmtPrice(c.avgExitPrice ?? c.avgClose) : t("feed.holding") }}
                </td>
                <td class="px-4 py-2.5 text-right text-slate-300">
                  {{ c.entryCount ?? c.entryOrdersCount ?? 0 }} / {{ c.scaleInCount ?? 0 }} / {{ c.exitCount ?? c.exitOrdersCount ?? 0 }}
                </td>
                <td class="px-4 py-2.5 text-right text-slate-400">{{ fmtDur(c.durationMs ?? (c.durationSec != null ? c.durationSec * 1000 : null)) }}</td>
                <td class="px-4 py-2.5 text-right font-semibold" :class="pnlColor(c.realizedPnl)">
                  {{ c.realizedPnl ? fmtUsd(c.realizedPnl, 2, true) : "—" }}
                </td>
              </tr>
            </tbody>
          </table>
          <div v-if="tradeCycles.length > 15" class="p-3 text-center border-t border-slate-800/60">
            <button
              type="button"
              @click="cyclesLimit = cyclesLimit >= tradeCycles.length ? 15 : tradeCycles.length"
              class="text-xs font-mono text-accent hover:underline cursor-pointer"
            >
              {{
                cyclesLimit >= tradeCycles.length
                  ? t("feed.showLess")
                  : t("feed.showMore", { shown: cyclesLimit, total: tradeCycles.length })
              }}
            </button>
          </div>
        </div>
      </div>

      <!-- TAB 3: Official Closed Positions -->
      <div v-if="activeTab === 'closed'">
        <div v-if="!closedList.length" class="p-8 text-center text-sm text-slate-400">
          {{ t("feed.noClosed") }}
        </div>
        <div v-else class="overflow-x-auto">
          <table class="w-full text-xs">
            <thead class="text-xs font-mono uppercase text-slate-400 border-b border-slate-800 bg-background/30">
              <tr>
                <th class="px-4 py-2.5 text-left">{{ t("feed.closedTime") }}</th>
                <th class="px-4 py-2.5 text-left">{{ t("feed.colDirSym") }}</th>
                <th class="px-4 py-2.5 text-left">{{ t("feed.closedLev") }}</th>
                <th class="px-4 py-2.5 text-right">{{ t("feed.closedAvgEntry") }}</th>
                <th class="px-4 py-2.5 text-right">{{ t("feed.closedAvgClose") }}</th>
                <th class="px-4 py-2.5 text-right">{{ t("feed.closedMaxQty") }}</th>
                <th class="px-4 py-2.5 text-right">{{ t("feed.closedPnl") }}</th>
                <th class="px-4 py-2.5 text-right">{{ t("feed.closedHeld") }}</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-800/60 font-mono">
              <tr v-for="(c, idx) in closedList.slice(0, closedLimit)" :key="idx" class="hover:bg-slate-800/30">
                <td class="px-4 py-2.5 text-slate-400 whitespace-nowrap">{{ fmtTime(c.closed) }}</td>
                <td class="px-4 py-2.5">
                  <span
                    class="px-1.5 py-0.5 rounded text-xs font-bold mr-1.5"
                    :class="c.side === 'Short' ? 'bg-rose-500/20 text-rose-400' : 'bg-emerald-500/20 text-emerald-400'"
                  >
                    {{ c.side?.toUpperCase() }}
                  </span>
                  <span class="font-bold text-white">{{ c.symbol }}</span>
                </td>
                <td class="px-4 py-2.5 text-slate-400">
                  {{ c.leverage }}x · {{ c.isolated === true || c.isolated === 'Isolated' ? t("feed.isolated") : t("feed.cross") }}
                </td>
                <td class="px-4 py-2.5 text-right text-slate-300">{{ fmtPrice(c.avgCost) }}</td>
                <td class="px-4 py-2.5 text-right text-white">{{ fmtPrice(c.avgClosePrice ?? c.avgClose) }}</td>
                <td class="px-4 py-2.5 text-right text-slate-300">
                  {{ formatQty(c.maxQty ?? c.closedVol ?? c.maxOpen ?? 0) }}
                </td>
                <td class="px-4 py-2.5 text-right font-semibold" :class="pnlColor(c.pnl)">
                  {{ fmtUsd(c.pnl, 2, true) }}
                </td>
                <td class="px-4 py-2.5 text-right text-slate-400">
                  {{ c.heldMs != null ? fmtDur(c.heldMs) : c.opened && c.closed ? fmtDur(c.closed - c.opened) : "—" }}
                </td>
              </tr>
            </tbody>
          </table>
          <div v-if="closedList.length > 15" class="p-3 text-center border-t border-slate-800/60">
            <button
              type="button"
              @click="closedLimit = closedLimit >= closedList.length ? 15 : closedList.length"
              class="text-xs font-mono text-accent hover:underline cursor-pointer"
            >
              {{
                closedLimit >= closedList.length
                  ? t("feed.showLess")
                  : t("feed.showMore", { shown: closedLimit, total: closedList.length })
              }}
            </button>
          </div>
        </div>
      </div>

      <!-- TAB 4: Raw Order Feed -->
      <div v-if="activeTab === 'orders'">
        <div v-if="!ordersList.length" class="p-8 text-center text-sm text-slate-400">
          {{ t("feed.noOrders") }}
        </div>
        <div v-else class="overflow-x-auto max-h-96 overflow-y-auto">
          <table class="w-full text-xs">
            <thead class="text-xs font-mono uppercase text-slate-400 border-b border-slate-800 bg-background/80 sticky top-0">
              <tr>
                <th class="px-4 py-2.5 text-left">{{ t("feed.colTime") }}</th>
                <th class="px-4 py-2.5 text-left">{{ t("feed.colDirSym") }}</th>
                <th class="px-4 py-2.5 text-right">{{ t("feed.colAvgPx") }}</th>
                <th class="px-4 py-2.5 text-right">{{ t("feed.colQty") }}</th>
                <th class="px-4 py-2.5 text-right">{{ t("feed.colNotional") }}</th>
                <th class="px-4 py-2.5 text-right">{{ t("feed.colRpnl") }}</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-800/50 font-mono">
              <tr v-for="(o, idx) in ordersList.slice(0, 80)" :key="idx" class="hover:bg-slate-800/30">
                <td class="px-4 py-2 text-slate-400 whitespace-nowrap">{{ fmtTime(o.time) }}</td>
                <td class="px-4 py-2">
                  <span class="px-1.5 py-0.5 rounded text-xs font-semibold mr-1.5" :class="dirBadgeClass(o.dir)">
                    {{ dirLabel(o.dir) }}
                  </span>
                  <span class="font-bold text-white">{{ o.symbol }}</span>
                </td>
                <td class="px-4 py-2 text-right text-slate-200">{{ fmtPrice(o.avgPrice) }}</td>
                <td class="px-4 py-2 text-right text-slate-300">
                  {{ formatQty(o.qty || 0) }}
                </td>
                <td class="px-4 py-2 text-right text-slate-200">{{ fmtUsd((o.qty || 0) * (o.avgPrice || 0), 2) }}</td>
                <td class="px-4 py-2 text-right font-semibold" :class="pnlColor(o.pnl)">
                  {{ o.pnl ? fmtUsd(o.pnl, 2, true) : "—" }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</template>
