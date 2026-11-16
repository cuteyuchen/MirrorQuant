<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "@/i18n";
const props = defineProps<{ environment?: string | null }>();
const { t } = useI18n();
// 实盘始终是实心红底，一眼可辨；模拟盘用中性描边；
// 未知环境用警告色单独标出，不冒充模拟盘。
const env = computed(() => props.environment?.toLowerCase());
</script>
<template>
  <span class="inline-flex shrink-0 items-center rounded-md border px-1.5 py-0.5 text-xs font-bold leading-4 tracking-wide"
    :class="env === 'live' ? 'border-dangerSolid bg-dangerSolid text-white' : env === 'demo' ? 'border-border/70 bg-surface2 text-muted' : 'border-warning/40 bg-warning/10 text-warning'">
    {{ env === "live" ? t("env.live") : env === "demo" ? t("env.demo") : t("common.unknown") }}
  </span>
</template>
