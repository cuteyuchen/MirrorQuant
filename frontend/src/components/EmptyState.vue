<script setup lang="ts">
import { Inbox } from "lucide-vue-next";
import { useI18n } from "@/i18n";
// 空数据与加载失败分开呈现，避免将接口失败误报为空仓。
defineProps<{ title?: string; description?: string }>();
const { t } = useI18n();
</script>
<template>
  <div class="empty-state flex flex-col items-center gap-3" role="status">
    <span class="inline-flex h-12 w-12 items-center justify-center rounded-2xl border border-border/60 bg-surface2/70 text-faint"><Inbox :size="22" aria-hidden="true" /></span>
    <p class="font-medium">{{ title || t("common.empty") }}</p>
    <p v-if="description" class="max-w-lg text-xs">{{ description }}</p>
    <slot />
  </div>
</template>
