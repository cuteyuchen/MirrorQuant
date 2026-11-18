import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import LeadComparison from "@/components/board/LeadComparison.vue";
import AccountSummary from "@/components/board/AccountSummary.vue";
import { useDataCacheStore } from "@/stores/dataCache";
import { mockBoard, mockResponse, mockSettings } from "@/mocks/board";
import { normalizeSide } from "@/utils/status";
import { baseAsset, formatQty } from "@/utils/format";
import { publicBoardLead } from "@/utils/publicBoard";
import type { PublicDashboardResponse } from "@/types/api";

// 看板验收直接使用同一份样例，不另造只适用于测试的状态。
const wrappers: VueWrapper[] = [];
beforeEach(() => {
  setActivePinia(createPinia());
  // 数量口径：看板验收断言数量文本，显示单位固定为数量模式。
  const settings = structuredClone(mockSettings);
  settings.notifications!.display_unit = "quantity";
  useDataCacheStore().settings = { data: settings, fetchedAt: Date.now() };
});
afterEach(() => wrappers.splice(0).forEach(wrapper => wrapper.unmount()));
function comparison() {
  const wrapper = mount(LeadComparison, {
    props: { lead: mockBoard.leads![0], accounts: mockBoard.accounts, receivedAt: Date.now() },
    global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
  });
  wrappers.push(wrapper);
  return wrapper;
}

describe("跟单看板", () => {
  it("展示五种跟单状态、部分数量和失败原因", () => {
    const wrapper = comparison();
    for (const label of ["已跟上", "部分跟上", "未跟", "被拦截", "未知"]) expect(wrapper.text()).toContain(label);
    expect(wrapper.text()).toContain("0.01 / 0.04");
    expect(wrapper.text()).toContain("今日");
    expect(wrapper.text()).toContain("—");
    expect(wrapper.findAll('[data-testid="mark-price"]')[0].text()).toBe("$69,500.00");
  });

  it("残留仓位置顶，多账户不折叠，今日新仓优先", () => {
    const wrapper = comparison();
    const html = wrapper.html();
    expect(html.indexOf('data-testid="orphans"')).toBeLessThan(html.indexOf("data-position"));
    expect(wrapper.text()).toContain("Main Futures");
    expect(wrapper.text()).toContain("Paper Alpha");
    const symbols = wrapper.findAll("[data-position]").map(row => row.attributes("data-position"));
    expect(symbols).toEqual(["BTCUSDT:LONG", "ETHUSDT:SHORT", "SOLUSDT:LONG", "BTCUSDC:LONG"]);
    expect(wrapper.text()).toContain("开仓");
    expect(wrapper.text()).toContain("加仓");
    expect(wrapper.text()).toContain("减仓");
    expect(wrapper.text()).toContain("平仓");
  });

  it("空仓和未知仓位分别表达", async () => {
    const wrapper = comparison();
    await wrapper.setProps({ lead: mockBoard.leads![1] });
    expect(wrapper.find('[data-testid="empty-positions"]').text()).toContain("当前无持仓");
    await wrapper.setProps({ lead: mockBoard.leads![2] });
    expect(wrapper.find('[data-testid="empty-positions"]').text()).not.toContain("当前无持仓");
  });

  // 隐藏持仓返回空数组时仍然没有证据证明已平仓。
  it("持仓未公开或公开状态未知时不把空数组解释成空仓", async () => {
    const wrapper = comparison();
    for (const positionShow of [false, null]) {
      await wrapper.setProps({ lead: { ...mockBoard.leads![1], positionShow, positions: [] } });
      expect(wrapper.find('[data-testid="empty-positions"]').text()).toContain("持仓数据尚不可用");
      expect(wrapper.find('[data-testid="empty-positions"]').text()).not.toContain("当前无持仓");
    }
  });

  it("账户金额与百分比有正负号，实盘明确标记", () => {
    const wrapper = mount(AccountSummary, { props: { accounts: mockBoard.accounts } });
    wrappers.push(wrapper);
    for (const value of ["+$226.50", "+2.22%", "-$217.40", "-0.87%", "LIVE", "DEMO"]) expect(wrapper.text()).toContain(value);
  });

  // 这里只验证收缩约束，真实 375px 溢出仍须浏览器验收。
  it("长账户名和大额摘要保留窄屏收缩及换行约束", () => {
    const name = "A".repeat(120);
    const wrapper = mount(AccountSummary, { props: {
      accounts: [{ ...mockBoard.accounts![0], name, equity: Number.MAX_SAFE_INTEGER }],
    } });
    wrappers.push(wrapper);
    expect(wrapper.find("section > div").classes()).toEqual(expect.arrayContaining(["min-w-0", "grid-cols-2"]));
    const label = wrapper.find("span.font-semibold");
    expect(label.text()).toBe(name);
    expect(label.classes()).toEqual(expect.arrayContaining(["min-w-0", "flex-1", "[overflow-wrap:anywhere]"]));
    expect(wrapper.findAll("dl").every(item => item.classes().includes("number"))).toBe(true);
  });

  it("只读对照表即使收到私有数据也不显示账户、残留仓或执行结果", async () => {
    const wrapper = comparison();
    await wrapper.setProps({ readOnly: true });
    for (const privateText of ["Paper Alpha", "Main Futures", "我的持仓", "已跟上", "保证金不足", "LIVE", "DEMO"]) {
      expect(wrapper.text()).not.toContain(privateText);
    }
    expect(wrapper.find('[data-testid="orphans"]').exists()).toBe(false);
    expect(wrapper.find("a").exists()).toBe(false);
    expect(wrapper.text()).toContain("$69,500.00");
  });

  it("公开投影逐字段白名单，不将截断订单冒充今日统计", () => {
    const response = mockResponse("/api/dashboard/public") as PublicDashboardResponse;
    const input = { ...response.leads![0], accountName: "must-not-leak", mine: mockBoard.accounts };
    const lead = publicBoardLead(input, Date.now());
    expect(lead.orphans).toBeNull();
    expect(lead.positions?.every(position => position.mine === null)).toBe(true);
    expect(lead.todayEvents?.every(event => event.mine === null)).toBe(true);
    expect(lead.todayOpenCount).toBeNull();
    expect(lead.todayRealizedPnl).toBeNull();
    expect(JSON.stringify(lead)).not.toContain("must-not-leak");
    expect(lead.positions?.find(position => position.symbol === "BTCUSDC")?.markPrice).toBeNull();
  });
});

describe("方向与未知数值", () => {
  it("净仓和混合大小写不误判为空头", () => {
    expect(normalizeSide("Long")).toBe("LONG");
    expect(normalizeSide("sHoRt")).toBe("SHORT");
    expect(normalizeSide("net", null)).toBe("UNKNOWN");
    expect(normalizeSide("both", "")).toBe("UNKNOWN");
    expect(normalizeSide("BOTH", 2)).toBe("LONG");
    expect(normalizeSide("NET", -2)).toBe("SHORT");
    expect(normalizeSide("net", 0)).toBe("UNKNOWN");
  });
  it("未知数量与 USDC 基础币种正确显示", () => {
    expect(formatQty(null)).toBe("—");
    expect(baseAsset("BTC-USDC-SWAP")).toBe("BTC");
    expect(baseAsset("ETHUSDT")).toBe("ETH");
  });
});
