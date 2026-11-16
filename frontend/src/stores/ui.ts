import { defineStore } from "pinia";
import { ref, shallowRef } from "vue";
import type { Environment } from "@/types/api";

export interface ConfirmOptions {
  title: string; message: string; confirmLabel?: string; strong?: boolean;
  environment?: Environment | null; environments?: (Environment | null)[];
}
export interface Toast { id: number; message: string; tone: "success" | "danger" | "warning" }

// 确认框只允许一个待决操作，接受后保持打开直到请求结束。
export const useUiStore = defineStore("ui", () => {
  const confirmation = shallowRef<ConfirmOptions | null>(null);
  const confirmationId = ref<number | null>(null);
  const sessionGeneration = ref(0);
  const confirming = ref(false);
  const toasts = ref<Toast[]>([]);
  let resolver: ((accepted: boolean) => void) | undefined;
  let sequence = 0;
  let confirmationSequence = 0;
  function ask(options: ConfirmOptions): Promise<boolean> {
    if (confirmation.value) return Promise.resolve(false);
    confirmationId.value = ++confirmationSequence;
    confirmation.value = options;
    return new Promise(resolve => { resolver = resolve; });
  }
  function accept() { confirming.value = true; resolver?.(true); resolver = undefined; }
  function close() {
    resolver?.(false); resolver = undefined;
    confirmationId.value = null;
    confirmation.value = null; confirming.value = false;
  }
  function dismiss(id: number) { toasts.value = toasts.value.filter(item => item.id !== id); }
  function toast(message: string, tone: Toast["tone"] = "success") {
    const id = ++sequence;
    toasts.value.push({ id, message, tone });
    setTimeout(() => dismiss(id), tone === "danger" ? 8000 : 4500);
  }
  // 退出先作废会话，迟到请求不能再更新确认框或提示。
  function clear() { sessionGeneration.value++; close(); toasts.value = []; }
  return { confirmation, confirmationId, sessionGeneration, confirming, toasts, ask, accept, close, toast, dismiss, clear };
});
