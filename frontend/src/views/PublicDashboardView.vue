<script setup lang="ts">
import { formatClock, formatPrice, signedUsd, timestampMs } from "@/utils/format";
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref } from "vue";
import { Lock, Radar, RefreshCw, Volume2, VolumeX } from "lucide-vue-next";
import { api, ApiError, isCancelled } from "@/api/client";
import { useI18n } from "@/i18n";
import LangToggle from "@/components/LangToggle.vue";
import ThemeToggle from "@/components/ThemeToggle.vue";
import LeadAvatar from "@/components/LeadAvatar.vue";
import LeadTelemetryPanel from "@/components/LeadTelemetryPanel.vue";
import LeadTimeline from "@/components/board/LeadTimeline.vue";
import FreshnessStamp from "@/components/FreshnessStamp.vue";
import EmptyState from "@/components/EmptyState.vue";
import { useAuthStore } from "@/stores/auth";
import { useNow } from "@/composables/useNow";
import { errorMessage } from "@/utils/errors";
import type { BoardEvent, Lead, PublicDashboardResponse } from "@/types/api";
import ErrorState from "@/components/ErrorState.vue";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";
import { publicBoardLead } from "@/utils/publicBoard";
import { positionsConfirmed } from "@/utils/positions";
import { actionLabel, pnlClass, sideLabel } from "@/utils/status";
import { MOCK_MODE } from "@/api/mode";

defineOptions({ name: "PublicDashboardView" });
const authStore = useAuthStore();

const { t } = useI18n();
const now = useNow();
const POLL_MS = 5000;

const loading = ref(true);
const enabled = ref<boolean | null>(null);
const leads = ref<Lead[]>([]);
const receivedAt = ref<number | null>(null);
const error = ref("");
// 下一次自动轮询的时刻；手动刷新不改变它，倒计时始终对应真实的轮询节奏。
const nextPollAt = ref<number | null>(null);
const countdownSec = computed(() => nextPollAt.value == null ? null
  : Math.min(POLL_MS / 1000, Math.max(0, Math.ceil((nextPollAt.value - now.value) / 1000))));
// 公开投影只在收到新数据时重算；“今天”的上界取收到数据的时刻，而不是每秒跳动的时钟。
const rows = computed(() => {
  const at = receivedAt.value ?? Date.now();
  return leads.value.map(lead => {
    const board = publicBoardLead(lead, at);
    // 最新动作按时间取，不依赖接口返回的顺序。
    const latest = board.todayEvents?.reduce<BoardEvent | null>((best, item) => best == null || (item.time ?? 0) > (best.time ?? 0) ? item : best, null) ?? null;
    return {
      lead, board, latest,
      name: lead.name || lead.snapshot?.profile?.nickname || lead.portfolioId || t("common.unknown"),
      pnl: lead.snapshot?.openPositionsSummary?.totalUnrealizedPnl ?? null,
    };
  });
});
// 隐藏或未知仓位不等于空仓：只有确认过的持仓列表才报数量。
function positionsText(lead: Lead) {
  if (!positionsConfirmed(lead.snapshot)) return t("pub.positionsUnknown");
  const count = lead.snapshot?.openPositions?.length ?? 0;
  return count ? t("pub.positionsCount", { n: count }) : t("tel.flatStatus");
}
function sideClass(side?: string | null) { return side === "LONG" ? "side-long" : side === "SHORT" ? "side-short" : "side-unknown"; }
// 速览卡片就是切换器：点谁就只看谁的完整分析，页面不滚动；选择记在本地，刷新后保持。
function savedLead() {
  try { const id = Number(localStorage.getItem("mq_pub_lead")); return Number.isInteger(id) && id > 0 ? id : null; } catch { return null; }
}
const selectedId = ref<number | null>(savedLead());
// 选中的带单员不在列表里（被下线或首次打开）时回落到第一位。
const activeId = computed(() => rows.value.some(row => row.lead.id === selectedId.value) ? selectedId.value : rows.value[0]?.lead.id ?? null);
function selectLead(id: number) {
  selectedId.value = id;
  try { localStorage.setItem("mq_pub_lead", String(id)); } catch { /* 隐私模式下不持久化，不影响切换。 */ }
}
function savedSound() {
  try { return localStorage.getItem("mq_pub_sound") === "1"; } catch { return false; }
}
const soundEnabled = ref(savedSound());
const lastOrderTimestamps: Record<number, number> = {};
let pollTimer: ReturnType<typeof setInterval> | undefined;
let audioContext: AudioContext | null = null;
let sequence = 0;

