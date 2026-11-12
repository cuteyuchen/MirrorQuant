<script setup lang="ts">
import { useI18n } from "../i18n";
const { isZh, toggleLocale } = useI18n();
</script>

<template>
  <button
    type="button"
    @click="toggleLocale"
    class="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border border-slate-700/80 bg-surface hover:border-accent/50 hover:bg-surface2 text-xs font-mono font-medium text-slate-300 hover:text-white transition-colors cursor-pointer shrink-0"
    :title="isZh ? 'Switch to English' : '切换为简体中文'"
    data-testid="lang-toggle"
  >
    <span :class="isZh ? 'text-accent font-bold' : 'text-slate-500'">中</span>
    <span class="text-slate-600">/</span>
    <span :class="!isZh ? 'text-accent font-bold' : 'text-slate-500'">EN</span>
  </button>
</template>
