<script setup lang="ts">
import { computed, nextTick, onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ChevronDown, Pencil, Plus, RefreshCw, Save, Send, Trash2 } from "lucide-vue-next";
import { api, ApiError, isCancelled } from "@/api/client";
import { useDataCacheStore } from "@/stores/dataCache";
import { useAsyncAction } from "@/composables/useAsyncAction";
import { useI18n } from "@/i18n";
import type { Lead } from "@/types/api";
import type { TableColumn } from "@/types/ui";
import { POLL_RULE, validateNumber } from "@/constants/validation";
import { telemetryDirectionLabel, telemetrySide } from "@/constants/telemetry";
import { errorMessage, fieldErrors, reasonText } from "@/utils/errors";
import { formatDateTime, formatQty, signedUsd, timestampMs } from "@/utils/format";
import { actionLabel, pnlClass, sideLabel } from "@/utils/status";
import BaseModal from "@/components/BaseModal.vue";
import DataTable from "@/components/DataTable.vue";
import MoreMenu from "@/components/MoreMenu.vue";
import PageHeader from "@/components/PageHeader.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import FreshnessStamp from "@/components/FreshnessStamp.vue";
import NumberField from "@/components/NumberField.vue";
defineOptions({ name: "LeadsView" });
const cache = useDataCacheStore();
const { t } = useI18n();
const route = useRoute();
const router = useRouter();
const { pending, error: actionError, run } = useAsyncAction();
const leads = computed(() => cache.leads?.data || []);
const proxies = computed(() => cache.proxies?.data || []);
const loading = ref(false);
const loadError = ref("");
const formElement = ref<HTMLFormElement | null>(null);
function boardLead(lead: Lead) { return cache.board?.data.leads?.find(item => item.id === lead.id); }
function positionCount(lead: Lead): number | null {
  const current = boardLead(lead);
  if (current) return current.positions == null || (current.positionShow !== true && !current.positions.length) ? null : current.positions.length;
  // 列表摘要可能把缺失快照记作零；缺少看板时只相信实际仓位列表。
  const snapshot = cache.leadSnapshots[lead.id]?.data ?? lead.snapshot;
  const positions = snapshot?.openPositions;
  return positions == null || ((snapshot?.profile?.positionShow ?? lead.positionShow) !== true && !positions.length)
    ? null : positions.length;
}
function stale(lead: Lead) { const current = boardLead(lead); return current ? current.stale : lead.stale ?? null; }
function followerCount(lead: Lead): number | null {
  if (!cache.bindings) return null;
  const bound = (cache.bindings.data.items ?? []).filter(binding => binding.leadId === lead.id);
  return bound.some(binding => binding.accountId == null) ? null : new Set(bound.map(binding => binding.accountId)).size;
}
function rowTone(lead: Lead) {
  if (lead.lastError || lead.status?.toUpperCase() === "ERROR") return "bg-danger/10";
  return stale(lead) === true || stale(lead) == null || positionCount(lead) == null || lead.isEnabled == null ? "bg-warning/5" : "";
}
const rows = computed(() => [...leads.value].sort((a, b) => {
  const rank = (lead: Lead) => rowTone(lead).includes("danger") ? 0 : rowTone(lead) ? 1 : 2;
  return rank(a) - rank(b) || a.id - b.id;
}));
const summary = computed(() => t("leads.summary", {
  n: leads.value.length,
  positions: leads.value.filter(lead => positionCount(lead) != null && positionCount(lead)! > 0).length,
  stale: leads.value.filter(lead => stale(lead) === true).length,
  unknown: leads.value.filter(lead => stale(lead) == null || positionCount(lead) == null || lead.isEnabled == null).length,
}));
const columns = computed<TableColumn[]>(() => [
  { key: "name", label: t("common.name"), width: "17%", mobileWide: true },
  // 开关与数字列留足不折行宽度，避免 1280px 桌面上数字折行或横向溢出。
  { key: "enabled", label: t("common.enable"), width: "54px", mobileWide: true },
  { key: "positions", label: t("leads.openPositions"), width: "7%", numeric: true, mobileWide: true },
  { key: "today", label: t("leads.todayActivity"), width: "8%", numeric: true, mobileWide: true },
  { key: "pnl", label: t("leads.todayRealized"), width: "10%", numeric: true, mobileWide: true },
  { key: "action", label: t("leads.latestAction"), width: "14%", mobileWide: true },
  { key: "freshness", label: t("board.snapshot"), width: "13%", mobileWide: true },
  { key: "followers", label: t("leads.followerAccounts"), width: "6%", numeric: true, mobileWide: true },
  { key: "public", label: t("leads.publicCol"), width: "54px", mobileWide: true },
  { key: "slotWatch", label: t("leads.slotWatchCol"), width: "54px", mobileWide: true },
  { key: "actions", label: t("common.actions"), width: "54px", mobileWide: true },
]);
interface LatestAction { time: number | null; symbol: string | null; side: string | null; qty: number | null; action: string | null; direction: string | null }
function latestAction(lead: Lead): LatestAction | null {
  const event = [...(boardLead(lead)?.todayEvents ?? [])].sort((a, b) => (b.time ?? -Infinity) - (a.time ?? -Infinity))[0];
  if (event) return { time: event.time, symbol: event.symbol, side: event.side, qty: event.qty, action: event.action, direction: null };
  if (lead.latestAction) {
    return {
      time: timestampMs(lead.latestAction.time),
      symbol: lead.latestAction.symbol ?? null,
      side: lead.latestAction.side ?? null,
      qty: lead.latestAction.qty ?? null,
      action: null,
      direction: telemetryDirectionLabel(lead.latestAction.direction),
    };
  }
  const snapshot = cache.leadSnapshots[lead.id]?.data ?? lead.snapshot;
  const order = [...(snapshot?.orders ?? [])].sort((a, b) => (b.time ?? b.t ?? -Infinity) - (a.time ?? a.t ?? -Infinity))[0];
  if (order) return { time: order.time ?? order.t ?? null, symbol: order.symbol ?? null, side: telemetrySide(order.dir, order.qty ?? order.q), qty: order.qty ?? order.q ?? null, action: null, direction: telemetryDirectionLabel(order.dir) };
  const last = timestampMs(lead.lastTradeTime ?? snapshot?.profile?.lastTradeTime);
  return last == null ? null : { time: last, symbol: null, side: null, qty: null, action: null, direction: null };
}
function leadAvatar(lead: Lead) { return lead.avatarUrl ?? boardLead(lead)?.avatarUrl ?? null; }
function openDetail(lead: Lead) { void router.push(`/leads/${lead.id}`); }
const showModal = ref(false);
const editing = ref<Lead | null>(null);
const errors = ref<Record<string, string>>({});
const form = ref({
  name: "",
  portfolioId: "",
  description: "",
  pollIntervalSec: 15 as number | null,
  proxyId: null as number | null,
  isPublicDashboard: false,
  slotWatchEnabled: false,
  notificationEnabled: false,
  notificationDingtalkUrl: "",
  notificationWebhookUrl: "",
});
const testPending = ref(false);
const testResult = ref<string | null>(null);

