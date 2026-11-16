<script setup lang="ts">
import { computed } from "vue";
import type { RiskSettings } from "@/types/api";
import { ORDER_MODES, RISK_FIELD_GROUPS, RISK_RULES, RISK_SWITCHES, SLIPPAGE_ACTIONS, TP_SL_MODES } from "@/constants/validation";
import { useI18n } from "@/i18n";
import NumberField from "./NumberField.vue";
const props = withDefaults(defineProps<{
  modelValue: RiskSettings; errors?: Record<string, string>; prefix?: string;
  section?: "all" | keyof typeof RISK_FIELD_GROUPS; defaults?: RiskSettings | null;
  disabledFields?: readonly (keyof RiskSettings)[];
}>(), { prefix: "", section: "all" });
const emit = defineEmits<{ "update:modelValue": [value: RiskSettings] }>();
const { t } = useI18n();
const switches = computed(() => (Object.keys(RISK_SWITCHES) as (keyof typeof RISK_SWITCHES)[])
  .filter(key => props.section === "all" || (props.section === "risk" ? ["enable_circuit_breaker", "enable_leverage_cap"].includes(key) :
    props.section === "protection" && ["enable_stop_loss", "enable_take_profit", "enable_trailing_stop"].includes(key))));
const numbers = computed(() => props.section === "all" ? Object.keys(RISK_RULES) as (keyof typeof RISK_RULES)[] : RISK_FIELD_GROUPS[props.section]);
// 明确停用的功能不展示其参数；状态未知时照常展示，有报错的字段强制展示，避免报错无处可看。
const GATED_NUMBERS: Partial<Record<keyof typeof RISK_RULES, (value: RiskSettings) => boolean>> = {
  max_total_leverage: value => value.enable_leverage_cap !== false,
  max_single_leverage: value => value.enable_leverage_cap !== false,
  max_instrument_leverage: value => value.enable_leverage_cap !== false,
  max_drawdown_pct: value => value.enable_circuit_breaker !== false,
  daily_max_loss_pct: value => value.enable_circuit_breaker !== false,
  stop_loss_pct: value => value.enable_stop_loss !== false,
  take_profit_pct: value => value.enable_take_profit !== false,
  trailing_callback_pct: value => value.enable_trailing_stop !== false,
  limit_order_ttl_sec: value => value.slippage_action !== "block",
};
const visibleNumbers = computed(() => numbers.value.filter(key => {
  const gate = GATED_NUMBERS[key];
  if (!gate) return true;
  if (gate(props.modelValue)) return true;
  return !!props.errors?.[`${props.prefix}${key}`];
}));
function changed(key: keyof RiskSettings) { return !!props.defaults && props.modelValue[key] !== props.defaults[key]; }
function locked(key: keyof RiskSettings) {
  return props.disabledFields?.includes(key) === true;
}
// 此处仅编辑草稿；调用方保存前统一确认所有保护项的减弱。
function update<K extends keyof RiskSettings>(key: K, value: RiskSettings[K]) {
  emit("update:modelValue", { ...props.modelValue, [key]: value });
}
</script>
<template>
  <div class="space-y-5">
    <div v-if="switches.length" class="grid gap-3 sm:grid-cols-2">
      <div v-for="key in switches" :key="key">
        <label class="flex min-h-11 items-center gap-3 text-sm">
          <input :checked="modelValue[key] === true" type="checkbox" :name="`${prefix}${key}`" :disabled="locked(key)" @change="update(key, ($event.target as HTMLInputElement).checked)" />{{ t(RISK_SWITCHES[key]) }}
          <span v-if="changed(key)" class="text-xs text-accent">{{ t("form.diffDefault") }}</span>
          <span v-if="modelValue[key] == null" class="text-warning">{{ t("state.UNKNOWN") }}</span>
        </label>
        <p class="text-xs text-muted">{{ t(`riskHelp.${key}`) }}</p>
        <p v-if="errors?.[`${prefix}${key}`]" class="field-error" role="alert">{{ errors[`${prefix}${key}`] }}</p>
      </div>
    </div>
    <div class="grid gap-4 sm:grid-cols-2">
      <div v-if="section === 'all' || section === 'execution'">
        <label class="field-label">{{ t("set.execMode") }} *<span v-if="changed('order_mode')" class="ml-2 text-xs text-accent">{{ t("form.diffDefault") }}</span>
          <select :value="modelValue.order_mode" :name="`${prefix}order_mode`" class="form-input mt-1" :disabled="locked('order_mode')" :aria-invalid="!!errors?.[`${prefix}order_mode`]" @change="update('order_mode', ($event.target as HTMLSelectElement).value)">
            <option v-if="modelValue.order_mode == null" value="">{{ t("common.unknown") }}</option><option v-for="mode in ORDER_MODES" :key="mode" :value="mode">{{ t(`mode.${mode}`) }}</option>
          </select>
        </label><p class="mt-1 text-xs text-muted">{{ t("riskHelp.order_mode") }}</p><p v-if="errors?.[`${prefix}order_mode`]" class="field-error" role="alert">{{ errors[`${prefix}order_mode`] }}</p>
      </div>
      <div v-if="section === 'all' || section === 'execution'">
        <label class="field-label">{{ t("set.slippageAction") }} *<span v-if="changed('slippage_action')" class="ml-2 text-xs text-accent">{{ t("form.diffDefault") }}</span>
          <select :value="modelValue.slippage_action" :name="`${prefix}slippage_action`" class="form-input mt-1" :disabled="locked('slippage_action')" :aria-invalid="!!errors?.[`${prefix}slippage_action`]" @change="update('slippage_action', ($event.target as HTMLSelectElement).value)">
            <option v-if="modelValue.slippage_action == null" value="">{{ t("common.unknown") }}</option><option v-for="action in SLIPPAGE_ACTIONS" :key="action" :value="action">{{ t(`slipAction.${action}`) }}</option>
          </select>
        </label><p class="mt-1 text-xs text-muted">{{ t("riskHelp.slippage_action") }}</p><p v-if="errors?.[`${prefix}slippage_action`]" class="field-error" role="alert">{{ errors[`${prefix}slippage_action`] }}</p>
      </div>
      <div v-if="section === 'all' || section === 'protection'">
        <label class="field-label">{{ t("set.tpslMode") }} *<span v-if="changed('tp_sl_mode')" class="ml-2 text-xs text-accent">{{ t("form.diffDefault") }}</span>
          <select :value="modelValue.tp_sl_mode" :name="`${prefix}tp_sl_mode`" class="form-input mt-1" :disabled="locked('tp_sl_mode')" :aria-invalid="!!errors?.[`${prefix}tp_sl_mode`]" @change="update('tp_sl_mode', ($event.target as HTMLSelectElement).value)">
            <option v-if="modelValue.tp_sl_mode == null" value="">{{ t("common.unknown") }}</option><option v-for="mode in TP_SL_MODES" :key="mode" :value="mode">{{ t(`mode.${mode}`) }}</option>
          </select>
        </label><p class="mt-1 text-xs text-muted">{{ t("riskHelp.tp_sl_mode") }}</p><p v-if="errors?.[`${prefix}tp_sl_mode`]" class="field-error" role="alert">{{ errors[`${prefix}tp_sl_mode`] }}</p>
      </div>
      <NumberField v-for="key in visibleNumbers" :key="key" :name="`${prefix}${key}`" :model-value="modelValue[key]" :rule="RISK_RULES[key]" :error="errors?.[`${prefix}${key}`]" :changed="changed(key)" :disabled="locked(key)" @update:model-value="update(key, $event)" />
    </div>
  </div>
</template>
