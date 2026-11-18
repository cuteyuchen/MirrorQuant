import { computed, onMounted } from "vue";
import { useAuthStore } from "@/stores/auth";
import { useDataCacheStore } from "@/stores/dataCache";
import type { DisplayUnit } from "@/types/api";
import { finiteNumber, formatQty, formatUsd, type NumericValue } from "@/utils/format";

export type { DisplayUnit };
export const DEFAULT_DISPLAY_UNIT: DisplayUnit = "notional";

export function resolveDisplayUnit(value: unknown): DisplayUnit {
  return value === "quantity" || value === "both" ? value : DEFAULT_DISPLAY_UNIT;
}

// 全局显示单位：通知与页面共用后端 notifications.display_unit；未登录、未加载
// 或无 Pinia 的轻量挂载时用默认金额口径，不阻塞渲染。
export function useDisplayUnit() {
  let stores: { auth: ReturnType<typeof useAuthStore>; cache: ReturnType<typeof useDataCacheStore> } | null = null;
  try {
    stores = { auth: useAuthStore(), cache: useDataCacheStore() };
  } catch {
    stores = null;
  }
  onMounted(() => {
    if (stores?.auth.authenticated) void stores.cache.fetchSettings().catch(() => {});
  });
  const unit = computed<DisplayUnit>(() => resolveDisplayUnit(stores?.cache.settings?.data.notifications?.display_unit));
  const showQuantity = computed(() => unit.value !== "notional");
  const showNotional = computed(() => unit.value !== "quantity");
  function sizeText(qty: NumericValue, notional: NumericValue, price?: NumericValue): string {
    let money = finiteNumber(notional);
    if (money === null) {
      const q = finiteNumber(qty);
      const p = finiteNumber(price);
      if (q !== null && p !== null) money = Math.abs(q * p);
    }
    if (unit.value === "notional") return formatUsd(money);
    if (unit.value === "quantity") return formatQty(qty);
    return `${formatQty(qty)} (${formatUsd(money)})`;
  }
  return { unit, showQuantity, showNotional, sizeText };
}
