<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { AlertOctagon, Bell, Clock, Radio, RotateCcw } from "lucide-vue-next";
import { useAuthStore } from "@/stores/auth";
import { useBoardStore } from "@/stores/board";
import { useSocketStore } from "@/stores/socket";
import { useAsyncAction } from "@/composables/useAsyncAction";
import { useNow } from "@/composables/useNow";
import { api } from "@/api/client";
import { MOCK_MODE } from "@/api/mode";
import { useI18n } from "@/i18n";
import { reasonText } from "@/utils/errors";
import { formatClock } from "@/utils/format";
import { statusTone } from "@/utils/status";
import BaseModal from "@/components/BaseModal.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import EnvBadge from "@/components/EnvBadge.vue";
import FreshnessStamp from "@/components/FreshnessStamp.vue";

const auth = useAuthStore();
const board = useBoardStore();
const socket = useSocketStore();
const { pending, run } = useAsyncAction();
const { t } = useI18n();
const now = useNow();
const showAlerts = ref(false);
// 把自身高度写进根元素的 --sticky-top：手机上状态条会折行变高，页面内的吸附元素与锚点跳转据此让位。
const bar = ref<HTMLElement | null>(null);
let barObserver: ResizeObserver | undefined;
onMounted(() => {
  if (!bar.value || typeof ResizeObserver === "undefined") return;
  barObserver = new ResizeObserver(() => {
    document.documentElement.style.setProperty("--sticky-top", `${(bar.value?.offsetHeight ?? 0) + 12}px`);
  });
  barObserver.observe(bar.value);
});
onUnmounted(() => {
  barObserver?.disconnect();
  document.documentElement.style.removeProperty("--sticky-top");
});
const kill = computed(() => board.data?.globalKillSwitch ?? null);
const alerts = computed(() => board.data?.alerts);
async function toggleKillSwitch() {
  if (kill.value == null) return;
  const next = !kill.value;
  await run(async () => {
    await api.post("/api/bindings/kill-switch", { enabled: next });
    await board.refresh(true);
  }, { confirm: {
    title: t(next ? "board.kill" : "board.resume"), message: t(next ? "confirm.kill" : "confirm.resume"),
    strong: !next, environments: (board.data?.accounts || []).map(account => account.environment),
  } });
}
// 未登录不读取私有告警，所有页面仍能看到明确的未知/断开状态。
// 实时流：已连接用盈利色并带呼吸点，断开用亏损色，其余过渡状态用警告色；急停时整条变红，不再单独着色。
const streamLive = computed(() => auth.authenticated && socket.state === "connected");
const streamClass = computed(() => {
  if (kill.value === true) return "";
  if (!auth.authenticated) return "text-muted";
  return socket.state === "connected" ? "text-success" : socket.state === "disconnected" ? "text-danger" : "text-warning";
});
// 告警按钮按最高级别着色：有严重告警一眼就能看出来。
const alertTone = computed(() => {
  const tones = (alerts.value || []).map(alert => statusTone(alert.level));
  return tones.includes("danger") ? "danger" : tones.includes("warning") ? "warning" : "neutral";
});
const alertClass = computed(() => {
  if (kill.value === true) return "border-white/40 text-white hover:bg-white/15";
  if (alertTone.value === "danger") return "border-danger/40 bg-danger/10 text-danger hover:bg-danger/20";
  if (alertTone.value === "warning") return "border-warning/40 bg-warning/10 text-warning hover:bg-warning/20";
  return "border-border/70 text-muted hover:bg-surface2 hover:text-foreground";
});
function alertContext(leadId: number | null, accountId: number | null) {
  return [
    board.data?.leads?.find(lead => lead.id === leadId)?.name,
    board.data?.accounts?.find(account => account.id === accountId)?.name,
  ].filter(Boolean).join(" · ");
}
</script>
<template>
  <!-- 吸顶状态条：急停、实时流、数据新鲜度、告警与急停按钮在任何页面都保持可见。 -->
  <div ref="bar" class="sticky top-0 z-30 border-b backdrop-blur-md" :class="kill === true ? 'border-dangerSolid bg-dangerSolid text-white' : 'border-border/60 bg-surface/85'">
    <div class="mx-auto flex w-full max-w-[1680px] flex-wrap items-center gap-x-4 gap-y-1 px-3 py-1.5 text-xs md:px-6">
      <span class="flex items-center gap-1.5 font-semibold" :class="kill == null ? 'text-warning' : kill ? '' : 'text-muted'">
        <AlertOctagon :size="16" aria-hidden="true" />{{ t(kill == null ? "board.killUnknown" : kill ? "board.killOn" : "board.killOff") }}
      </span>
      <span class="flex items-center gap-1.5 font-medium" :class="streamClass">
        <span v-if="streamLive" class="live-dot" aria-hidden="true" /><Radio v-else :size="14" aria-hidden="true" />
        {{ auth.authenticated ? t(`stream.${socket.state}`) : t("stream.authRequired") }}
      </span>
      <span class="flex items-center gap-1.5" :class="kill ? '' : 'text-muted'">{{ t("board.updated") }}<FreshnessStamp :timestamp="board.receivedAt" :warn-after="15" :critical-after="45" :stale="!!board.error" :class="kill ? '!text-white' : ''" /></span>
      <span v-if="MOCK_MODE" class="rounded-full border px-2 py-0.5 font-semibold" :class="kill ? 'border-white/50 bg-white/15 text-white' : 'border-warning/40 bg-warning/10 text-warning'">{{ t("board.mock") }}</span>
      <div class="ml-auto flex items-center gap-2">
        <span class="hidden items-center gap-1.5 font-mono tabular-nums lg:flex" :class="kill ? '' : 'text-muted'" :title="t('app.beijingTime')">
          <Clock :size="14" aria-hidden="true" />{{ formatClock(now) }}<span :class="kill ? 'opacity-80' : 'text-faint'">UTC+8</span>
        </span>
        <button type="button" class="inline-flex min-h-10 items-center gap-1.5 rounded-lg border px-2.5 font-semibold transition md:min-h-8" :class="alertClass"
          :disabled="!auth.authenticated" :aria-label="t('board.alertList')" @click="showAlerts = true">
          <Bell :size="15" aria-hidden="true" />{{ t("board.alerts", { n: alerts?.length ?? "—" }) }}
        </button>
        <button v-if="auth.authenticated" type="button" data-testid="kill-switch" class="inline-flex min-h-10 items-center gap-1.5 rounded-lg px-3 font-bold shadow-sm transition md:min-h-8"
          :class="kill ? 'bg-white text-dangerSolid hover:bg-white/90' : 'bg-dangerSolid text-white hover:brightness-110'" :disabled="pending || kill == null" @click="toggleKillSwitch">
          <RotateCcw v-if="kill" :size="15" aria-hidden="true" /><AlertOctagon v-else :size="15" aria-hidden="true" />{{ t(kill ? "board.resume" : "board.kill") }}
        </button>
      </div>
    </div>
  </div>
  <BaseModal :open="showAlerts" :title="t('board.alertList')" @close="showAlerts = false">
    <div v-if="alerts == null" class="empty-state">{{ t("common.unknown") }}</div>
    <div v-else-if="!alerts.length" class="empty-state">{{ t("board.noAlerts") }}</div>
    <ul v-else class="space-y-2">
      <li v-for="alert in alerts" :key="`${alert.code}:${alert.bindingId}:${alert.leadId}:${alert.accountId}`" class="min-w-0 rounded-xl border p-3"
        :class="statusTone(alert.level) === 'danger' ? 'border-danger/35 bg-danger/5' : statusTone(alert.level) === 'warning' ? 'border-warning/35 bg-warning/5' : 'border-border/60 bg-surface2/50'">
        <div class="flex flex-wrap items-center gap-2">
          <StatusBadge :state="alert.level" />
          <EnvBadge v-if="alert.accountId != null" :environment="board.data?.accounts?.find(account => account.id === alert.accountId)?.environment" />
          <span class="ml-auto min-w-0 text-xs text-muted [overflow-wrap:anywhere]">{{ alertContext(alert.leadId, alert.accountId) }}</span>
        </div>
        <p class="mt-2 text-sm [overflow-wrap:anywhere]">{{ reasonText(alert.message || alert.code) }}</p>
      </li>
    </ul>
  </BaseModal>
</template>
