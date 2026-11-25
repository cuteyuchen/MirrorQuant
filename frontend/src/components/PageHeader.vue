<script setup lang="ts">
// 页面标题与一句话摘要固定在同一层级，主操作使用具名插槽。
// 摘要元素里只放摘要文本，页面可以直接对它做断言与朗读。
defineProps<{ title: string; summary?: string }>();
</script>
<template>
  <header class="page-heading">
    <!-- 紧凑按钮与标题同行右对齐，长按钮组自动换行；避免单个刷新图标独占一整行。 -->
    <div class="min-w-[11rem] flex-1">
      <h1 class="page-title">{{ title }}</h1>
      <p v-if="summary" class="mt-0.5 text-xs sm:text-sm text-muted" data-testid="page-summary">{{ summary }}</p>
    </div>
    <div v-if="$slots.actions" class="flex shrink-0 flex-wrap items-center gap-2"><slot name="actions" /></div>
  </header>
</template>
