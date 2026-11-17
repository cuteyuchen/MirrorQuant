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
  it("认领详情解析两侧敞口与容差", () => {
    expect(reasonText("Adopted existing position: my exposure 0.10x, lead exposure 0.10x, tolerance 10.0%")).toBe(
      t("reasonDetail.adoptedPosition", { mine: "0.10", lead: "0.10", tol: "10.0" }),
    );
  });
  it("认领详情格式不符保留原文", () => {
    expect(reasonText("Adopted existing position: my exposure 0.10x")).toBe("Adopted existing position: my exposure 0.10x");
  });
  it("价差与滑点详情解析实测值与上限", () => {
    expect(reasonText("Orderbook spread 0.381% exceeds guard 0.300%")).toBe(
      t("reasonDetail.spreadGuard", { measured: "0.381", guard: "0.300" }),
    );
    expect(reasonText("Slippage 1.24% exceeds limit 1.00%")).toBe(
      t("reasonDetail.slippageGuard", { measured: "1.24", limit: "1.00" }),
    );
    expect(reasonText("Orderbook spread 0.381%")).toBe("Orderbook spread 0.381%");
  });
  it("格式不符保留原文，已知错误码映射不变", () => {
    expect(reasonText("Catch-up price drift 1.72% > max 0.50%")).toBe("Catch-up price drift 1.72% > max 0.50%");
    expect(reasonText("some unknown free text")).toBe("some unknown free text");
    expect(reasonText("INSUFFICIENT_MARGIN")).toBe(t("reason.INSUFFICIENT_MARGIN"));
    expect(reasonText(null)).toBe(t("common.unknown"));
  });
});
