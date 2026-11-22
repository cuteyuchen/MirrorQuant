<script setup lang="ts">
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { ArrowLeft, Download, RefreshCw } from "lucide-vue-next";
import { api, ApiError, isCancelled } from "@/api/client";
import { MOCK_MODE } from "@/api/mode";
import { useAsyncAction } from "@/composables/useAsyncAction";
import { useDataCacheStore } from "@/stores/dataCache";
import { useI18n } from "@/i18n";
import { errorMessage } from "@/utils/errors";
import LeadComparison from "@/components/board/LeadComparison.vue";
import LeadTelemetryPanel from "@/components/LeadTelemetryPanel.vue";
import EmptyState from "@/components/EmptyState.vue";
import ErrorState from "@/components/ErrorState.vue";
import LoadingSkeleton from "@/components/LoadingSkeleton.vue";
import MoreMenu from "@/components/MoreMenu.vue";
import PageHeader from "@/components/PageHeader.vue";

defineOptions({ name: "LeadDetailView" });
const cache = useDataCacheStore();
const route = useRoute();
const { t } = useI18n();
const { pending, error: actionError, run } = useAsyncAction();
const id = computed(() => Number(route.params.id));
const validId = computed(() => Number.isInteger(id.value) && id.value > 0);
const lead = computed(() => cache.leads?.data.find(item => item.id === id.value));
const boardLead = computed(() => cache.board?.data.leads?.find(item => item.id === id.value));
// 按路由 ID 投影缓存，A 的旧响应永远不会成为 B 的当前快照。
const snapshot = computed(() => cache.leadSnapshots[id.value]?.data ?? lead.value?.snapshot ?? null);
const followers = computed(() => {
  if (!cache.bindings) return null;
  const bound = (cache.bindings.data.items ?? []).filter(binding => binding.leadId === id.value);
  return bound.some(binding => binding.accountId == null) ? null : new Set(bound.map(binding => binding.accountId)).size;
});
const summary = computed(() => t("leadDetail.summary", {
  n: boardLead.value?.positions == null || (boardLead.value.positionShow !== true && !boardLead.value.positions.length)
    ? t("common.unknown") : boardLead.value.positions.length,
  open: boardLead.value?.todayOpenCount ?? t("common.unknown"),
  close: boardLead.value?.todayCloseCount ?? t("common.unknown"),
  accounts: followers.value ?? t("common.unknown"),
}));
const loading = ref(false);
const snapshotLoading = ref(false);
const loadError = ref("");
const boardError = ref("");
const snapshotError = ref("");
let sequence = 0;
let pollTimer: ReturnType<typeof setInterval> | undefined;

