import { ref, watch } from "vue";
import { finiteNumber } from "@/utils/format";

const STORAGE_KEY = "mq_calc_capital";
function saved(): number | null {
  try {
    const value = finiteNumber(localStorage.getItem(STORAGE_KEY));
    return value != null && value > 0 ? value : null;
  } catch { return null; }
}
// 跟单本金是“我的钱”：所有测算器共用同一个数并记在本机，换带单员、换页面或刷新后都不用重填。
const capitalInput = ref<string | number>(saved() ?? 1000);
watch(capitalInput, value => {
  const number = finiteNumber(value);
  if (number == null || number <= 0) return;
  try { localStorage.setItem(STORAGE_KEY, String(number)); } catch { /* 存储受限只影响记忆，不影响本次测算。 */ }
});

export function useCopyCapital() { return capitalInput; }
