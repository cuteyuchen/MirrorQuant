<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { ExternalLink } from "lucide-vue-next";
import { api, isCancelled } from "@/api/client";
import { useAsyncAction } from "@/composables/useAsyncAction";
import { useDataCacheStore } from "@/stores/dataCache";
import { useI18n } from "@/i18n";
import { errorMessage } from "@/utils/errors";
import type { Lead } from "@/types/api";
// 公开开关与带单员的逐个公开授权缺一不可。
defineProps<{ modelValue: boolean | null; error?: string }>();
defineEmits<{ "update:modelValue": [value: boolean] }>();
const { t } = useI18n();
const cache = useDataCacheStore();
const { pending, error: actionError, run } = useAsyncAction();
const loadError = ref("");
const leads = computed(() => [...(cache.leads?.data ?? [])].sort((a, b) => a.id - b.id));
const publicCount = computed(() => leads.value.filter(lead => lead.isPublicDashboard === true).length);
async function load(force = false) {
  try { await cache.fetchLeads(force); loadError.value = ""; }
  catch (cause: unknown) { if (!isCancelled(cause)) loadError.value = errorMessage(cause); }
}
// 逐个带单员的公开授权是独立接口：点一下立即生效，不跟随本页的“保存”。
async function toggle(lead: Lead) {
  // 服务端没返回当前值时不猜目标值。
  if (lead.isPublicDashboard == null) return;
  const result = await run(() => api.put(`/api/leads/${lead.id}`, { isPublicDashboard: !lead.isPublicDashboard }));
  if (result.ok) await load(true);
}
onMounted(() => { void load(); });
</script>
<template>
  <section class="space-y-4">
    <label class="flex min-h-11 items-center gap-3"><input type="checkbox" :checked="modelValue === true" @change="$emit('update:modelValue', ($event.target as HTMLInputElement).checked)" />{{ t("set.publicEnabled") }}</label>
    <p v-if="modelValue == null" class="text-warning">{{ t("state.UNKNOWN") }}</p>
    <p v-if="error" class="field-error" role="alert">{{ error }}</p>
    <p class="text-sm text-muted">{{ t("set.publicScope") }}</p>
    <!-- 带单员清单：谁在公开页上一眼可见，开关直接切换。 -->
    <div class="rounded-lg border border-border/60">
      <header class="flex flex-wrap items-center justify-between gap-2 border-b border-border/60 px-3 py-2.5">
        <h3 class="text-sm font-semibold">{{ t("set.publicLeads") }}</h3>
        <span v-if="cache.leads" class="pill font-mono tabular-nums">{{ t("set.publicLeadsCount", { n: publicCount, total: leads.length }) }}</span>
      </header>
      <p v-if="modelValue === false" class="border-b border-border/60 px-3 py-2 text-xs text-warning">{{ t("set.publicMasterOff") }}</p>
      <p v-if="loadError || actionError" class="border-b border-border/60 px-3 py-2 text-xs text-danger" role="alert">{{ loadError || actionError }}</p>
      <p v-if="!cache.leads" class="px-3 py-4 text-sm text-muted">{{ t(loadError ? "state.UNKNOWN" : "common.loading") }}</p>
      <p v-else-if="!leads.length" class="px-3 py-4 text-sm text-muted">{{ t("leads.emptyTitle") }}</p>
      <ul v-else class="divide-y divide-border/40">
        <li v-for="lead in leads" :key="lead.id" class="flex min-h-12 items-center gap-3 px-3 py-1.5">
          <div class="min-w-0 flex-1">
            <p class="truncate text-sm font-semibold">{{ lead.name ?? t("common.unknown") }}</p>
            <p class="truncate text-xs text-muted">{{ lead.portfolioId ?? t("common.unknown") }}</p>
          </div>
          <span class="shrink-0 text-xs" :class="lead.isPublicDashboard ? 'text-success' : 'text-muted'">{{ t(lead.isPublicDashboard == null ? "state.UNKNOWN" : lead.isPublicDashboard ? "set.publicShown" : "set.publicHidden") }}</span>
          <button type="button" class="icon-button shrink-0" role="switch" :aria-checked="lead.isPublicDashboard === true" :disabled="pending || lead.isPublicDashboard == null"
            :aria-label="t('leads.publicSwitch', { name: lead.name ?? t('common.unknown') })" :title="t('leads.publicSwitch', { name: lead.name ?? t('common.unknown') })" @click="toggle(lead)">
            <span class="relative h-5 w-9 rounded-full" :class="lead.isPublicDashboard ? 'bg-accent' : 'bg-surface3'" aria-hidden="true"><span class="absolute top-1 h-3 w-3 rounded-full bg-foreground" :class="lead.isPublicDashboard ? 'right-1' : 'left-1'" /></span>
          </button>
        </li>
      </ul>
    </div>
    <p class="text-xs text-muted">{{ t("set.publicLeadsHint") }}</p>
    <RouterLink to="/public" class="button-secondary"><ExternalLink :size="16" />{{ t("nav.publicFeed") }}</RouterLink>
  </section>
</template>