function playPing() {
  if (!soundEnabled.value) return;
  try {
    const AudioCtx = window.AudioContext || (window as Window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!AudioCtx) return;
    // 复用音频上下文，并在离开页面时关闭，不为每次提示音泄漏资源。
    const ctx = audioContext ??= new AudioCtx();
    if (ctx.state === "suspended") void ctx.resume().catch(() => {});
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.setValueAtTime(740, ctx.currentTime);
    osc.frequency.exponentialRampToValueAtTime(1046, ctx.currentTime + 0.14);
    gain.gain.setValueAtTime(0.05, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.25);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.26);
    osc.onended = () => { osc.disconnect(); gain.disconnect(); };
  } catch {
    // 浏览器禁止自动播放时，仍保留可见的动态记录。
  }
}

function toggleSound() {
  soundEnabled.value = !soundEnabled.value;
  try {
    localStorage.setItem("mq_pub_sound", soundEnabled.value ? "1" : "0");
  } catch {
    // 存储受限不影响本次开关。
  }
  if (soundEnabled.value) playPing();
}

async function fetchPublic() {
  const request = ++sequence;
  loading.value = true;
  try {
    const res = await api.get<PublicDashboardResponse>("/api/dashboard/public");
    if (request !== sequence) return;
    if (res.enabled !== true && res.enabled !== false) throw new ApiError(502, "INVALID_RESPONSE");
    if (res.enabled && !Array.isArray(res.leads)) throw new ApiError(502, "INVALID_RESPONSE");
    enabled.value = res.enabled;
    leads.value = res.enabled ? res.leads! : [];
    receivedAt.value = Date.now();
    error.value = "";

    for (const l of leads.value) {
      const latest = l.snapshot?.profile?.lastTradeTime || 0;
      if (lastOrderTimestamps[l.id] && latest > lastOrderTimestamps[l.id]) {
        playPing();
      }
      if (latest) lastOrderTimestamps[l.id] = latest;
    }
  } catch (cause: unknown) {
    if (request === sequence && !isCancelled(cause)) error.value = errorMessage(cause);
  } finally {
    if (request === sequence) loading.value = false;
  }
}

// 信标三态：实时、延迟、中断；色调直接给徽标用。
const beaconStatus = computed(() => {
  if (receivedAt.value == null || enabled.value == null) return { key: "pub.beaconStale", tone: "tone-warning" };
  if (error.value || leads.value.some(lead => lead.stale === true || lead.snapshot?.meta?.stale === true)) {
    return { key: "pub.beaconOff", tone: "tone-danger" };
  }
  const ages = leads.value.map(lead => {
    if (lead.snapshotAgeSec != null && receivedAt.value != null) return lead.snapshotAgeSec + (now.value - receivedAt.value) / 1000;
    const generated = timestampMs(lead.snapshot?.meta?.generatedAt);
    return generated == null ? null : (now.value - generated) / 1000;
  });
  if ((now.value - receivedAt.value) / 1000 >= 45 || ages.some(age => age != null && age >= 90)) return { key: "pub.beaconOff", tone: "tone-danger" };
  if ((now.value - receivedAt.value) / 1000 >= 15 || ages.some(age => age == null || age >= 30)) return { key: "pub.beaconStale", tone: "tone-warning" };
  return { key: "pub.beaconLive", tone: "tone-success" };
});

function startPolling() {
  if (pollTimer) return;
  void fetchPublic();
  nextPollAt.value = Date.now() + POLL_MS;
  pollTimer = setInterval(() => {
    nextPollAt.value = Date.now() + POLL_MS;
    if (!loading.value) void fetchPublic();
  }, POLL_MS);
}

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = undefined;
  nextPollAt.value = null;
  sequence++;
  loading.value = false;
  if (audioContext) { void audioContext.close().catch(() => {}); audioContext = null; }
}

