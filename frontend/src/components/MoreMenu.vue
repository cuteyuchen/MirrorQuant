<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { Ellipsis } from "lucide-vue-next";
import { useI18n } from "@/i18n";
const { t } = useI18n();
const menu = ref<HTMLDetailsElement | null>(null);
// 原生 details 保留键盘能力；外部点击、Esc 与执行操作后都关闭菜单。
function outside(event: Event) { if (!menu.value?.contains(event.target as Node) && menu.value) menu.value.open = false; }
function close() { if (menu.value) menu.value.open = false; }
onMounted(() => document.addEventListener("click", outside));
onBeforeUnmount(() => document.removeEventListener("click", outside));
</script>
<template>
  <details ref="menu" class="relative inline-block text-left" @keydown.esc.stop="close">
    <summary class="icon-button cursor-pointer list-none [&::-webkit-details-marker]:hidden" :aria-label="t('nav.more')" :title="t('nav.more')"><Ellipsis :size="20" aria-hidden="true" /></summary>
    <div class="absolute right-0 z-20 mt-1 grid w-56 max-w-[calc(100vw-2rem)] gap-0.5 rounded-xl border border-border/70 bg-surface p-1.5 font-sans shadow-pop" @click="close"><slot /></div>
  </details>
</template>
