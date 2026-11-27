import { afterEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { h, nextTick } from "vue";
import { createPinia, setActivePinia } from "pinia";
import RoiChart from "@/components/telemetry/RoiChart.vue";
import PositionAnalysis from "@/components/telemetry/PositionAnalysis.vue";
import PerformancePanel from "@/components/telemetry/PerformancePanel.vue";
import OrderHistory from "@/components/telemetry/OrderHistory.vue";
import LeadTelemetryPanel from "@/components/LeadTelemetryPanel.vue";
import PositionCalculatorCard from "@/components/PositionCalculatorCard.vue";
import { telemetryDirectionLabel, telemetrySide } from "@/constants/telemetry";
import { useI18n } from "@/i18n";
import type { LeadSnapshot } from "@/types/api";

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
  it("直连接口已确认空仓时不报数据不可用", () => {
    setActivePinia(createPinia());
    const { t } = useI18n();
    const snapshot = {
      openPositions: null,
      openPositionsMeta: { source: "api", authoritative: true, complete: true, emptyVerified: true },
    };
    const analysis = mount(PositionAnalysis, { props: { snapshot } });
    expect(analysis.text()).toContain(t("tel.noOpenPositions"));
    expect(analysis.text()).not.toContain(t("board.unknownPositions"));
    analysis.unmount();
    const calculator = mount(PositionCalculatorCard, { props: { snapshot } });
    expect(calculator.text()).toContain(t("calc.flatNotice"));
    expect(calculator.text()).not.toContain(t("calc.unknownPositions"));
    calculator.unmount();
  });
});

