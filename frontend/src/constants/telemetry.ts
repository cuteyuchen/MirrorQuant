import { useI18n } from "@/i18n";
import { actionLabel, normalizeSide, sideLabel } from "@/utils/status";

const ORDER_DIRECTIONS = ["OPEN_LONG", "ADD_LONG", "CLOSE_LONG", "REDUCE_LONG", "OPEN_SHORT", "ADD_SHORT", "CLOSE_SHORT", "REDUCE_SHORT"] as const;
// 原始采集订单的复合方向与看板 OPEN/ADD 枚举不同，不能混用同一套文案。
export function telemetryDirectionLabel(direction?: string | null): string {
  const normalized = direction?.trim().toUpperCase() ?? "";
  if ((ORDER_DIRECTIONS as readonly string[]).includes(normalized)) return useI18n().t(`dir.${normalized}`);
  if (normalized === "LONG" || normalized === "SHORT") return sideLabel(normalized);
  return actionLabel(normalized);
}
export function telemetrySide(direction?: string | null, qty?: number | null): "LONG" | "SHORT" | "UNKNOWN" {
  const normalized = direction?.trim().toUpperCase() ?? "";
  if ((ORDER_DIRECTIONS as readonly string[]).includes(normalized)) return normalized.endsWith("_LONG") ? "LONG" : "SHORT";
  return normalizeSide(normalized, qty);
}
