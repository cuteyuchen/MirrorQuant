<script setup lang="ts">
import type { ResilienceSettings } from "@/types/api";
import { RESILIENCE_RULES } from "@/constants/validation";
import { useI18n } from "@/i18n";
import NumberField from "@/components/NumberField.vue";
const props = defineProps<{ modelValue: ResilienceSettings; errors: Record<string, string> }>();
const emit = defineEmits<{ "update:modelValue": [value: ResilienceSettings] }>();
const { t } = useI18n();
const catchupKeys = ["missed_order_max_age_sec", "catchup_max_price_dev_pct"] as const;
const reconcileKeys = ["reconcile_interval_sec", "max_consecutive_failures"] as const;
// 所有韧性数字共用后端边界；兼容字段只读保留，不伪造可操作能力。
function update<K extends keyof ResilienceSettings>(key: K, value: ResilienceSettings[K]) { emit("update:modelValue", { ...props.modelValue, [key]: value }); }
</script>
<template>
  <section class="space-y-5">
    <fieldset class="space-y-4 rounded-xl border border-border/50 bg-background/30 p-3.5 sm:p-4">
      <legend class="section-title px-1">{{ t("set.resCatchup") }}</legend>
      <p class="text-xs text-muted">{{ t("set.catchupNote") }}</p>
      <div class="grid gap-5 sm:grid-cols-2"><NumberField v-for="key in catchupKeys" :key="key" :rule="RESILIENCE_RULES[key]" :name="`resilience.${key}`" :model-value="modelValue[key]" :error="errors[`resilience.${key}`]" @update:model-value="update(key, $event)" /></div>
      <label class="field-label">{{ t("set.marginPolicy") }} *
        <select :value="modelValue.insufficient_margin_policy ?? ''" name="resilience.insufficient_margin_policy" class="form-input mt-1" :aria-invalid="!!errors['resilience.insufficient_margin_policy']" @change="update('insufficient_margin_policy', ($event.target as HTMLSelectElement).value)"><option disabled value="">{{ t("state.UNKNOWN") }}</option><option value="auto_scale_down">{{ t("resilience.auto_scale_down") }}</option><option value="skip_alert">{{ t("resilience.skip_alert") }}</option></select>
        <span class="mt-1 block text-xs text-muted">{{ t("resHelp.margin") }}</span><span v-if="errors['resilience.insufficient_margin_policy']" class="field-error">{{ errors["resilience.insufficient_margin_policy"] }}</span>
      </label>
    </fieldset>
    <fieldset class="space-y-4 rounded-xl border border-border/50 bg-background/30 p-3.5 sm:p-4">
      <legend class="section-title px-1">{{ t("set.resReconcile") }}</legend>
      <div class="grid gap-5 sm:grid-cols-2"><NumberField v-for="key in reconcileKeys" :key="key" :rule="RESILIENCE_RULES[key]" :name="`resilience.${key}`" :model-value="modelValue[key]" :error="errors[`resilience.${key}`]" @update:model-value="update(key, $event)" /></div>
      <label class="field-label">{{ t("set.orphanPolicy") }} *
        <select :value="modelValue.auto_close_orphan_positions ?? ''" name="resilience.auto_close_orphan_positions" class="form-input mt-1" :aria-invalid="!!errors['resilience.auto_close_orphan_positions']" @change="update('auto_close_orphan_positions', ($event.target as HTMLSelectElement).value)"><option disabled value="">{{ t("state.UNKNOWN") }}</option><option value="alert_only">{{ t("resilience.alert_only") }}</option><option value="auto_close">{{ t("resilience.auto_close") }}</option></select>
        <span class="mt-1 block text-xs text-muted">{{ t("resHelp.orphans") }}</span><span v-if="errors['resilience.auto_close_orphan_positions']" class="field-error">{{ errors["resilience.auto_close_orphan_positions"] }}</span>
      </label>
    </fieldset>
    <details class="rounded-xl border border-border/50 bg-background/30 px-3.5 py-3" :open="!!errors['resilience.clock_sync_interval_sec']">
      <summary class="cursor-pointer text-sm font-semibold">{{ t("set.resAdvanced") }}</summary>
      <div class="mt-4 space-y-4"><NumberField :rule="RESILIENCE_RULES.clock_sync_interval_sec" name="resilience.clock_sync_interval_sec" :model-value="modelValue.clock_sync_interval_sec" :error="errors['resilience.clock_sync_interval_sec']" @update:model-value="update('clock_sync_interval_sec', $event)" /><p class="text-xs text-muted">{{ t("resHelp.chronology") }}</p></div>
    </details>
  </section>
</template>
