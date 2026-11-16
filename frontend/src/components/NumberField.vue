<script setup lang="ts">
import { useId } from "vue";
import type { NumericRule } from "@/constants/validation";
import { useI18n } from "@/i18n";
const props = defineProps<{ modelValue: number | null | undefined; rule: NumericRule; error?: string; name?: string; disabled?: boolean; changed?: boolean }>();
const emit = defineEmits<{ "update:modelValue": [value: number | null] }>();
const { t } = useI18n();
const id = useId();
// 清空输入立即转为 null，提交层再显示必填错误，永不发送空字符串。
function input(event: Event) {
  const target = event.target as HTMLInputElement;
  emit("update:modelValue", target.value === "" || !Number.isFinite(target.valueAsNumber) ? null : target.valueAsNumber);
}
</script>
<template>
  <div>
    <label :for="id" class="field-label mb-1">{{ t(rule.label) }} <span aria-hidden="true">*</span><span v-if="changed" class="ml-2 text-xs text-accent">{{ t("form.diffDefault") }}</span></label>
    <div class="flex min-w-0 items-center gap-2">
    <input :id="id" :name="name" :value="modelValue ?? ''" type="number" class="form-input number" required
      :min="rule.exclusive ? undefined : rule.min" :max="rule.max" :step="rule.integer ? 1 : 'any'"
      :disabled="disabled" :aria-invalid="!!error" :aria-describedby="`${id}-hint${error ? ` ${id}-error` : ''}`" @input="input" />
    <span v-if="rule.unit" class="shrink-0 text-xs text-muted">{{ t(`unit.${rule.unit}`) }}</span>
    </div>
    <p :id="`${id}-hint`" class="mt-1 text-xs text-muted">{{ t(rule.hint || "form.numberRange", { min: rule.exclusive ? `> ${rule.min}` : rule.min, max: rule.max }) }}</p>
    <p v-if="error" :id="`${id}-error`" class="field-error" role="alert">{{ error }}</p>
  </div>
</template>
