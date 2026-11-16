<script setup lang="ts">
// 页面标题与一句话摘要固定在同一层级，主操作使用具名插槽。
// 摘要元素里只放摘要文本，页面可以直接对它做断言与朗读。
defineProps<{ title: string; summary?: string }>();
</script>
<template>
  <header class="page-heading">
    <!-- 窄屏标题区先占满一行，操作按钮换到下一行，避免摘要被按钮挤到只剩半行。 -->
    <div class="min-w-0 flex-1 basis-full sm:basis-0">
      <h1 class="page-title">{{ title }}</h1>
      <p v-if="summary" class="mt-1 text-sm text-muted" data-testid="page-summary">{{ summary }}</p>
    </div>
    <div v-if="$slots.actions" class="flex shrink-0 flex-wrap items-center gap-2"><slot name="actions" /></div>
  </header>
</template>
