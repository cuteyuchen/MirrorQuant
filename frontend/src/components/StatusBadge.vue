<script setup lang="ts">
import { computed } from "vue";
import { Check, AlertTriangle, Ban, HelpCircle } from "lucide-vue-next";
import { statusLabel, statusTone } from "@/utils/status";
const props = defineProps<{ state?: string | null; large?: boolean }>();
// 文案和图标共同表达状态，不能只依赖红绿颜色。
const tone = computed(() => statusTone(props.state));
const icon = computed(() => tone.value === "success" ? Check : tone.value === "danger" ? Ban : tone.value === "warning" ? AlertTriangle : HelpCircle);
</script>
<template>
  <!-- 胶囊徽章通过 tone-* 取色，亮暗两套主题共用同一组语义色。 -->
  <span class="pill font-semibold" :class="[`tone-${tone}`, large ? 'px-3 py-1 text-sm' : '']">
    <component :is="icon" class="shrink-0" :size="large ? 16 : 13" aria-hidden="true" />{{ statusLabel(state) }}
  </span>
</template>