onMounted(startPolling);
onActivated(startPolling);
onDeactivated(stopPolling);
onUnmounted(stopPolling);
</script>

<template>
  <div class="min-w-0 space-y-4" :class="authStore.authenticated ? '' : 'mx-auto min-h-dvh max-w-[1800px] p-3 md:p-6'">
    <!-- 顶栏：标题旁就是监控信标，右侧是倒计时与开关；异常横幅始终排在持仓之前。 -->
    <header class="page-heading">
      <div class="flex min-w-0 flex-1 items-center gap-3">
        <span v-if="!authStore.authenticated" class="brand-mark" aria-hidden="true"><Radar :size="19" /></span>
        <div class="min-w-0">
          <div class="flex flex-wrap items-center gap-x-2.5 gap-y-1">
            <h1 class="page-title">{{ t("pub.title") }}</h1>
            <span v-if="enabled !== false" class="pill font-semibold" :class="beaconStatus.tone" role="status">{{ t(beaconStatus.key) }}</span>
            <span v-if="MOCK_MODE" class="pill tone-warning">{{ t("board.mock") }}</span>
          </div>
          <p class="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted">
            <span data-testid="page-summary">{{ t("pub.summary", { n: receivedAt == null ? "—" : leads.length }) }}</span>
            <span class="text-xs">{{ t("app.beijingTime") }}</span>
            <span v-if="enabled !== false" class="inline-flex items-center gap-1.5 text-xs">{{ t("board.updated") }}<FreshnessStamp :timestamp="receivedAt" :warn-after="15" :critical-after="45" :stale="!!error" /></span>
          </p>
          <p v-if="!authStore.authenticated" class="mt-0.5 hidden text-xs text-faint md:block">{{ t("pub.subtitle") }}</p>
        </div>
      </div>
      <div class="flex shrink-0 flex-wrap items-center gap-1">
        <span v-if="countdownSec != null" class="pill mr-1 font-mono tabular-nums">{{ t("pub.countdown", { n: countdownSec }) }}</span>
        <template v-if="!authStore.authenticated"><ThemeToggle /><LangToggle /></template>
        <button type="button" class="icon-button" :title="soundEnabled ? t('pub.soundOn') : t('pub.soundOff')" :aria-label="soundEnabled ? t('pub.soundOn') : t('pub.soundOff')" :aria-pressed="soundEnabled" @click="toggleSound">
          <Volume2 v-if="soundEnabled" :size="20" aria-hidden="true" /><VolumeX v-else :size="20" aria-hidden="true" />
        </button>
        <button type="button" class="icon-button" :disabled="loading" :title="t('common.refresh')" :aria-label="t('common.refresh')" @click="fetchPublic"><RefreshCw :size="20" :class="{ 'animate-spin': loading }" aria-hidden="true" /></button>
        <RouterLink v-if="!authStore.authenticated" to="/login" class="button-secondary ml-1"><Lock :size="16" aria-hidden="true" />{{ t("pub.console") }}</RouterLink>
      </div>
    </header>
    <ErrorState v-if="error" :message="error" :busy="loading" @retry="fetchPublic" />
    <LoadingSkeleton v-if="loading && receivedAt == null" />
    <EmptyState v-else-if="enabled === false" :title="t('pub.disabledTitle')" :description="t('pub.disabledDesc')" />
    <EmptyState v-else-if="receivedAt != null && !leads.length" :title="t('pub.empty')" />
    <template v-else>
      <!-- 速览兼切换：每位带单员一张卡，持仓、浮盈与最新动作一眼看完；点击后下方只显示这位带单员的完整分析。 -->
      <nav v-if="rows.length > 1" class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3" :aria-label="t('pub.overview')">
        <button v-for="row in rows" :key="row.lead.id" type="button" class="card block w-full p-3.5 text-left transition"
          :class="row.lead.id === activeId ? 'border-accent bg-accent/5 ring-1 ring-accent/40' : 'hover:border-accent/50 hover:bg-surface2/40'"
          :aria-pressed="row.lead.id === activeId" :title="t('pub.jumpToLead', { name: row.name })" @click="selectLead(row.lead.id)">
          <span class="flex min-w-0 items-center gap-3">
            <LeadAvatar :name="row.name" :url="row.board.avatarUrl" :size="40" />
            <span class="min-w-0 flex-1">
              <strong class="block truncate text-sm">{{ row.name }}</strong>
              <span class="mt-0.5 block text-xs text-muted">{{ positionsText(row.lead) }}</span>
            </span>
            <span class="shrink-0 text-right">
              <span class="block text-xs text-muted">{{ t("pub.openPnl") }}</span>
              <strong class="block font-mono text-base tabular-nums" :class="pnlClass(row.pnl)">{{ signedUsd(row.pnl) }}</strong>
            </span>
          </span>
          <!-- 每个持仓一行：方向、均价 → 现价、浮盈，不用往下翻就能看到带单员手里有什么。 -->
          <span v-if="row.board.positions?.length" class="mt-3 block space-y-1.5 border-t border-border/40 pt-2.5">
            <span v-for="position in row.board.positions" :key="`${position.symbol}:${position.side}`" class="flex min-w-0 items-center gap-2 text-xs">
              <strong class="w-[5.5rem] shrink-0 truncate">{{ position.symbol ?? t("common.unknown") }}</strong>
              <span :class="sideClass(position.side)">{{ sideLabel(position.side) }}</span>
              <span class="min-w-0 flex-1 truncate font-mono tabular-nums text-muted" :title="`${t('board.entry')} → ${t('board.mark')}`">{{ formatPrice(position.entryPrice) }} → <span class="text-foreground">{{ formatPrice(position.markPrice) }}</span></span>
              <strong class="shrink-0 font-mono tabular-nums" :class="pnlClass(position.pnl)">{{ signedUsd(position.pnl) }}</strong>
            </span>
          </span>
          <span class="mt-3 flex min-w-0 flex-wrap items-center justify-between gap-x-3 gap-y-1 border-t border-border/40 pt-2.5 text-xs">
            <span class="flex min-w-0 items-center gap-1.5">
              <span class="shrink-0 text-muted">{{ t("pub.latestAction") }}</span>
              <template v-if="row.latest">
                <strong class="truncate">{{ actionLabel(row.latest.action) }} {{ row.latest.symbol ?? t("common.unknown") }}</strong>
                <span :class="sideClass(row.latest.side)">{{ sideLabel(row.latest.side) }}</span>
                <span class="shrink-0 font-mono tabular-nums text-muted">{{ formatClock(row.latest.time) }}</span>
              </template>
              <span v-else class="text-faint">{{ t(row.board.todayEvents == null ? "pub.actionsUnknown" : "pub.noActionToday") }}</span>
            </span>
            <FreshnessStamp :seconds="row.board.snapshotAgeSec" :received-at="receivedAt" :stale="row.board.stale" />
          </span>
        </button>
      </nav>
      <!-- 完整遥测：持仓与分析、今日动态、仓位测算、绩效、曲线、资产分布、交易历史。未选中的只是隐藏，测算器里填的本金不会丢。 -->
      <div>
        <section v-for="row in rows" v-show="row.lead.id === activeId" :id="`pub-lead-${row.lead.id}`" :key="row.lead.id" :aria-label="row.name">
          <LeadTelemetryPanel v-if="row.lead.snapshot" :snapshot="row.lead.snapshot" :portfolio-id="row.lead.portfolioId ?? undefined"
            :display-name="row.lead.name ?? undefined" :countdown-sec="countdownSec ?? undefined">
            <template #aside><LeadTimeline :events="row.board.todayEvents ?? null" :positions="row.board.positions ?? null" :accounts="null" read-only /></template>
          </LeadTelemetryPanel>
          <div v-else class="card card-accent">
            <div class="card-body flex items-center gap-3.5">
              <LeadAvatar :name="row.name" :url="row.board.avatarUrl" :size="56" />
              <div class="min-w-0">
                <h2 class="text-xl font-bold tracking-tight">{{ row.name }}</h2>
                <p class="mt-1 text-sm text-muted">{{ t("pub.noSnapshot") }}</p>
              </div>
            </div>
          </div>
        </section>
      </div>
    </template>
  </div>
</template>
