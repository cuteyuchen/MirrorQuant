<script setup lang="ts">
import { useI18n } from "@/i18n";
// 固定行高保留内容空间；减少动态效果设置由全局样式接管。
withDefaults(defineProps<{ rows?: number }>(), { rows: 4 });
const { t } = useI18n();
</script>
<template>
  <div role="status" :aria-label="t('common.loading')" class="space-y-3 py-4" data-testid="loading-skeleton">
    <span class="sr-only">{{ t("common.loading") }}</span>
    <div v-for="row in rows" :key="`skeleton-${row}`" class="h-16 animate-pulse rounded-xl border border-border/40 bg-surface2/70" aria-hidden="true" />
  </div>
</template>
