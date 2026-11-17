import { ApiError } from "@/api/client";
import { useI18n } from "@/i18n";
import type { ValidationIssue } from "@/types/api";

// 后端稳定错误码优先翻译；无法识别的详情保留原文，避免掩盖拒绝原因。
const knownErrors: Record<string, string> = {
  "Disable the binding and drain exposure before deletion": "errors.bindingExposure",
  "Disable the account and drain exposure before deletion": "errors.accountExposure",
  "Disable the lead and drain exposure before deletion": "errors.leadExposure",
  "Disable the account and drain exposure before changing account identity or mode": "errors.accountIdentity",
  "Binding for this Lead and Account already exists": "errors.bindingExists",
  "Portfolio ID already registered": "errors.leadExists",
  "Invalid stored risk defaults; correct settings before applying": "errors.invalidDefaults",
  "Binding has exposure, protection, or retained order history; disable it and retain its ledger": "errors.retainedLedger",
  "Drain and reconcile binding exposure before changing its mapping or protection": "errors.mappingExposure",
  "Cannot verify a flat account; keep it disabled until reconciliation succeeds": "errors.unverifiedFlat",
  "Drain physical positions and pending orders before changing or deleting the account": "errors.accountExposure",
  "Proxy is assigned; explicitly reassign accounts and leads first": "errors.proxyAssigned",
  REQUEST_TIMEOUT: "errors.timeout", NETWORK_ERROR: "errors.network", MOCK_READONLY: "errors.mockReadonly",
  INVALID_RESPONSE: "errors.invalidResponse",
  GLOBAL_KILL_SWITCH: "reason.GLOBAL_KILL_SWITCH", BINDING_DISABLED: "reason.BINDING_DISABLED",
  CIRCUIT_BREAKER: "reason.CIRCUIT_BREAKER", LEAD_STALE: "reason.LEAD_STALE",
  ACCOUNT_DISABLED: "reason.ACCOUNT_DISABLED", INSUFFICIENT_MARGIN: "reason.INSUFFICIENT_MARGIN",
  "Invalid credentials": "errors.credentials", "Invalid username or password": "errors.credentials",
  "Invalid TOTP code": "errors.totp",
};
export function reasonText(reason?: string | null): string {
  if (!reason) return useI18n().t("common.unknown");
  const { t } = useI18n();
  const parsed = parseGuardDetails(reason, t);
  if (parsed) return parsed;
  const key = knownErrors[reason] || `reason.${reason.toUpperCase()}`;
  return t(key) === key ? reason : t(key);
}
// 风控拦截的详情是 risk.py 固定格式的自由文本（如 "Catch-up price drift 1.72% > max 0.50% (age 39149s)"），
// 严格全匹配才转译为本地化原因行，格式稍有出入就保留原文，绝不猜测数字含义。
const DRIFT_RE = /^Catch-up price drift ([0-9]+(?:\.[0-9]+)?)% > max ([0-9]+(?:\.[0-9]+)?)% \(age ([0-9]+(?:\.[0-9]+)?)s\)$/;
const STALE_RE = /^Missed order stale: age ([0-9]+(?:\.[0-9]+)?)s > max ([0-9]+(?:\.[0-9]+)?)s$/;
const ADOPT_RE = /^Adopted existing position: my exposure ([0-9]+(?:\.[0-9]+)?)x, lead exposure ([0-9]+(?:\.[0-9]+)?)x, tolerance ([0-9]+(?:\.[0-9]+)?)%$/;
const SPREAD_RE = /^Orderbook spread ([0-9]+(?:\.[0-9]+)?)% exceeds guard ([0-9]+(?:\.[0-9]+)?)%$/;
const SLIPPAGE_RE = /^Slippage ([0-9]+(?:\.[0-9]+)?)% exceeds limit ([0-9]+(?:\.[0-9]+)?)%$/;
function durationText(seconds: number, t: ReturnType<typeof useI18n>["t"]): string {
  const total = Math.floor(seconds);
  if (total < 120) return t("duration.seconds", { n: total });
  if (total < 3600) return t("duration.minutes", { m: Math.floor(total / 60), s: total % 60 });
  if (total < 86400) return t("duration.hours", { h: Math.floor(total / 3600), m: Math.floor((total % 3600) / 60) });
  return t("duration.days", { d: Math.floor(total / 86400), h: Math.floor((total % 86400) / 3600) });
}
function parseGuardDetails(reason: string, t: ReturnType<typeof useI18n>["t"]): string | null {
  const drift = DRIFT_RE.exec(reason);
  if (drift) {
    return t("reasonDetail.catchupDrift", { drift: drift[1], max: drift[2], age: durationText(Number(drift[3]), t) });
  }
  const stale = STALE_RE.exec(reason);
  if (stale) {
    return t("reasonDetail.staleOrder", { age: durationText(Number(stale[1]), t), max: durationText(Number(stale[2]), t) });
  }
  const adopted = ADOPT_RE.exec(reason);
  if (adopted) {
    return t("reasonDetail.adoptedPosition", { mine: adopted[1], lead: adopted[2], tol: adopted[3] });
  }
  const spread = SPREAD_RE.exec(reason);
  if (spread) {
    return t("reasonDetail.spreadGuard", { measured: spread[1], guard: spread[2] });
  }
  const slippage = SLIPPAGE_RE.exec(reason);
  if (slippage) {
    return t("reasonDetail.slippageGuard", { measured: slippage[1], limit: slippage[2] });
  }
  return null;
}
export function errorMessage(cause: unknown): string {
  const { t } = useI18n();
  if (cause instanceof ApiError) {
    const detail = cause.data?.detail;
    const code = detail && !Array.isArray(detail) && typeof detail === "object" ? detail.code : cause.data?.code;
    const mapped = knownErrors[code || ""] || knownErrors[cause.detail];
    if (mapped) return t(mapped);
    if (cause.status === 401) return t("errors.unauthorized");
    if (cause.status === 403) return t("errors.forbidden");
    if (cause.status === 422) return t("errors.validation");
    if (cause.detail && !cause.detail.startsWith("HTTP ")) return reasonText(cause.detail);
    return t("errors.http", { status: cause.status });
  }
  return t("errors.network");
}
function issueText(issue: ValidationIssue) {
  const { t } = useI18n();
  const type = issue.type;
  if (type === "missing") return t("validation.required");
  if (type === "greater_than") return t("validation.gt", { n: issue.ctx?.gt ?? "—" });
  if (type === "greater_than_equal") return t("validation.min", { n: issue.ctx?.ge ?? "—" });
  if (type === "less_than_equal") return t("validation.max", { n: issue.ctx?.le ?? "—" });
  if (type.includes("int")) return t("validation.integer");
  if (type.includes("float")) return t("validation.number");
  if (type === "string_too_short") return t("validation.minLength", { n: issue.ctx?.min_length ?? "—" });
  if (type === "string_too_long") return t("validation.maxLength", { n: issue.ctx?.max_length ?? "—" });
  return t("validation.invalid");
}
export function fieldErrors(cause: unknown): Record<string, string> {
  if (!(cause instanceof ApiError) || !Array.isArray(cause.data?.detail)) return {};
  return Object.fromEntries(cause.data.detail.map(issue => [
    issue.loc.filter(part => part !== "body").join(".").replace(/[A-Z]/g, char => `_${char.toLowerCase()}`),
    issueText(issue),
  ]));
}
