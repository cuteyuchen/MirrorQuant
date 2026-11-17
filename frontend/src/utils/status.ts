import { useI18n } from "@/i18n";

// 只在净持仓数量已知时推断方向，缺失或零净仓不推断为空头。
export function normalizeSide(side?: string | null, qty?: number | string | null): "LONG" | "SHORT" | "UNKNOWN" {
  const normalized = side?.trim().toUpperCase();
  if (normalized === "LONG" || normalized === "SHORT") return normalized;
  if (normalized === "NET" || normalized === "BOTH") {
    if (qty == null || (typeof qty === "string" && !qty.trim()) || !Number.isFinite(Number(qty)) || Number(qty) === 0) return "UNKNOWN";
    return Number(qty) < 0 ? "SHORT" : "LONG";
  }
  return "UNKNOWN";
}
export function sideLabel(side?: string | null, qty?: number | string | null) {
  return useI18n().t(`side.${normalizeSide(side, qty)}`);
}
export function statusTone(state?: string | null): "success" | "danger" | "warning" | "neutral" {
  const s = state?.toUpperCase() || "";
  if (["ALIGNED", "EXECUTED", "FILLED", "CONNECTED", "HEALTHY", "ACTIVE", "ENABLED", "SUCCESS", "ADOPTED_POSITION"].includes(s)) return "success";
  if (s.startsWith("BLOCKED") || ["FAILED", "ERROR", "REJECTED", "UNFILLED", "CRITICAL", "CIRCUIT_BREAKER", "KILL_SWITCH"].includes(s)) return "danger";
  if (["PARTIAL", "MISSING", "UNKNOWN", "SUBMITTING", "STALE", "WARNING", "ACCUMULATED_DEBT", "PARTIALLY_FILLED", "PENDING_ORDERS", "BASELINE_PENDING", "LIMIT_PLACED", "RESTING"].includes(s)) return "warning";
  return "neutral";
}
export function statusLabel(state?: string | null): string {
  const { t } = useI18n();
  const key = `state.${state?.toUpperCase() || "UNKNOWN"}`;
  const label = t(key);
  if (label !== key) return label;
  if (state?.toUpperCase().startsWith("BLOCKED")) return t("state.BLOCKED");
  if (state?.toUpperCase().startsWith("SKIPPED")) return t("state.SKIPPED");
  return t("state.UNKNOWN");
}
export function actionLabel(action?: string | null) {
  const { t } = useI18n();
  const key = `action.${action?.toUpperCase() || "UNKNOWN"}`;
  return t(key) === key ? t("common.unknown") : t(key);
}
export function pnlClass(value?: number | string | null) {
  if (value == null || value === "" || !Number.isFinite(Number(value))) return "text-muted";
  return Number(value) > 0 ? "text-success" : Number(value) < 0 ? "text-danger" : "text-foreground";
}
