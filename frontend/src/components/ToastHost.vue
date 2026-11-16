<script setup lang="ts">
import { AlertTriangle, CheckCircle2, X, XCircle } from "lucide-vue-next";
import { useUiStore } from "@/stores/ui";
import { useI18n } from "@/i18n";
// 提示不抢焦点，失败信息留出更长阅读时间。
const ui = useUiStore();
const { t } = useI18n();
</script>
<template>
  <div class="fixed bottom-24 right-4 z-[70] flex max-w-[calc(100vw-2rem)] flex-col gap-2 md:bottom-5" aria-live="polite">
    <!-- 图标与边框颜色一起表达成功 / 警告 / 失败，正文保持前景色保证可读。 -->
    <div v-for="item in ui.toasts" :key="item.id" class="flex max-w-lg items-start gap-2.5 rounded-xl border bg-surface p-3 text-sm shadow-pop"
      :class="item.tone === 'danger' ? 'border-danger/60' : item.tone === 'warning' ? 'border-warning/60' : 'border-success/60'"
      :role="item.tone === 'danger' ? 'alert' : 'status'">
      <XCircle v-if="item.tone === 'danger'" class="mt-0.5 shrink-0 text-danger" :size="18" aria-hidden="true" />
      <AlertTriangle v-else-if="item.tone === 'warning'" class="mt-0.5 shrink-0 text-warning" :size="18" aria-hidden="true" />
      <CheckCircle2 v-else class="mt-0.5 shrink-0 text-success" :size="18" aria-hidden="true" />
      <span class="min-w-0 flex-1 break-words pt-0.5 font-medium">{{ item.message }}</span>
      <button class="-my-1.5 -mr-1.5 inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-muted transition hover:bg-surface2 hover:text-foreground" :aria-label="t('common.close')" @click="ui.dismiss(item.id)"><X :size="16" aria-hidden="true" /></button>
    </div>
  </div>
</template>
