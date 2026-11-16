import { useI18n } from "@/i18n";
import { timestampMs } from "@/utils/format";
import { useNow } from "./useNow";

// 相对时间跟着共享秒钟走；时间未知时返回空串，由调用方决定显示破折号还是整段省略。
export function useAgo() {
  const { t } = useI18n();
  const now = useNow();
  return (value: Date | string | number | null | undefined): string => {
    const time = timestampMs(value);
    if (time == null) return "";
    const seconds = Math.max(0, Math.floor((now.value - time) / 1000));
    if (seconds < 60) return t("time.secAgo", { n: seconds });
    if (seconds < 3600) return t("time.minAgo", { n: Math.floor(seconds / 60) });
    if (seconds < 86400) return t("time.hourAgo", { n: Math.floor(seconds / 3600) });
    return t("time.dayAgo", { n: Math.floor(seconds / 86400) });
  };
}
