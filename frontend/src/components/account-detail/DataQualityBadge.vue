<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "@/i18n";
import { formatDateTime } from "@/utils/format";
import type { DatasetQualityInfo, DatasetStatus } from "@/types/api";

const props = defineProps<{
  quality?: DatasetQualityInfo | null;
  status?: DatasetStatus | string | null;
  label?: string;
}>();

const { t } = useI18n();

const resolvedStatus = computed<DatasetStatus>(() => {
  const s = (props.quality?.status ?? props.status ?? "unavailable") as string;
  if (s === "ok" || s === "stale" || s === "partial" || s === "unsupported" || s === "rate_limited") {
    return s;
  }
  return "unavailable";
});

const statusKeyMap: Record<DatasetStatus, string> = {
  ok: "accountDetail.dq.ok",
  stale: "accountDetail.dq.stale",
  partial: "accountDetail.dq.partial",
  unsupported: "accountDetail.dq.unsupported",
  unavailable: "accountDetail.dq.unavailable",
  rate_limited: "accountDetail.dq.rate_limited",
};

const toneClass = computed(() => {
  switch (resolvedStatus.value) {
    case "ok":
      return "tone-success";
    case "stale":
    case "partial":
    case "rate_limited":
      return "tone-warning";
    case "unsupported":
      return "tone-neutral";
    default:
      return "tone-danger";
  }
});

const tooltip = computed(() => {
  const parts: string[] = [];
  if (props.quality?.fetchedAt != null) {
    parts.push(t("accountDetail.dq.fetchedAt", { time: formatDateTime(props.quality.fetchedAt, { short: true }) }));
  }
  if (props.quality?.coverageStart != null && props.quality?.coverageEnd != null) {
    parts.push(
      t("accountDetail.dq.coverage", {
        start: formatDateTime(props.quality.coverageStart, { short: true, withSeconds: false }),
        end: formatDateTime(props.quality.coverageEnd, { short: true, withSeconds: false }),
      }),
    );
  }
  if (props.quality?.errorMessage) {
    parts.push(props.quality.errorMessage);
  }
  return parts.join(" · ") || undefined;
});
</script>

<template>
  <span class="pill text-[11px] font-medium" :class="toneClass" :title="tooltip">
    <span v-if="label" class="mr-1 opacity-80">{{ label }}:</span>
    <span>{{ t(statusKeyMap[resolvedStatus]) }}</span>
  </span>
</template>
