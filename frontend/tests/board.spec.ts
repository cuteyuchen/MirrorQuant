import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import DashboardView from "@/views/DashboardView.vue";
import LeadComparison from "@/components/board/LeadComparison.vue";
import LeadTimeline from "@/components/board/LeadTimeline.vue";
import AccountSummary from "@/components/board/AccountSummary.vue";
import { useAuthStore } from "@/stores/auth";
import { useBoardStore } from "@/stores/board";
import { useDataCacheStore } from "@/stores/dataCache";
import { mockBoard, mockResponse, mockSettings } from "@/mocks/board";
import { normalizeSide } from "@/utils/status";
import { baseAsset, formatQty } from "@/utils/format";
import { publicBoardLead } from "@/utils/publicBoard";
import type { PublicDashboardResponse } from "@/types/api";
import type { BoardEvent } from "@/types/api";

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
    expect(wrapper.find('[data-testid="empty-positions"]').text()).toContain("当前暂无持仓");
    await wrapper.setProps({ lead: mockBoard.leads![2] });
    expect(wrapper.find('[data-testid="empty-positions"]').text()).not.toContain("当前暂无持仓");
  });

  // 隐藏持仓返回空数组时如果没有确认标记则仍报未知；已确认空仓（如后端重建或直连证实）则显示当前暂无持仓。
  it("持仓未公开或公开状态未知时不把空数组解释成空仓，但已确认空仓显式展示暂无持仓", async () => {
    const wrapper = comparison();
    for (const positionShow of [false, null]) {
      await wrapper.setProps({ lead: { ...mockBoard.leads![1], positionShow, positionsConfirmed: undefined, positions: [] } });
      expect(wrapper.find('[data-testid="empty-positions"]').text()).toContain("持仓数据尚不可用");
      expect(wrapper.find('[data-testid="empty-positions"]').text()).not.toContain("当前暂无持仓");
    }
    await wrapper.setProps({ lead: { ...mockBoard.leads![1], positionShow: false, positionsConfirmed: true, positions: [] } });
    expect(wrapper.find('[data-testid="empty-positions"]').text()).toContain("当前暂无持仓");
  });

  it("管理员看板同时显示全部账户与自己绑定账户的盈亏，且账户卡片支持点击跳转", async () => {
    const auth = useAuthStore();
    auth.authenticated = true;
    auth.user = { id: 1, username: "admin", role: "admin" };
    useDataCacheStore().board = { data: structuredClone(mockBoard), fetchedAt: Date.now() };
    const board = useBoardStore();
    vi.spyOn(board, "refresh").mockResolvedValue(undefined);
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: "/", component: { template: "<div />" } },
        { path: "/accounts", component: { template: "<div />" } },
        { path: "/bindings", component: { template: "<div />" } },
        { path: "/leads/:id", component: { template: "<div />" } },
      ],
    });
    await router.push("/");
    await router.isReady();
    const wrapper = mount(DashboardView, { global: { plugins: [router] } });
    wrappers.push(wrapper);
    await flushPromises();

    expect(wrapper.text()).toContain("全部账户");
    const ownSection = wrapper.get('[data-testid="admin-own-kpis"]');
    expect(ownSection.text()).toContain("我的实盘权益");
    expect(ownSection.text()).toContain("$24,782.60");
    expect(wrapper.get('[data-testid="account-card-1"]').text()).toContain("我的");
    expect(wrapper.get('[data-testid="account-card-3"]').text()).not.toContain("我的");

    await wrapper.get('[data-testid="account-card-2"]').trigger("click");
    await flushPromises();
    expect(router.currentRoute.value.fullPath).toBe("/accounts?account=2");
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
    // 移动端横向滑动：卡片以 260px 为底宽、不压缩（容器横向滚动）；sm 及以上恢复 min-w-0 收缩。
    expect(wrapper.find("section > div").classes()).toEqual(expect.arrayContaining(["sm:min-w-0", "grid-cols-2"]));
    const label = wrapper.find("span.font-semibold");
    expect(label.text()).toBe(name);
    expect(label.classes()).toEqual(expect.arrayContaining(["min-w-0", "flex-1", "[overflow-wrap:anywhere]"]));
    expect(wrapper.findAll("dl").every(item => item.classes().includes("number"))).toBe(true);
  });

  it("账户超过两个时手机端横向滑动，不超过两个时纵向铺满", () => {
    const base = mockBoard.accounts![0];
    const many = mount(AccountSummary, { props: { accounts: [base, { ...base, id: 98 }, { ...base, id: 99 }] } });
    const few = mount(AccountSummary, { props: { accounts: [base, { ...base, id: 98 }] } });
    wrappers.push(many, few);
    expect(many.find("section > div").classes()).toEqual(expect.arrayContaining(["min-w-[260px]", "shrink-0"]));
    expect(few.find("section > div").classes()).not.toContain("shrink-0");
  });

  it("未绑定带单员显示仅监控标记，不在每个持仓下重复“尚未绑定”", async () => {
    const wrapper = comparison();
    await wrapper.setProps({ lead: { ...mockBoard.leads![0], boundCount: 0, activeBoundCount: 0 } });
    expect(wrapper.text()).toContain("仅监控 · 未绑定");
    expect(wrapper.text()).not.toContain("我的持仓");
    expect(wrapper.findAll("[data-position]").length).toBeGreaterThan(0);
  });

  it("单持仓不拆半宽留白，多持仓在PC按两列平铺", async () => {
    const wrapper = comparison();
    // 已绑定多持仓在 2xl 双列展示
    expect(wrapper.get('[data-testid="positions-grid"]').classes()).toContain("2xl:grid-cols-2");
    // 未绑定多持仓在 lg 双列展示
    await wrapper.setProps({ lead: { ...mockBoard.leads![0], boundCount: 0, activeBoundCount: 0 } });
    expect(wrapper.get('[data-testid="positions-grid"]').classes()).toContain("lg:grid-cols-2");
    // 未绑定仅单个持仓时不启用双列，占满容器宽度避免右侧大片空白
    await wrapper.setProps({ lead: { ...mockBoard.leads![0], boundCount: 0, activeBoundCount: 0, positions: [mockBoard.leads![0].positions![0]] } });
    const singleClasses = wrapper.get('[data-testid="positions-grid"]').classes();
    expect(singleClasses).toContain("grid-cols-1");
    expect(singleClasses).not.toContain("lg:grid-cols-2");
    expect(singleClasses).not.toContain("2xl:grid-cols-2");
  });

  it("空闲带单员默认折叠为一行摘要，展开后恢复持仓明细", async () => {
    const wrapper = comparison();
    await wrapper.setProps({ lead: mockBoard.leads![1], defaultCollapsed: true });
    // defaultCollapsed 只影响初始状态，这里重新挂载以验证。
    const collapsed = mount(LeadComparison, {
      props: { lead: mockBoard.leads![1], accounts: mockBoard.accounts, receivedAt: Date.now(), defaultCollapsed: true },
      global: { stubs: { RouterLink: { template: "<a><slot /></a>" } } },
    });
    wrappers.push(collapsed);
    expect(collapsed.find('[data-testid="empty-positions"]').exists()).toBe(false);
    await collapsed.find("button[aria-expanded]").trigger("click");
    expect(collapsed.find('[data-testid="empty-positions"]').exists()).toBe(true);
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

// 今日动态：成交确认的对账审计串不进动态卡片，拦截原因照常显示，列表高度封顶内部滚动。
describe("今日动态", () => {
  const AUDIT_DETAILS = "Confirmed cumulative 0.024 of 0.024; remaining 0.000; order filled. Event remaining None; replacement only after terminal reconciliation. Intent accounting key mqd1c68aecbc0418edbe897225496bca; Filled 0.024 @ 64146.3";
  function timeline(events: BoardEvent[]) {
    const wrapper = mount(LeadTimeline, { props: { events, positions: [], accounts: [] } });
    wrappers.push(wrapper);
    return wrapper;
  }
  it("不展示对账审计串，拦截原因本地化显示", () => {
    const wrapper = timeline([
      { id: 1, time: Date.now(), symbol: "BTCUSDT", side: "SHORT", action: "ADD", qty: 8.011, price: 64377.98, realizedPnl: null,
        mine: [{ bindingId: 3, accountName: "Paper Alpha", status: "EXECUTED", qty: 0.024, price: 64146.3, slippagePct: 0.36, spreadPct: 0.0012, details: AUDIT_DETAILS }] },
      { id: 2, time: Date.now() - 1000, symbol: "BTCUSDT", side: "SHORT", action: "ADD", qty: 9.22, price: 64522.24, realizedPnl: null,
        mine: [{ bindingId: 2, accountName: "Paper Alpha", status: "BLOCKED_SPREAD", qty: null, price: null, slippagePct: null, spreadPct: null, details: "Orderbook spread 0.381% exceeds guard 0.300%" }] },
    ]);
    expect(wrapper.text()).not.toContain("Intent accounting key");
    expect(wrapper.text()).not.toContain("Confirmed cumulative");
    expect(wrapper.text()).toContain("盘口价差过大，实测 0.381%，上限 0.300%");
  });
  it("动态列表高度封顶、内部滚动", () => {
    const wrapper = timeline([
      { id: 1, time: Date.now(), symbol: "BTCUSDT", side: "SHORT", action: "OPEN", qty: 8.491, price: 64345.39, realizedPnl: null, mine: [] },
    ]);
    const scroller = wrapper.find("ol").element.parentElement;
    expect(scroller?.className).toContain("overflow-y-auto");
    expect(scroller?.className).toContain("max-h-");
  });
  it("未绑定监控模式下不每条刷屏提示尚无对应执行结果，已绑定模式照常提示", () => {
    const events: BoardEvent[] = [
      { id: 1, time: Date.now(), symbol: "ETHUSDT", side: "SHORT", action: "OPEN", qty: 10, price: 1730, realizedPnl: null, mine: [] },
    ];
    const bound = timeline(events);
    expect(bound.text()).toContain("尚无对应执行结果");
    const unboundWrapper = mount(LeadTimeline, { props: { events, positions: [], accounts: [], unbound: true } });
    wrappers.push(unboundWrapper);
    expect(unboundWrapper.text()).not.toContain("尚无对应执行结果");
  });
});
