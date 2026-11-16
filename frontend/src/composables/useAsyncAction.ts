import { ref } from "vue";
import { useUiStore, type ConfirmOptions } from "@/stores/ui";
import { useI18n } from "@/i18n";
import { errorMessage } from "@/utils/errors";

// 危险操作先等待确认；取消和失败都返回显式结果，避免未处理的 rejection。
export function useAsyncAction() {
  const pending = ref(false);
  const error = ref("");
  const ui = useUiStore();
  const { t } = useI18n();
  async function run<T>(
    action: () => Promise<T>,
    options: { confirm?: ConfirmOptions; success?: string | false; onError?: (error: unknown) => void } = {},
  ): Promise<{ ok: true; value: T } | { ok: false }> {
    if (pending.value) return { ok: false };
    pending.value = true;
    error.value = "";
    let accepted = false;
    const generation = ui.sessionGeneration;
    const confirm = options.confirm;
    let confirmationId: number | null = null;
    // 同时检查会话代次和确认编号，不能接管另一轮操作。
    const current = () => ui.sessionGeneration === generation
      && (!confirm || (confirmationId != null && ui.confirmationId === confirmationId));
    try {
      if (confirm) {
        const answer = ui.ask(confirm);
        confirmationId = ui.confirmationId;
        accepted = await answer;
        if (!accepted) return { ok: false };
      }
      if (!current()) return { ok: false };
      const value = await action();
      if (!current()) return { ok: false };
      if (options.success !== false) ui.toast(options.success || t("common.success"));
      return { ok: true, value };
    } catch (cause: unknown) {
      if (!current()) return { ok: false };
      error.value = errorMessage(cause);
      ui.toast(error.value, "danger");
      options.onError?.(cause);
      return { ok: false };
    } finally {
      if (accepted && current()) ui.close();
      pending.value = false;
    }
  }
  return { pending, error, run };
}
