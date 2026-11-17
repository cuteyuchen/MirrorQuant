import type { Binding, ResilienceSettings, RiskSettings } from "@/types/api";
import { useI18n } from "@/i18n";

export interface NumericRule { min: number; max: number; exclusive?: boolean; integer?: boolean; label: string; unit?: string; hint?: string }
// 只读核对 backend/app/schemas/api.py；绑定和设置共用同一套边界。
export const RISK_RULES = {
  max_total_leverage: { min: 0, max: 100, label: "set.maxTotalLev", unit: "x", hint: "riskHelp.max_total_leverage" },
  max_single_leverage: { min: 0, max: 100, label: "set.maxSingleLev", unit: "x", hint: "riskHelp.max_single_leverage" },
  max_instrument_leverage: { min: 1, max: 125, integer: true, label: "set.maxInstLev", unit: "x", hint: "riskHelp.max_instrument_leverage" },
  max_drawdown_pct: { min: 0, max: 100, label: "set.mddBreaker", unit: "pct", hint: "riskHelp.max_drawdown_pct" },
  daily_max_loss_pct: { min: 0, max: 100, label: "set.dailyLossBreaker", unit: "pct", hint: "riskHelp.daily_max_loss_pct" },
  max_slippage_pct: { min: 0, exclusive: true, max: 50, label: "set.maxSlip", unit: "pct", hint: "riskHelp.max_slippage_pct" },
  max_spread_pct: { min: 0, max: 20, label: "set.maxSpread", unit: "pct", hint: "riskHelp.max_spread_pct" },
  ioc_offset_pct: { min: 0, max: 10, label: "set.iocOffset", unit: "pct", hint: "riskHelp.ioc_offset_pct" },
  limit_order_ttl_sec: { min: 10, max: 3600, integer: true, label: "set.limitTtl", unit: "seconds", hint: "riskHelp.limit_order_ttl_sec" },
  adopt_tolerance_pct: { min: 0, max: 100, label: "set.adoptTolerance", unit: "pct", hint: "riskHelp.adopt_tolerance_pct" },
  stop_loss_pct: { min: 0, exclusive: true, max: 95, label: "set.slPct", unit: "pct", hint: "riskHelp.stop_loss_pct" },
  take_profit_pct: { min: 0, exclusive: true, max: 500, label: "set.tpPct", unit: "pct", hint: "riskHelp.take_profit_pct" },
  trailing_callback_pct: { min: 0, exclusive: true, max: 50, label: "set.tsCallback", unit: "pct", hint: "riskHelp.trailing_callback_pct" },
  hard_stop_loss_pct: { min: 0, exclusive: true, max: 95, label: "set.hardSlPct", unit: "pct", hint: "riskHelp.hard_stop_loss_pct" },
} satisfies Record<string, NumericRule>;
export const RESILIENCE_RULES = {
  missed_order_max_age_sec: { min: 10, max: 3600, integer: true, label: "set.missedOrderSec", unit: "seconds", hint: "resHelp.missed_order_max_age_sec" },
  catchup_max_price_dev_pct: { min: 0, exclusive: true, max: 10, label: "set.catchupDevPct", unit: "pct", hint: "resHelp.catchup_max_price_dev_pct" },
  reconcile_interval_sec: { min: 15, max: 600, integer: true, label: "set.reconcileSec", unit: "seconds", hint: "resHelp.reconcile_interval_sec" },
  clock_sync_interval_sec: { min: 30, max: 3600, integer: true, label: "set.clockSyncSec", unit: "seconds", hint: "resHelp.clock_sync_interval_sec" },
  max_consecutive_failures: { min: 1, max: 100, integer: true, label: "set.maxConsecFail", unit: "times", hint: "resHelp.max_consecutive_failures" },
} satisfies Record<string, NumericRule>;
export const RISK_SCALE_RULE: NumericRule = { min: 0, exclusive: true, max: 20, label: "bind.riskScale", unit: "x", hint: "riskHelp.risk_scale" };
export const POLL_RULE: NumericRule = { min: 5, max: 3600, integer: true, label: "leads.pollInterval", unit: "seconds" };
export const PROXY_PRIORITY_RULE: NumericRule = { min: 1, max: 1000, integer: true, label: "proxy.priority" };
export const ORDER_MODES = ["ioc_then_market", "ioc_only", "market_only"] as const;
// 滑点超限时的处理方式：拦截，或改挂限价单等待成交。
export const SLIPPAGE_ACTIONS = ["block", "limit"] as const;
export const TP_SL_MODES = ["follow_lead", "independent", "hybrid_guard"] as const;
export const RISK_SWITCHES = {
  enable_circuit_breaker: "risk.circuitBreaker", enable_leverage_cap: "risk.leverageCap",
  adopt_existing_positions: "set.adoptExisting",
  enable_stop_loss: "set.enableSl", enable_take_profit: "set.enableTp", enable_trailing_stop: "set.enableTs",
} as const;
export const RISK_FIELD_GROUPS = {
  execution: ["ioc_offset_pct", "limit_order_ttl_sec", "adopt_tolerance_pct"],
  risk: ["max_total_leverage", "max_single_leverage", "max_instrument_leverage", "max_drawdown_pct", "daily_max_loss_pct", "max_slippage_pct", "max_spread_pct"],
  protection: ["stop_loss_pct", "take_profit_pct", "trailing_callback_pct", "hard_stop_loss_pct"],
} as const satisfies Record<string, readonly (keyof typeof RISK_RULES)[]>;
// 与绑定更新接口一致：有敞口或非终态订单时仅锁定映射及保护参数。
export const FLAT_REQUIRED_RISK_FIELDS = ["tp_sl_mode", "enable_stop_loss", "stop_loss_pct", "enable_take_profit", "take_profit_pct",
  "enable_trailing_stop", "trailing_callback_pct", "hard_stop_loss_pct"] as const satisfies readonly (keyof RiskSettings)[];
