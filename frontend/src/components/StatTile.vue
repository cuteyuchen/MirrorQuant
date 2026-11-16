<script setup lang="ts">
import { useI18n } from "@/i18n";
// 指标小格：标签 + 数值 + 可选副文案；不依赖任何 store，无 Pinia 的子组件也能用。
withDefaults(defineProps<{
  label: string; value?: string | number | null; sub?: string | null;
  valueClass?: string; subClass?: string; boxed?: boolean;
}>(), { boxed: true });
const { t } = useI18n();
</script>
<template>
  <!-- dt 只放标签、第一个 dd 只放数值，副文案单独放第二个 dd，读屏与测试都能精确取值。 -->
  <div class="min-w-0" :class="boxed ? 'tile' : ''"><dt class="stat-label">{{ label }}</dt><dd class="stat-value" :class="valueClass">{{ value ?? t("common.unknown") }}</dd><dd v-if="sub" class="stat-sub" :class="subClass">{{ sub }}</dd></div>
</template>
