import { afterEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { h, nextTick } from "vue";
import { createPinia, setActivePinia } from "pinia";
import RoiChart from "@/components/telemetry/RoiChart.vue";
import PositionAnalysis from "@/components/telemetry/PositionAnalysis.vue";
import PerformancePanel from "@/components/telemetry/PerformancePanel.vue";
import OrderHistory from "@/components/telemetry/OrderHistory.vue";
import PositionCalculatorCard from "@/components/PositionCalculatorCard.vue";
import { telemetryDirectionLabel, telemetrySide } from "@/constants/telemetry";
import { useI18n } from "@/i18n";

afterEach(() => { vi.useRealTimers(); });
describe("深度分析的保留与安全呈现", () => {
  it("多个收益曲线实例使用不同渐变 ID，未知点断开而不是补成零", () => {
    const points = [{ t: 1_800_000_000_000, v: 1 }, { t: 1_800_010_000_000, v: null }, { t: 1_800_020_000_000, v: 3 }];
    const page = mount({ render: () => h("div", [h(RoiChart, { points }), h(RoiChart, { points })]) });
    const gradients = page.findAll("linearGradient").map(gradient => gradient.attributes("id"));
    expect(gradients).toHaveLength(2);
    expect(new Set(gradients).size).toBe(2);
    expect(page.find("path").attributes("d").match(/[ML]/g)).toEqual(["M", "M"]);
    page.unmount();
  });
  it("窄屏轴文字留在 HTML 中，不随 SVG 缩小为不足12px", () => {
    const page = mount(RoiChart, { props: { points: [
      { t: 1_800_000_000_000, v: -10 }, { t: 1_800_020_000_000, v: 30 },
    ] } });
    expect(page.findAll("svg text")).toHaveLength(0);
    expect(page.get('[data-testid="roi-axis"]').classes()).toContain("text-xs");
    expect(page.findAll('[data-testid="roi-axis"] span')).toHaveLength(3);
    expect(page.get("svg").attributes("preserveAspectRatio")).toBe("none");
    page.unmount();
  });
  it("未知绩效周期不显示内部翻译 key，未知收益不补零", () => {
    const page = mount(PerformancePanel, { props: { snapshot: { performance: { FUTURE: { roi: null, pnl: null } } } } });
    expect(page.text()).not.toContain("timeframe.FUTURE");
    expect(page.get("tbody tr").findAll("td")[0].text()).toBe(useI18n().t("common.unknown"));
    expect(page.get("tbody tr").findAll("td")[1].text()).toBe(useI18n().t("common.unknown"));
    page.unmount();
  });
  it("持仓时长随全局秒钟更新，不依赖不更新的 Date.now computed", async () => {
    vi.useFakeTimers();
    const now = 1_800_000_000_000;
    vi.setSystemTime(now);
    const page = mount(PositionAnalysis, { props: { snapshot: { openPositions: [
      { symbol: "BTCUSDT", positionSide: "long", qty: 0.2, currentPrice: 68000, openTime: now - 599_000, mmr: { mmrPct: 0.5 } },
    ] } } });
    const { t } = useI18n();
    const held = () => page.findAll("dt").find(field => field.text() === t("pos.heldDur"))?.element.nextElementSibling?.textContent;
    expect(held()).toBe(t("dur.hourMin", { h: 0, m: 9 }));
    expect(page.findAll("dt").find(field => field.text() === t("pos.mmrRate"))?.element.nextElementSibling?.textContent).toBe("0.50%");
    await vi.advanceTimersByTimeAsync(1_000);
    await nextTick();
    expect(held()).toBe(t("dur.hourMin", { h: 0, m: 10 }));
    page.unmount();
  });
  it("原始订单复合方向使用人话文案，大小写和净仓方向不会猜为空头", () => {
    const { t } = useI18n();
    expect(telemetryDirectionLabel("open_long")).toBe(t("dir.OPEN_LONG"));
    expect(telemetryDirectionLabel("CLOSE_SHORT")).toBe(t("dir.CLOSE_SHORT"));
    expect(telemetryDirectionLabel(null)).toBe(t("common.unknown"));
    expect(telemetrySide("Open_Long")).toBe("LONG");
    expect(telemetrySide("both")).toBe("UNKNOWN");
    expect(telemetrySide("net", -0.2)).toBe("SHORT");
    const page = mount(OrderHistory, { props: { ordersOnly: true, snapshot: { orders: [
      { time: 1_800_000_000_000, symbol: "BTCUSDT", dir: "OPEN_LONG", qty: 0.2, avgPrice: 68000, pnl: null, type: "market" },
    ] } } });
    expect(page.text()).toContain(t("dir.OPEN_LONG"));
    expect(page.text()).not.toContain("action.OPEN_LONG");
    page.unmount();
  });
  it("未知仓位不显示为已空仓，复制测算不可用", () => {
    setActivePinia(createPinia());
    const page = mount(PositionCalculatorCard, { props: { snapshot: { openPositions: null } } });
    const { t } = useI18n();
    expect(page.text()).toContain(t("calc.unknownPositions"));
    expect(page.text()).not.toContain(t("calc.flatNotice"));
    expect(page.get("header button").attributes("disabled")).toBeDefined();
    page.unmount();
  });
});
