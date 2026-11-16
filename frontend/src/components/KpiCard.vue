<script setup lang="ts">
import type { Component } from "vue";
import { useI18n } from "@/i18n";

// 首屏指标卡：标签、等宽大数字和一行补充说明。
// 色调只做强调，含义始终由文字与正负号表达；未知值显示破折号而不是零。
withDefaults(defineProps<{
  label: string;
  value?: string | number | null;
  sub?: string;
  tone?: "accent" | "brand" | "success" | "danger" | "warning" | "neutral";
  icon?: Component;
  valueClass?: string;
}>(), { tone: "neutral" });
const { t } = useI18n();
</script>

<template>
  <div class="kpi-card" :class="`tone-${tone}`">
    <p class="kpi-label justify-between">
      <span class="min-w-0">{{ label }}</span>
      <component :is="icon" v-if="icon" class="kpi-icon" :size="16" aria-hidden="true" />
    </p>
    <p class="kpi-value" :class="valueClass">{{ value ?? t("common.unknown") }}</p>
    <p v-if="sub || $slots.sub" class="kpi-sub"><slot name="sub">{{ sub }}</slot></p>
    <slot />
  </div>
</template>
