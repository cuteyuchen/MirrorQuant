<script setup lang="ts">
import { useI18n } from "@/i18n";
import { formatDateTime } from "@/utils/format";
import type { AccountDetailAlert, DatasetSyncEntry } from "@/types/api";
import EmptyState from "@/components/EmptyState.vue";
import DataQualityBadge from "./DataQualityBadge.vue";

defineProps<{
  activeAlerts: AccountDetailAlert[];
  recentResolved: AccountDetailAlert[];
  datasets: Record<string, DatasetSyncEntry>;
}>();

const { t } = useI18n();

function severityTone(sev: string) {
  if (sev === "critical") return "border-danger/50 bg-danger/10 text-danger";
  if (sev === "warning") return "border-warning/50 bg-warning/10 text-warning";
  return "border-border/60 bg-background/50 text-muted";
}

function alertText(al: AccountDetailAlert) {
  const translated = t(al.messageKey, (al.params ?? {}) as Record<string, string | number>);
  return translated === al.messageKey ? al.code : translated;
}
</script>

<template>
  <section class="card" aria-labelledby="alerts-panel-title">
    <header class="card-header">
      <div class="flex flex-wrap items-center gap-2">
        <h2 id="alerts-panel-title" class="section-title">{{ t("accountDetail.alerts.title") }}</h2>
        <span class="pill font-mono text-xs tabular-nums" :class="activeAlerts.length ? 'tone-warning' : 'tone-success'">
          {{ activeAlerts.length }}
        </span>
      </div>
    </header>

    <div class="card-body space-y-4">
      <EmptyState v-if="!activeAlerts.length" :title="t('accountDetail.alerts.none')" />

      <ul v-else class="space-y-2">
        <li
          v-for="al in activeAlerts"
          :key="`${al.code}:${al.scopeKey}`"
          class="rounded-xl border px-3 py-2 text-xs"
          :class="severityTone(al.severity)"
        >
          <div class="flex flex-wrap items-center justify-between gap-2">
            <div class="font-medium">{{ alertText(al) }}</div>
            <div class="font-mono text-[11px] opacity-80 tabular-nums">
              <span>{{ al.code }}</span>
              <span v-if="al.firstSeenAtMs"> · {{ formatDateTime(al.firstSeenAtMs, { short: true }) }}</span>
            </div>
          </div>
        </li>
      </ul>

      <div class="space-y-2 rounded-xl border border-border/50 bg-background/40 p-3">
        <h3 class="text-xs font-semibold text-muted">{{ t("accountDetail.dq.panelTitle") }}</h3>
        <div class="flex flex-wrap gap-2">
          <DataQualityBadge
            v-for="(ds, name) in datasets"
            :key="name"
            :label="String(name)"
            :status="ds.status"
            :quality="{ status: ds.status, fetchedAt: ds.lastSuccessAtMs, coverageStart: ds.coverageStartMs, coverageEnd: ds.coverageEndMs, errorMessage: ds.errorMessage }"
          />
        </div>
      </div>

      <details v-if="recentResolved.length" class="text-xs text-muted">
        <summary class="cursor-pointer font-medium hover:text-foreground">
          {{ t("accountDetail.alerts.resolvedToggle", { n: recentResolved.length }) }}
        </summary>
        <ul class="mt-2 space-y-1.5">
          <li v-for="al in recentResolved" :key="`res-${al.code}:${al.scopeKey}`" class="flex justify-between rounded-lg bg-background/40 px-2.5 py-1.5">
            <span>{{ alertText(al) }}</span>
            <span class="font-mono tabular-nums">{{ formatDateTime(al.resolvedAtMs, { short: true }) }}</span>
          </li>
        </ul>
      </details>
    </div>
  </section>
</template>
