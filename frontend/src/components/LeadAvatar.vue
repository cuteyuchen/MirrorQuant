<script setup lang="ts">
import { computed, ref, watch } from "vue";

// 带单员头像：只加载 http(s) 外链并且不带来源页地址；
// 图片缺失或加载失败时回退为名称前两个字符，不留下破图。
const props = withDefaults(defineProps<{ name?: string | null; url?: string | null; size?: number }>(), { size: 40 });
const failed = ref(false);
const src = computed(() => props.url && /^https?:\/\//i.test(props.url) && !failed.value ? props.url : null);
const initials = computed(() => (props.name?.trim() || "?").slice(0, 2).toUpperCase());
// 换了头像地址后重新尝试加载。
watch(() => props.url, () => { failed.value = false; });
</script>

<template>
  <span
    class="inline-flex shrink-0 select-none items-center justify-center overflow-hidden rounded-xl border border-accent/30 bg-gradient-to-br from-accent/25 to-brand/25 font-bold text-accent"
    :style="{ width: `${size}px`, height: `${size}px`, fontSize: `${Math.round(size * 0.36)}px` }"
    aria-hidden="true"
  >
    <img v-if="src" :src="src" alt="" loading="lazy" referrerpolicy="no-referrer" class="h-full w-full object-cover" @error="failed = true" />
    <template v-else>{{ initials }}</template>
  </span>
</template>