export const DEFAULT_RISK: RiskSettings = {
  enable_circuit_breaker: true, enable_leverage_cap: false, max_total_leverage: 8,
  max_single_leverage: 4, max_instrument_leverage: 20, max_drawdown_pct: 30,
  daily_max_loss_pct: 10, max_slippage_pct: 1, max_spread_pct: 0.3, ioc_offset_pct: 0.5,
  order_mode: "ioc_then_market", slippage_action: "block", limit_order_ttl_sec: 120,
  adopt_existing_positions: true, adopt_tolerance_pct: 10,
  tp_sl_mode: "follow_lead", enable_stop_loss: false,
  stop_loss_pct: 5, enable_take_profit: false, take_profit_pct: 10,
  enable_trailing_stop: false, trailing_callback_pct: 2, hard_stop_loss_pct: 15,
};
export const DEFAULT_RESILIENCE: ResilienceSettings = {
  missed_order_max_age_sec: 900, catchup_max_price_dev_pct: 0.5, reconcile_interval_sec: 60,
  auto_close_orphan_positions: "alert_only", insufficient_margin_policy: "auto_scale_down",
  flip_close_first: false, clock_sync_interval_sec: 300, max_consecutive_failures: 5,
};
export const BINDING_RISK_FIELDS = {
  enable_circuit_breaker: "enableCircuitBreaker", enable_leverage_cap: "enableLeverageCap",
  max_total_leverage: "maxTotalLeverage", max_single_leverage: "maxSingleLeverage",
  max_instrument_leverage: "maxInstrumentLeverage", max_drawdown_pct: "maxDrawdownPct",
  daily_max_loss_pct: "dailyMaxLossPct", max_slippage_pct: "maxSlippagePct", max_spread_pct: "maxSpreadPct",
  ioc_offset_pct: "iocOffsetPct", order_mode: "orderMode", tp_sl_mode: "tpSlMode",
  slippage_action: "slippageAction", limit_order_ttl_sec: "limitOrderTtlSec",
  adopt_existing_positions: "adoptExistingPositions", adopt_tolerance_pct: "adoptTolerancePct",
  enable_stop_loss: "enableStopLoss", stop_loss_pct: "stopLossPct", enable_take_profit: "enableTakeProfit",
  take_profit_pct: "takeProfitPct", enable_trailing_stop: "enableTrailingStop",
  trailing_callback_pct: "trailingCallbackPct", hard_stop_loss_pct: "hardStopLossPct",
} as const satisfies Record<keyof RiskSettings, keyof Binding>;
export function bindingRisk(binding: Binding): RiskSettings {
  return Object.fromEntries(Object.entries(BINDING_RISK_FIELDS).map(([key, field]) => [key, binding[field] ?? null])) as unknown as RiskSettings;
}
export function validateNumber(value: unknown, rule: NumericRule): string {
  const { t } = useI18n();
  if (value == null || value === "") return t("validation.required");
  if (typeof value !== "number" || !Number.isFinite(value)) return t("validation.number");
  if (rule.integer && !Number.isInteger(value)) return t("validation.integer");
  if (rule.exclusive ? value <= rule.min : value < rule.min) return t(rule.exclusive ? "validation.gt" : "validation.min", { n: rule.min });
  if (value > rule.max) return t("validation.max", { n: rule.max });
  return "";
}
export function validateFields<T extends object>(value: T, rules: Partial<Record<keyof T, NumericRule>>, prefix = "") {
  const errors: Record<string, string> = {};
  for (const key of Object.keys(rules) as (keyof T & string)[]) {
    const error = validateNumber(value[key], rules[key]!);
    if (error) errors[`${prefix}${key}`] = error;
  }
  return errors;
}
export function riskReductions(before: RiskSettings | null, after: RiskSettings | null): string[] {
  if (!after) return [];
  // 新建草稿也有默认保护基线，不能因尚无绑定而跳过关闭保护的确认。
  const baseline = before ?? DEFAULT_RISK;
  const { t } = useI18n();
  const changes = (Object.keys(RISK_SWITCHES) as (keyof typeof RISK_SWITCHES)[])
    .filter(key => baseline[key] !== false && after[key] === false).map(key => t(RISK_SWITCHES[key]));
  for (const key of ["max_total_leverage", "max_single_leverage", "max_drawdown_pct", "daily_max_loss_pct", "max_spread_pct"] as const) {
    if (baseline[key] !== 0 && after[key] === 0) changes.push(t(RISK_RULES[key].label));
  }
  if (baseline.tp_sl_mode !== after.tp_sl_mode) changes.push(t("set.tpslMode"));
  return changes;
}
