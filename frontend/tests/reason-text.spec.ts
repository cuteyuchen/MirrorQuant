import { describe, expect, it } from "vitest";
import { reasonText } from "@/utils/errors";
import { useI18n } from "@/i18n";

describe("拦截详情转译为本地化原因行", () => {
  const { t } = useI18n();
  it("漂移详情解析数字与耗时", () => {
    expect(reasonText("Catch-up price drift 1.72% > max 0.50% (age 39149s)")).toBe(
      t("reasonDetail.catchupDrift", { drift: "1.72", max: "0.50", age: t("duration.hours", { h: 10, m: 52 }) }),
    );
  });
  it("过期详情解析两个耗时", () => {
    expect(reasonText("Missed order stale: age 3600s > max 900s")).toBe(
      t("reasonDetail.staleOrder", { age: t("duration.hours", { h: 1, m: 0 }), max: t("duration.minutes", { m: 15, s: 0 }) }),
    );
  });
  it("耗时分桶：秒/分/天", () => {
    expect(reasonText("Catch-up price drift 0.60% > max 0.50% (age 45s)")).toContain(t("duration.seconds", { n: 45 }));
    expect(reasonText("Catch-up price drift 0.60% > max 0.50% (age 600s)")).toContain(t("duration.minutes", { m: 10, s: 0 }));
    expect(reasonText("Catch-up price drift 0.60% > max 0.50% (age 90000s)")).toContain(t("duration.days", { d: 1, h: 1 }));
  });
  it("格式不符保留原文，已知错误码映射不变", () => {
    expect(reasonText("Catch-up price drift 1.72% > max 0.50%")).toBe("Catch-up price drift 1.72% > max 0.50%");
    expect(reasonText("some unknown free text")).toBe("some unknown free text");
    expect(reasonText("INSUFFICIENT_MARGIN")).toBe(t("reason.INSUFFICIENT_MARGIN"));
    expect(reasonText(null)).toBe(t("common.unknown"));
  });
});