async function testLeadNotify() {
  if (!editing.value) return;
  testPending.value = true;
  testResult.value = null;
  try {
    const res = await api.post<{ ok: boolean }>(`/api/leads/${editing.value.id}/test-notify`);
    testResult.value = res.ok ? t("state.SUCCESS") : t("state.FAILED");
  } catch (err: unknown) {
    testResult.value = errorMessage(err);
  } finally {
    testPending.value = false;
  }
}
const tableError = computed(() => loadError.value || (!showModal.value ? actionError.value : ""));
let loadSequence = 0;
let pollTimer: ReturnType<typeof setInterval> | undefined;
async function load(force = false) {
  const request = ++loadSequence;
  loading.value = true;
  if (force) { loadError.value = ""; actionError.value = ""; }
  const results = await Promise.allSettled([cache.fetchLeads(force), cache.fetchProxies(force), cache.fetchBindings(force), cache.fetchBoard(force)]);
  if (request !== loadSequence) return;
  if (results[0].status === "fulfilled") loadError.value = "";
  else if (!isCancelled(results[0].reason)) loadError.value = errorMessage(results[0].reason);
  loading.value = false;
}
function environments(lead: Lead) { return (cache.bindings?.data.items || []).filter(binding => binding.leadId === lead.id).map(binding => binding.accountEnvironment); }
function edit(lead: Lead | null) {
  editing.value = lead; errors.value = {}; actionError.value = ""; testResult.value = null;
  const nc = lead?.notificationConfig;
  form.value = {
    name: lead?.name || "",
    portfolioId: lead?.portfolioId || "",
    description: lead?.description || "",
    pollIntervalSec: lead ? lead.pollIntervalSec ?? null : 15,
    proxyId: lead?.proxyId ?? null,
    isPublicDashboard: lead?.isPublicDashboard === true,
    slotWatchEnabled: lead?.slotWatchEnabled === true,
    notificationEnabled: !!(nc?.dingtalk_enabled || nc?.webhook_enabled),
    notificationDingtalkUrl: nc?.dingtalk_webhook_url || "",
    notificationWebhookUrl: nc?.webhook_url || "",
  };
  showModal.value = true;
}
async function save() {
  errors.value = {};
  const validation = validateNumber(form.value.pollIntervalSec, POLL_RULE);
  if (validation) errors.value.poll_interval_sec = validation;
  if (!form.value.name.trim()) errors.value.name = t("validation.required");
  else if (form.value.name.trim().length > 120) errors.value.name = t("validation.maxLength", { n: 120 });
  if (!editing.value && form.value.portfolioId.trim().length < 3) errors.value.portfolio_id = t("validation.minLength", { n: 3 });
  else if (!editing.value && form.value.portfolioId.trim().length > 100) errors.value.portfolio_id = t("validation.maxLength", { n: 100 });
  if (Object.keys(errors.value).length) { await focusError(); return; }
  const { portfolioId, notificationEnabled, notificationDingtalkUrl, notificationWebhookUrl, ...other } = form.value;
  const notification_config = notificationEnabled ? {
    dingtalk_enabled: !!notificationDingtalkUrl,
    dingtalk_webhook_url: notificationDingtalkUrl || "",
    webhook_enabled: !!notificationWebhookUrl,
    webhook_url: notificationWebhookUrl || "",
    notify_on_lead_order: true,
  } : null;
  const payload = {
    ...other,
    name: other.name.trim(),
    notification_config,
  };
  const result = await run(() => editing.value ? api.put(`/api/leads/${editing.value.id}`, payload)
    : api.post("/api/leads", { ...payload, portfolioId: portfolioId.trim(), platform: "binance_bapi" }), {
    confirm: editing.value ? { title: t("common.save"), message: t("confirm.edit", { name: form.value.name }), environments: environments(editing.value),
      strong: !cache.bindings || environments(editing.value).some(environment => environment == null) } : undefined,
    onError: cause => { errors.value = fieldErrors(cause); void focusError(); },
  });
  if (result.ok) { showModal.value = false; await load(true); }
}
async function operate(lead: Lead, operation: "toggle" | "delete") {
  // 服务端尚未返回启用状态时不允许猜测目标值。
  if (operation === "toggle" && lead.isEnabled == null) return;
  const title = t(operation === "delete" ? "common.delete" : lead.isEnabled ? "common.disable" : "common.enable");
  const result = await run(async () => {
    if (operation === "delete") await api.delete(`/api/leads/${lead.id}?auto_disable=true`);
    else await api.put(`/api/leads/${lead.id}`, { isEnabled: !lead.isEnabled });
  }, { confirm: { title, message: t(operation === "delete" ? "confirm.delete" : "confirm.toggle", { name: lead.name || "—", action: title }),
    environments: environments(lead), strong: !cache.bindings || environments(lead).some(environment => environment == null) || (operation === "toggle" && !lead.isEnabled) } });
  if (result.ok) await load(true);
}
// 是否进公开看板只影响展示，不影响跟单，所以不弹确认，点一下就切换。
async function togglePublic(lead: Lead) {
  if (lead.isPublicDashboard == null) return;
  const result = await run(() => api.put(`/api/leads/${lead.id}`, { isPublicDashboard: !lead.isPublicDashboard }));
  if (result.ok) await load(true);
}
// 名额监控只影响通知，不影响跟单，所以不弹确认，点一下就切换。
async function toggleSlotWatch(lead: Lead) {
  if (lead.slotWatchEnabled == null) return;
  const result = await run(() => api.put(`/api/leads/${lead.id}`, { slotWatchEnabled: !lead.slotWatchEnabled }));
  if (result.ok) await load(true);
}
async function focusError() { await nextTick(); formElement.value?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus(); }
async function sync(lead: Lead) {
  // 手动采集会进入跟单处理；HTTP 200 的业务失败不能误报成功。
  const result = await run(async () => {
    const response = await api.post<{ ok: boolean | null; error?: string | null }>(`/api/leads/${lead.id}/sync`);
    if (response?.ok !== true) throw new ApiError(502, response?.error || "LEAD_SYNC_FAILED");
    return response;
  }, { confirm: { title: t("leads.syncNow"), message: t("confirm.syncLead", { name: lead.name ?? t("common.unknown") }),
    environments: environments(lead), strong: !cache.bindings || environments(lead).some(environment => environment == null) } });
  if (result.ok) await load(true);
}
// 保留旧看板的详情书签，但详情现在有独立路由而不是主导航弹窗。
watch(() => route.query.detail, () => {
  if (route.path !== "/leads") return;
  const target = Number(route.query.detail);
  if (Number.isInteger(target) && target > 0) {
    const { detail, ...query } = route.query;
    void router.replace({ path: `/leads/${target}`, query });
  }
}, { immediate: true });
watch(() => cache.leads, value => {
  if (value == null) { showModal.value = false; editing.value = null; errors.value = {}; actionError.value = ""; }
});
function start() {
  if (pollTimer) return;
  void load();
  pollTimer = setInterval(() => { if (!loading.value && !pending.value) void load(); }, 15000);
}
function stop() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = undefined; loadSequence++; loading.value = false;
}
onMounted(start);
onActivated(start);
onDeactivated(stop);
onUnmounted(stop);
</script>
<template>
  <div class="min-w-0 space-y-5">
    <PageHeader :title="t('leads.title')" :summary="cache.leads ? summary : t('common.loading')">
      <template #actions><button class="icon-button" :disabled="loading || pending" :aria-label="t('common.refresh')" :title="t('common.refresh')" @click="load(true)"><RefreshCw :size="18" :class="{ 'animate-spin': loading }" /></button><button class="button-primary" :disabled="pending || !cache.leads" @click="edit(null)"><Plus :size="17" />{{ t("leads.addBtn") }}</button></template>
    </PageHeader>
    <DataTable :rows="rows" :columns="columns" :row-key="lead => lead.id" :row-tone="rowTone" :loading="loading" :error="tableError" :empty="t('leads.emptyTitle')" :label="t('leads.title')" selectable @retry="load(true)" @select="openDetail">
      <template #cell-name="{ row }"><div class="flex min-w-0 items-center gap-3"><img v-if="leadAvatar(row)" :src="leadAvatar(row) ?? undefined" :alt="row.name ?? t('common.unknown')" width="32" height="32" class="h-8 w-8 shrink-0 rounded-full object-cover" /><span v-else class="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-surface3 text-xs font-semibold text-muted" aria-hidden="true">{{ row.name?.slice(0, 2) ?? t("common.unknown") }}</span><div class="min-w-0"><RouterLink :to="`/leads/${row.id}`" class="break-words text-sm font-semibold hover:text-accent">{{ row.name ?? t("common.unknown") }}</RouterLink><p class="mt-1 break-all text-xs text-muted">{{ row.portfolioId ?? t("common.unknown") }}</p></div></div></template>
      <template #cell-enabled="{ row }"><button class="icon-button" role="switch" :aria-checked="row.isEnabled === true" :disabled="pending || row.isEnabled == null" :aria-label="t('leads.enableSwitch', { name: row.name ?? t('common.unknown') })" :title="t(row.isEnabled == null ? 'state.UNKNOWN' : row.isEnabled ? 'common.disable' : 'common.enable')" @click="operate(row, 'toggle')"><span class="relative h-5 w-9 rounded-full" :class="row.isEnabled ? 'bg-accent' : 'bg-surface3'" aria-hidden="true"><span class="absolute top-1 h-3 w-3 rounded-full bg-foreground" :class="row.isEnabled ? 'right-1' : 'left-1'" /></span></button></template>
      <template #cell-public="{ row }"><button class="icon-button" role="switch" :aria-checked="row.isPublicDashboard === true" :disabled="pending || row.isPublicDashboard == null" :aria-label="t('leads.publicSwitch', { name: row.name ?? t('common.unknown') })" :title="t('leads.publicSwitch', { name: row.name ?? t('common.unknown') })" @click="togglePublic(row)"><span class="relative h-5 w-9 rounded-full" :class="row.isPublicDashboard ? 'bg-accent' : 'bg-surface3'" aria-hidden="true"><span class="absolute top-1 h-3 w-3 rounded-full bg-foreground" :class="row.isPublicDashboard ? 'right-1' : 'left-1'" /></span></button></template>
      <template #cell-slotWatch="{ row }"><button class="icon-button" role="switch" :aria-checked="row.slotWatchEnabled === true" :disabled="pending || row.slotWatchEnabled == null" :aria-label="t('leads.slotWatchSwitch', { name: row.name ?? t('common.unknown') })" :title="t('leads.slotWatchSwitch', { name: row.name ?? t('common.unknown') })" @click="toggleSlotWatch(row)"><span class="relative h-5 w-9 rounded-full" :class="row.slotWatchEnabled ? 'bg-accent' : 'bg-surface3'" aria-hidden="true"><span class="absolute top-1 h-3 w-3 rounded-full bg-foreground" :class="row.slotWatchEnabled ? 'right-1' : 'left-1'" /></span></button></template>
      <template #cell-positions="{ row }"><span class="whitespace-nowrap text-base font-semibold">{{ formatQty(positionCount(row)) }}</span></template>
      <template #cell-today="{ row }"><span class="whitespace-nowrap">{{ formatQty(boardLead(row)?.todayOpenCount) }} / {{ formatQty(boardLead(row)?.todayCloseCount) }}</span></template>
      <template #cell-pnl="{ row }"><span class="whitespace-nowrap font-semibold" :class="pnlClass(boardLead(row)?.todayRealizedPnl)">{{ signedUsd(boardLead(row)?.todayRealizedPnl) }}</span></template>
      <template #cell-action="{ row }"><template v-if="latestAction(row)"><p class="break-words text-xs font-medium">{{ latestAction(row)?.symbol ?? t("common.unknown") }} · {{ sideLabel(latestAction(row)?.side, latestAction(row)?.qty) }}</p><p v-if="latestAction(row)?.action || latestAction(row)?.direction" class="mt-1 text-xs text-muted">{{ latestAction(row)?.direction ?? actionLabel(latestAction(row)?.action) }}</p><time class="mt-1 block text-xs tabular-nums text-muted">{{ formatDateTime(latestAction(row)?.time, { short: true, withSeconds: false }) }}</time></template><span v-else class="text-xs text-muted">{{ t("leads.noRecentAction") }}</span></template>
      <template #cell-freshness="{ row }"><div class="flex flex-wrap items-center gap-2"><StatusBadge :state="row.isEnabled === false ? 'DISABLED' : stale(row) === true ? 'STALE' : row.status" /><FreshnessStamp v-if="boardLead(row)" :seconds="boardLead(row)?.snapshotAgeSec" :received-at="cache.board?.fetchedAt" :stale="stale(row)" /><FreshnessStamp v-else-if="row.snapshotAgeSec != null" :seconds="row.snapshotAgeSec" :received-at="cache.leads?.fetchedAt" :stale="row.stale" /><FreshnessStamp v-else :timestamp="row.lastSyncAt" :stale="row.stale" /></div><p v-if="row.lastError" class="mt-1 break-words text-xs text-danger">{{ reasonText(row.lastError) }}</p></template>
      <template #cell-followers="{ row }"><span>{{ formatQty(followerCount(row)) }}</span></template>
      <template #cell-actions="{ row }"><MoreMenu><button class="button-secondary justify-start" :disabled="pending" @click="edit(row)"><Pencil :size="16" />{{ t("common.edit") }}</button><button class="button-secondary justify-start" :disabled="pending" @click="sync(row)"><RefreshCw :size="16" />{{ t("leads.syncNow") }}</button><button class="button-danger justify-start" :disabled="pending" @click="operate(row, 'delete')"><Trash2 :size="16" />{{ t("common.delete") }}</button></MoreMenu></template>
      <template #mobile-card="{ row }">
        <div class="space-y-3">
          <div class="flex items-start gap-3">
            <img v-if="leadAvatar(row)" :src="leadAvatar(row) ?? undefined" :alt="row.name ?? t('common.unknown')" width="36" height="36" class="h-9 w-9 shrink-0 rounded-full object-cover" />
            <span v-else class="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-surface3 text-xs font-semibold text-muted" aria-hidden="true">{{ row.name?.slice(0, 2) ?? t("common.unknown") }}</span>
            <div class="min-w-0 flex-1">
              <div class="flex flex-wrap items-center justify-between gap-2">
                <RouterLink :to="`/leads/${row.id}`" class="truncate text-sm font-bold hover:text-accent" @click.stop>{{ row.name ?? t("common.unknown") }}</RouterLink>
                <StatusBadge :state="row.isEnabled === false ? 'DISABLED' : stale(row) === true ? 'STALE' : row.status" />
              </div>
              <div class="mt-0.5 flex flex-wrap items-center gap-2 text-xs text-muted">
                <span class="truncate font-mono">{{ row.portfolioId ?? t("common.unknown") }}</span>
                <FreshnessStamp v-if="boardLead(row)" :seconds="boardLead(row)?.snapshotAgeSec" :received-at="cache.board?.fetchedAt" :stale="stale(row)" />
                <FreshnessStamp v-else-if="row.snapshotAgeSec != null" :seconds="row.snapshotAgeSec" :received-at="cache.leads?.fetchedAt" :stale="row.stale" />
                <FreshnessStamp v-else :timestamp="row.lastSyncAt" :stale="row.stale" />
              </div>
            </div>
          </div>
          <p v-if="row.lastError" class="text-xs text-danger">{{ reasonText(row.lastError) }}</p>
          <div class="grid grid-cols-3 gap-2 rounded-lg bg-background/40 px-2.5 py-2 text-xs">
            <div>
              <span class="text-muted">{{ t("leads.openPositions") }}</span>
              <p class="mt-0.5 font-mono text-sm font-bold tabular-nums">{{ formatQty(positionCount(row)) }}</p>
            </div>
            <div>
              <span class="text-muted">{{ t("leads.todayActivity") }}</span>
              <p class="mt-0.5 font-mono font-semibold tabular-nums">{{ formatQty(boardLead(row)?.todayOpenCount) }} / {{ formatQty(boardLead(row)?.todayCloseCount) }}</p>
            </div>
            <div class="text-right">
              <span class="text-muted">{{ t("leads.todayRealized") }}</span>
              <p class="mt-0.5 font-mono font-semibold tabular-nums" :class="pnlClass(boardLead(row)?.todayRealizedPnl)">{{ signedUsd(boardLead(row)?.todayRealizedPnl) }}</p>
            </div>
          </div>
          <div class="flex flex-wrap items-center justify-between gap-2 text-xs text-muted">
            <span v-if="latestAction(row)" class="min-w-0 truncate">
              {{ latestAction(row)?.symbol ?? t("common.unknown") }} · {{ sideLabel(latestAction(row)?.side, latestAction(row)?.qty) }}
              <template v-if="latestAction(row)?.direction || latestAction(row)?.action"> · {{ latestAction(row)?.direction ?? actionLabel(latestAction(row)?.action) }}</template>
            </span>
            <span v-else>{{ t("leads.noRecentAction") }}</span>
            <span class="shrink-0">{{ t("leads.followerAccounts") }} {{ formatQty(followerCount(row)) }}</span>
          </div>
          <div class="flex flex-wrap items-center justify-between gap-2 border-t border-border/40 pt-2">
            <div class="flex flex-wrap items-center gap-3 text-xs">
              <label class="inline-flex items-center gap-1.5" @click.stop>
                <button class="inline-flex items-center" role="switch" :aria-checked="row.isEnabled === true" :disabled="pending || row.isEnabled == null" :aria-label="t('leads.enableSwitch', { name: row.name ?? t('common.unknown') })" @click.stop="operate(row, 'toggle')"><span class="relative h-5 w-9 rounded-full" :class="row.isEnabled ? 'bg-accent' : 'bg-surface3'" aria-hidden="true"><span class="absolute top-1 h-3 w-3 rounded-full bg-foreground" :class="row.isEnabled ? 'right-1' : 'left-1'" /></span></button>
                <span class="text-muted">{{ t("common.enable") }}</span>
              </label>
              <label class="inline-flex items-center gap-1.5" @click.stop>
                <button class="inline-flex items-center" role="switch" :aria-checked="row.isPublicDashboard === true" :disabled="pending || row.isPublicDashboard == null" :aria-label="t('leads.publicSwitch', { name: row.name ?? t('common.unknown') })" @click.stop="togglePublic(row)"><span class="relative h-5 w-9 rounded-full" :class="row.isPublicDashboard ? 'bg-accent' : 'bg-surface3'" aria-hidden="true"><span class="absolute top-1 h-3 w-3 rounded-full bg-foreground" :class="row.isPublicDashboard ? 'right-1' : 'left-1'" /></span></button>
                <span class="text-muted">{{ t("leads.publicCol") }}</span>
              </label>
              <label class="inline-flex items-center gap-1.5" @click.stop>
                <button class="inline-flex items-center" role="switch" :aria-checked="row.slotWatchEnabled === true" :disabled="pending || row.slotWatchEnabled == null" :aria-label="t('leads.slotWatchSwitch', { name: row.name ?? t('common.unknown') })" @click.stop="toggleSlotWatch(row)"><span class="relative h-5 w-9 rounded-full" :class="row.slotWatchEnabled ? 'bg-accent' : 'bg-surface3'" aria-hidden="true"><span class="absolute top-1 h-3 w-3 rounded-full bg-foreground" :class="row.slotWatchEnabled ? 'right-1' : 'left-1'" /></span></button>
                <span class="text-muted">{{ t("leads.slotWatchCol") }}</span>
              </label>
            </div>
            <div @click.stop>
              <MoreMenu><button class="button-secondary justify-start" :disabled="pending" @click="edit(row)"><Pencil :size="16" />{{ t("common.edit") }}</button><button class="button-secondary justify-start" :disabled="pending" @click="sync(row)"><RefreshCw :size="16" />{{ t("leads.syncNow") }}</button><button class="button-danger justify-start" :disabled="pending" @click="operate(row, 'delete')"><Trash2 :size="16" />{{ t("common.delete") }}</button></MoreMenu>
            </div>
          </div>
        </div>
      </template>
      <template #select-label>{{ t("common.details") }}</template>
    </DataTable>
    <BaseModal :open="showModal" :title="t(editing ? 'leads.editTitle' : 'leads.modalAddTitle')" :busy="pending" @close="showModal = false">
      <form ref="formElement" novalidate @submit.prevent="save"><fieldset class="space-y-4" :disabled="pending">
        <p v-if="actionError" class="error-banner" role="alert">{{ actionError }}</p>
        <label class="field-label">{{ t("common.name") }} *<input v-model="form.name" name="name" class="form-input mt-1" :aria-invalid="!!errors.name" required maxlength="120" /><span v-if="errors.name" class="field-error" role="alert">{{ errors.name }}</span></label>
        <div v-if="!editing"><label class="field-label">{{ t("leads.portfolioId") }} *<input v-model="form.portfolioId" name="portfolioId" class="form-input mt-1" :aria-invalid="!!errors.portfolio_id" required minlength="3" maxlength="100" /><span v-if="errors.portfolio_id" class="field-error" role="alert">{{ errors.portfolio_id }}</span></label><p class="mt-2 text-xs font-semibold text-muted">{{ t("leads.portfolioGuideTitle") }}</p><p class="mt-1 text-xs leading-relaxed text-muted">{{ t("leads.portfolioGuide") }}</p></div>
        <details :open="!!(errors.poll_interval_sec || errors.proxy_id)" class="rounded-xl border border-border/50 bg-background/40 px-3.5 py-2.5"><summary class="flex min-h-10 cursor-pointer items-center justify-between gap-2 text-sm font-semibold">{{ t("leads.extraOptions") }}<ChevronDown :size="17" /></summary><div class="mt-3 space-y-4">
          <NumberField v-model="form.pollIntervalSec" name="pollIntervalSec" :rule="POLL_RULE" :error="errors.poll_interval_sec" />
          <label class="field-label">{{ t("accts.proxy") }}<select v-model="form.proxyId" class="form-input mt-1" :aria-invalid="!!errors.proxy_id"><option :value="null">{{ t("accts.direct") }}</option><option v-for="proxy in proxies" :key="proxy.id" :value="proxy.id">{{ proxy.name ?? t("common.unknown") }}</option></select><span v-if="errors.proxy_id" class="field-error" role="alert">{{ errors.proxy_id }}</span></label>
          <label class="field-label">{{ t("leads.description") }}<textarea v-model="form.description" class="form-input mt-1" rows="2" /></label>
          <label class="flex min-h-10 items-center gap-3 text-sm"><input v-model="form.isPublicDashboard" type="checkbox" />{{ t("leads.public") }}</label>
          <label class="flex min-h-10 items-center gap-3 text-sm"><input v-model="form.slotWatchEnabled" type="checkbox" />{{ t("leads.slotWatch") }}</label>
          <p class="text-xs text-muted">{{ t("leads.slotWatchHint") }}</p>
        </div></details>
        <details class="rounded-xl border border-border/50 bg-background/40 px-3.5 py-2.5">
          <summary class="flex min-h-10 cursor-pointer items-center justify-between gap-2 text-sm font-semibold">
            {{ t("set.leadNotifyTitle") }}
            <ChevronDown :size="17" />
          </summary>
          <div class="mt-3 space-y-4">
            <p class="text-xs text-muted">{{ t("set.leadNotifyHint") }}</p>
            <label class="flex min-h-10 items-center gap-3 text-sm">
              <input v-model="form.notificationEnabled" type="checkbox" />
              {{ t("set.enableChannel") }}
            </label>
            <div v-if="form.notificationEnabled" class="space-y-3">
              <label class="field-label">
                {{ t("set.dingtalk") }} Webhook
                <input v-model="form.notificationDingtalkUrl" type="password" class="form-input mt-1" placeholder="https://oapi.dingtalk.com/robot/send?access_token=..." autocomplete="off" />
              </label>
              <label class="field-label">
                {{ t("set.webhook") }} URL
                <input v-model="form.notificationWebhookUrl" type="password" class="form-input mt-1" placeholder="https://..." autocomplete="off" />
              </label>
              <div v-if="editing" class="flex items-center gap-3 pt-1">
                <button type="button" class="button-secondary" :disabled="testPending" @click="testLeadNotify">
                  <Send :size="16" />{{ t("set.sendTest") }}
                </button>
                <span v-if="testResult" class="text-xs text-muted">{{ testResult }}</span>
              </div>
            </div>
          </div>
        </details>
        <footer class="flex justify-end gap-3"><button type="button" class="button-secondary" @click="showModal = false">{{ t("common.cancel") }}</button><button type="submit" class="button-primary"><Save :size="17" />{{ t(pending ? "common.working" : "common.save") }}</button></footer>
      </fieldset></form>
    </BaseModal>
  </div>
</template>