async function load(force = false) {
  const request = ++sequence;
  const targetId = id.value;
  if (!validId.value) { loading.value = false; return; }
  loading.value = true;
  if (force) actionError.value = "";
  const results = await Promise.allSettled([cache.fetchLeads(force), cache.fetchBoard(force), cache.fetchBindings(force)]);
  if (request !== sequence || id.value !== targetId) return;
  loadError.value = results[0].status === "rejected" && !isCancelled(results[0].reason) ? errorMessage(results[0].reason) : "";
  boardError.value = results[1].status === "rejected" && !isCancelled(results[1].reason) ? errorMessage(results[1].reason) : "";
  if (results[2].status === "rejected" && !isCancelled(results[2].reason) && !loadError.value) loadError.value = errorMessage(results[2].reason);
  loading.value = false;
  if (!lead.value || loadError.value) return;
  snapshotLoading.value = true;
  try {
    await cache.fetchLeadSnapshot(targetId, force);
    if (request === sequence && id.value === targetId) snapshotError.value = "";
  } catch (cause: unknown) {
    if (request === sequence && id.value === targetId && !isCancelled(cause)) snapshotError.value = errorMessage(cause);
  } finally { if (request === sequence && id.value === targetId) snapshotLoading.value = false; }
}
async function sync() {
  const target = lead.value;
  if (!target) return;
  const environments = (cache.bindings?.data.items ?? []).filter(binding => binding.leadId === target.id).map(binding => binding.accountEnvironment);
  // 同步成功会处理跟单事件，未知环境与实盘都不能跳过确认。
  const result = await run(async () => {
    const response = await api.post<{ ok: boolean | null; error?: string | null }>(`/api/leads/${target.id}/sync`);
    if (response?.ok !== true) throw new ApiError(502, response?.error || "LEAD_SYNC_FAILED");
    return response;
  }, { confirm: { title: t("leads.syncNow"), message: t("confirm.syncLead", { name: target.name ?? t("common.unknown") }),
    environments, strong: !cache.bindings || environments.some(environment => environment == null) } });
  if (result.ok) await load(true);
}
function downloadCsv(type: "orders" | "cycles") {
  if (!MOCK_MODE && lead.value) window.open(api.resolveUrl(`/api/leads/${lead.value.id}/export-csv?export_type=${type}`), "_blank", "noopener");
}
function start() {
  if (pollTimer) return;
  void load();
  pollTimer = setInterval(() => { if (!loading.value && !snapshotLoading.value && !pending.value) void load(); }, 15000);
}
function stop() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = undefined; sequence++; loading.value = false; snapshotLoading.value = false;
}
watch(id, () => {
  sequence++; snapshotError.value = ""; loadError.value = ""; boardError.value = ""; snapshotLoading.value = false;
  if (route.path.startsWith("/leads/")) void load();
});
onMounted(start);
onActivated(start);
onDeactivated(stop);
onUnmounted(stop);
</script>
<template>
  <div class="min-w-0 space-y-5">
    <RouterLink to="/leads" class="inline-flex min-h-11 items-center gap-2 text-sm text-muted hover:text-foreground"><ArrowLeft :size="17" />{{ t("leadDetail.back") }}</RouterLink>
    <PageHeader :title="lead?.name ?? t('leads.detail')" :summary="summary">
      <template #actions>
        <button class="icon-button" :disabled="loading || snapshotLoading || pending" :aria-label="t('common.refresh')" :title="t('common.refresh')" @click="load(true)"><RefreshCw :size="18" :class="{ 'animate-spin': loading || snapshotLoading }" /></button>
        <button class="button-secondary" :disabled="!lead || loading || snapshotLoading || pending" @click="sync"><RefreshCw :size="16" />{{ t("leads.syncNow") }}</button>
        <MoreMenu><button v-for="kind in (['orders', 'cycles'] as const)" :key="kind" class="button-secondary justify-start" :disabled="MOCK_MODE || !lead" @click="downloadCsv(kind)"><Download :size="16" />{{ t(kind === "orders" ? "leads.exportOrders" : "leads.exportCycles") }}</button></MoreMenu>
      </template>
    </PageHeader>
    <ErrorState v-if="loadError || actionError" :message="loadError || actionError" :busy="loading || pending" @retry="load(true)" />
    <LoadingSkeleton v-if="loading && !lead" />
    <EmptyState v-else-if="!validId || (!loading && !lead && !loadError)" :title="t('leadDetail.missing')" :description="t('leadDetail.missingHint')" />
    <template v-if="lead">
      <ErrorState v-if="boardError" :message="boardError" :busy="loading" @retry="load(true)" />
      <!-- 全页面唯一的权威持仓展示处，下方分析面板均链接回此锚点 -->
      <LeadComparison id="canonical-positions" v-if="boardLead" :lead="boardLead" :received-at="cache.board?.fetchedAt ?? null" :accounts="cache.board?.data.accounts ?? null" :show-details="false" />
      <LoadingSkeleton v-else-if="loading" />
      <EmptyState v-else-if="!boardError" :title="t('board.unknownPositions')" />
      <section class="min-w-0 space-y-4 pt-3" :aria-label="t('leadDetail.analysis')">
        <h2 class="section-title">{{ t("leadDetail.analysis") }}</h2>
        <ErrorState v-if="snapshotError" :message="snapshotError" :busy="snapshotLoading" @retry="load(true)" />
        <LoadingSkeleton v-if="snapshotLoading && !snapshot" />
        <!-- 页头已经展示了带单员名称，面板以 embedded 模式嵌入，不再重复头像与名称。 -->
        <LeadTelemetryPanel v-else-if="snapshot" :snapshot="snapshot" :portfolio-id="lead.portfolioId ?? undefined" :display-name="lead.name ?? undefined" embedded />
        <EmptyState v-else-if="!snapshotError" :title="t('leads.noSnapshot')" />
      </section>
    </template>
  </div>
</template>
