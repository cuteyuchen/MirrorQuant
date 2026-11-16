<script setup lang="ts">
import { Copy } from "lucide-vue-next";
import type { RiskSettings } from "@/types/api";
import { useI18n } from "@/i18n";
import RiskPolicyFields from "@/components/RiskPolicyFields.vue";
// 全局默认值只影响新建绑定，批量应用保留独立强确认入口。
defineProps<{ modelValue: RiskSettings; errors: Record<string, string>; dirty: boolean }>();
defineEmits<{ "update:modelValue": [value: RiskSettings]; apply: [] }>();
const { t } = useI18n();
</script>
<template>
  <section class="space-y-4">
    <RiskPolicyFields :model-value="modelValue" :errors="errors" prefix="risk_defaults." @update:model-value="$emit('update:modelValue', $event)" />
    <div class="flex flex-wrap items-center justify-between gap-3 border-t border-border/50 pt-4">
      <p class="text-xs text-muted">{{ t(dirty ? "set.saveFirst" : "set.defaultsScope") }}</p>
      <button type="button" class="button-secondary" :disabled="dirty" @click="$emit('apply')"><Copy :size="16" />{{ t("set.applyDefaultsBtn") }}</button>
    </div>
  </section>
</template>