describe("操作流倒序展示", () => {
  it("最新的批次在最上面，时间缺失的沉底", () => {
    const batches = [
      { symbol: "AAAUSDT", dir: "OPEN_LONG", endTime: 1_800_000_000_000, avgPrice: 10, qty: 1, orders: [] },
      { symbol: "BBBUSDT", dir: "OPEN_LONG", endTime: 1_800_010_000_000, avgPrice: 10, qty: 1, orders: [] },
      { symbol: "CCCUSDT", dir: "OPEN_LONG", endTime: 1_800_020_000_000, avgPrice: 10, qty: 1, orders: [] },
      { symbol: "DDDUSDT", dir: "OPEN_LONG", endTime: null, startTime: null, time: null, avgPrice: 10, qty: 1, orders: [] },
    ];
    const page = mount(OrderHistory, { props: { snapshot: { orderBatches: batches } } });
    const symbols = page.findAll("tbody tr").map(row => row.find("strong").text());
    expect(symbols).toEqual(["CCCUSDT", "BBBUSDT", "AAAUSDT", "DDDUSDT"]);
    page.unmount();
  });

  it("空仓时的单向历史订单自动推算开仓/加仓/减仓/平仓并重建交易轮次", async () => {
    const { t } = useI18n();
    const snapshot: LeadSnapshot = {
      profile: { positionShow: true },
      openPositions: [],
      orders: [
        { id: "1", time: 1000, symbol: "ETHUSDT", side: "SELL", positionSide: "BOTH", dir: "SELL", qty: 100, avgPrice: 1700, pnl: 0, pnlKnown: true },
        { id: "2", time: 2000, symbol: "ETHUSDT", side: "SELL", positionSide: "BOTH", dir: "SELL", qty: 70, avgPrice: 1680, pnl: 0, pnlKnown: true },
        { id: "3", time: 3000, symbol: "ETHUSDT", side: "BUY", positionSide: "BOTH", dir: "BUY", qty: 60, avgPrice: 1720, pnl: -1680, pnlKnown: true },
        { id: "4", time: 4000, symbol: "ETHUSDT", side: "BUY", positionSide: "BOTH", dir: "BUY", qty: 110, avgPrice: 1740, pnl: -5280, pnlKnown: true },
      ],
      orderBatches: [
        { id: "b1", symbol: "ETHUSDT", dir: "SELL", side: "SELL", positionSide: "BOTH", startTime: 1000, endTime: 1000, qty: 100, avgPrice: 1700, totalPnl: 0, orderCount: 1, items: [] },
        { id: "b2", symbol: "ETHUSDT", dir: "SELL", side: "SELL", positionSide: "BOTH", startTime: 2000, endTime: 2000, qty: 70, avgPrice: 1680, totalPnl: 0, orderCount: 1, items: [] },
        { id: "b3", symbol: "ETHUSDT", dir: "BUY", side: "BUY", positionSide: "BOTH", startTime: 3000, endTime: 3000, qty: 60, avgPrice: 1720, totalPnl: -1680, orderCount: 1, items: [] },
        { id: "b4", symbol: "ETHUSDT", dir: "BUY", side: "BUY", positionSide: "BOTH", startTime: 4000, endTime: 4000, qty: 110, avgPrice: 1740, totalPnl: -5280, orderCount: 1, items: [] },
      ],
      tradeCycles: [],
    };
    const page = mount(OrderHistory, { props: { snapshot } });
    const text = page.find("table").text();
    expect(text).toContain(t("dir.OPEN_SHORT"));
    expect(text).toContain(t("dir.ADD_SHORT"));
    expect(text).toContain(t("dir.REDUCE_SHORT"));
    expect(text).toContain(t("dir.CLOSE_SHORT"));
    expect(text).not.toContain("买入ETHUSDT");
    // 交易轮次页签已自动重建为 1 个已平仓空头轮次
    const cyclesTab = page.findAll('[role="tab"]')[1];
    expect(cyclesTab.text()).toContain("1");
    await cyclesTab.trigger("click");
    expect(page.find("table").text()).toContain("ETHUSDT");
    expect(page.find("table").text()).toContain(t("side.SHORT"));
    page.unmount();
  });

  it("LeadTelemetryPanel 将当前持仓与动态最优杠杆测算合并，不重复渲染独立测算卡", () => {
    setActivePinia(createPinia());
    const { t } = useI18n();
    const snapshot: LeadSnapshot = {
      profile: { nickname: "Merged Lead", positionShow: true, marginBalance: 10000 },
      openPositionsSummary: { positionCount: 1, longCount: 1, marginBalance: 10000, totalMarkNotional: 20000, totalNotionalLeverage: 2 },
      openPositions: [{ symbol: "BTCUSDT", positionSide: "LONG", qty: 0.4, avgEntry: 50000, currentPrice: 50000 }],
      roiChart365D: [],
      allocation: { items: [] },
    };
    const panel = mount(LeadTelemetryPanel, {
      props: { snapshot, portfolioId: "p1", displayName: "Merged Lead" },
      global: { stubs: { RoiChart: true, AllocationCard: true, PerformancePanel: true, OrderHistory: true } },
    });
    expect(panel.find('[data-testid="merged-calculator-controls"]').exists()).toBe(true);
    expect(panel.find('[data-testid="position-sizing-strip"]').exists()).toBe(true);
    expect(panel.findComponent(PositionCalculatorCard).exists()).toBe(false);
    expect(panel.text()).toContain(t("tel.openAndCalcTitle"));
    panel.unmount();
  });

  it("持仓分析与动态杠杆卡片：单持仓横向分栏、多持仓PC两列展示且r值不重复", async () => {
    setActivePinia(createPinia());
    const posA = { symbol: "BTCUSDT", positionSide: "LONG", qty: 0.4, avgEntry: 50000, currentPrice: 51000, notionalLeverage: 2 };
    const posB = { symbol: "ETHUSDT", positionSide: "SHORT", qty: 5, avgEntry: 2000, currentPrice: 1950, notionalLeverage: 1.5 };
    const baseSnapshot: LeadSnapshot = {
      profile: { nickname: "Dual Col", positionShow: true, marginBalance: 10000 },
      openPositionsSummary: { positionCount: 1, longCount: 1, marginBalance: 10000, totalMarkNotional: 20400, totalNotionalLeverage: 2.04 },
      openPositions: [posA],
    };
    const page = mount(PositionAnalysis, { props: { snapshot: baseSnapshot, embedded: true, withCalculator: true } });
    expect(page.get('[data-testid="analysis-positions-grid"]').classes()).not.toContain("xl:grid-cols-2");
    expect(page.find(".cq-single-split").exists()).toBe(true);
    const strip = page.get('[data-testid="position-sizing-strip"]');
    expect(strip.text().match(/r=\d+(?:\.\d+)?/g)).toHaveLength(1);

    await page.setProps({ snapshot: { ...baseSnapshot, openPositions: [posA, posB] } });
    expect(page.get('[data-testid="analysis-positions-grid"]').classes()).toContain("xl:grid-cols-2");
    expect(page.find(".cq-single-split").exists()).toBe(false);
    page.unmount();
  });
});
