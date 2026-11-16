<script setup lang="ts">
import { computed } from "vue";
import { useNow } from "@/composables/useNow";
import { useI18n } from "@/i18n";
import { timestampMs } from "@/utils/format";
const props = withDefaults(defineProps<{
  seconds?: number | null; receivedAt?: number | null; timestamp?: number | string | null;
  stale?: boolean | null; warnAfter?: number; criticalAfter?: number;
}>(), { warnAfter: 30, criticalAfter: 90 });
const now = useNow();
const { t } = useI18n();
const age = computed(() => {
  if (props.timestamp != null) {
    const value = timestampMs(props.timestamp);
    return value == null ? null : Math.max(0, Math.floor((now.value - value) / 1000));
  }
  if (props.seconds == null || !Number.isFinite(props.seconds)) return null;
  return Math.max(0, Math.floor(props.seconds + (props.receivedAt == null ? 0 : (now.value - props.receivedAt) / 1000)));
});
// 后端 stale 是权威标记；计时只用于展示刷新后的自然老化。
const tone = computed(() => props.stale || (age.value != null && age.value >= props.criticalAfter) ? "text-danger"
  : age.value == null || age.value >= props.warnAfter ? "text-warning" : "text-muted");
</script>
<template><span class="inline-flex flex-wrap justify-end gap-1 text-xs tabular-nums" :class="tone"><span v-if="stale">{{ t("state.STALE") }} · </span>{{ age == null ? t("common.unknown") : t("time.secAgo", { n: age }) }}</span></template>
