import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import { api } from "@/api/client";
import { mockResponse, mockWrite } from "@/mocks/board";
import { useDataCacheStore } from "@/stores/dataCache";
import AccountDetailView from "@/views/AccountDetailView.vue";
import AccountsView from "@/views/AccountsView.vue";

vi.mock("@/api/client", async importOriginal => {
  const original = await importOriginal<typeof import("@/api/client")>();
  return {
    ...original,
    api: {
      get: vi.fn(),
      post: vi.fn(),
      put: vi.fn(),
      delete: vi.fn(),
      resolveUrl: vi.fn((path: string) => path),
    },
  };
});

const mounted: ReturnType<typeof mount>[] = [];

function createTestRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/accounts", component: AccountsView },
      { path: "/accounts/:id", component: AccountDetailView },
    ],
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.clearAllMocks();
  setActivePinia(createPinia());
  const cache = useDataCacheStore();
  cache.accounts = { data: mockResponse("/api/accounts") as never, fetchedAt: Date.now() };
  cache.proxies = { data: [], fetchedAt: Date.now() };
  cache.leads = { data: [], fetchedAt: Date.now() };
  cache.bindings = { data: { items: [], globalKillSwitch: false }, fetchedAt: Date.now() };
  vi.spyOn(cache, "fetchAccounts").mockResolvedValue((mockResponse("/api/accounts") as { items: never[] }).items);
  vi.spyOn(cache, "fetchProxies").mockResolvedValue([]);
  vi.spyOn(cache, "fetchLeads").mockResolvedValue([]);
  vi.spyOn(cache, "fetchBindings").mockResolvedValue({ items: [], globalKillSwitch: false });

  vi.mocked(api.get).mockImplementation(async (endpoint: string) => mockResponse(endpoint) as never);
  vi.mocked(api.post).mockImplementation(async (endpoint: string, body?: unknown) => mockWrite(endpoint, "POST", body) as never);
});

afterEach(() => {
  while (mounted.length) mounted.pop()?.unmount();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("跟单账户只读详情页 (/accounts/:id)", () => {
  it("默认首屏显示风险总览与持仓挂单，其余维度按页签懒加载切换且无写操作按钮", async () => {
    const router = createTestRouter();
    await router.push("/accounts/2");
    await router.isReady();
    const wrapper = mount(AccountDetailView, { global: { plugins: [router] } });
    mounted.push(wrapper);
    await flushPromises();

    // Default active tab is positions (RiskOverview + PositionsPanel + OpenOrdersPanel first)
    const tabButtons = wrapper.findAll('[role="tab"]');
    expect(tabButtons.map(b => b.attributes("data-testid"))).toEqual([
      "account-tab-positions",
      "account-tab-equity",
      "account-tab-cashflows",
      "account-tab-trades",
      "account-tab-performance",
    ]);
    expect(wrapper.get('[data-testid="account-tab-positions"]').attributes("aria-selected")).toBe("true");
    expect(wrapper.find('[data-testid="account-panel-positions"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="account-panel-equity"]').exists()).toBe(false);

    const posPanelText = wrapper.get('[data-testid="account-panel-positions"]').text();
    expect(wrapper.text()).toContain("Main Futures");
    expect(wrapper.text()).toContain("BINANCE");
    expect(wrapper.text()).toContain("只读视图");
    expect(posPanelText).toContain("$24,782.60");
    expect(posPanelText).toContain("BTCUSDT");
    expect(posPanelText).toContain("SOLUSDT");
    expect(posPanelText).toContain("手动持仓");
    expect(posPanelText).toContain("USDC");

    // Tab 2: equity & PnL (SVG curve + transfer markers)
    await wrapper.get('[data-testid="account-tab-equity"]').trigger("click");
    await flushPromises();
    expect(wrapper.get('[data-testid="account-panel-equity"]').isVisible()).toBe(true);
    expect(wrapper.get('[data-testid="account-panel-positions"]').isVisible()).toBe(false);
    expect(wrapper.findAll("svg").length).toBeGreaterThanOrEqual(1);
    expect(wrapper.findAll("path").length).toBeGreaterThanOrEqual(2);
    expect(wrapper.findAll("polygon").length).toBeGreaterThanOrEqual(2); // transfer & exchange_daily markers

    // Tab 3: cashflows
    await wrapper.get('[data-testid="account-tab-cashflows"]').trigger("click");
    await flushPromises();
    expect(wrapper.get('[data-testid="account-panel-cashflows"]').text()).toContain("REALIZED_PNL");

    // Tab 4: trades & order history
    await wrapper.get('[data-testid="account-tab-trades"]').trigger("click");
    await flushPromises();
    const tradesText = wrapper.get('[data-testid="account-panel-trades"]').text();
    expect(tradesText).toContain("Slippage 0.92% > 0.50%");
    expect(tradesText).toContain("拦截");

    // Tab 5: performance & alerts
    await wrapper.get('[data-testid="account-tab-performance"]').trigger("click");
    await flushPromises();
    expect(wrapper.get('[data-testid="account-panel-performance"]').text()).toContain("跟单成功率");

    // Read-only invariant across all mounted panels: no close/cancel/leverage write controls
    const buttonTexts = wrapper.findAll("button").map(btn => btn.text()).join(" | ");
    expect(buttonTexts).not.toMatch(/平仓|撤单|改杠杆|Close Position|Cancel Order/i);
  });

  it("未知、从未同步和过期账户不伪造零值，也不混淆空挂单与未拉取挂单", async () => {
    const router = createTestRouter();
    await router.push("/accounts/4"); // Awaiting Sync: never synced, null fields
    await router.isReady();
    const wrapper = mount(AccountDetailView, { global: { plugins: [router] } });
    mounted.push(wrapper);
    await flushPromises();

    expect(wrapper.text()).toContain("Awaiting Sync");
    expect(wrapper.text()).toContain("尚未成功拉取当前挂单");
    expect(wrapper.text()).toContain("—");
    expect(wrapper.text()).not.toContain("$0.00");

    // Switch to account 3 (stale open orders)
    await router.push("/accounts/3");
    await flushPromises();
    expect(wrapper.text()).toContain("Reserve Paper");
    expect(wrapper.text()).toContain("当前挂单快照已过期");
  });

  it("旧链接 /accounts?account=<id> 保持兼容，且账户列表详情入口跳转到 /accounts/:id", async () => {
    const cache = useDataCacheStore();
    cache.accounts = {
      data: (mockResponse("/api/accounts") as { items: never[] }).items,
      fetchedAt: Date.now(),
    };
    const router = createTestRouter();
    await router.push("/accounts?account=2");
    await router.isReady();
    const wrapper = mount(AccountsView, { global: { plugins: [router], stubs: { Teleport: true } } });
    mounted.push(wrapper);
    await flushPromises();

    // Legacy ?account=2 still expands account row 2
    expect(wrapper.find("table").text()).toContain("BTCUSDT");

    // Clicking "详情" jumps to /accounts/:id
    const detailBtn = wrapper.findAll("button").find(b => b.text().includes("详情"));
    expect(detailBtn).toBeDefined();
    await detailBtn!.trigger("click");
    await flushPromises();
    expect(router.currentRoute.value.path).toMatch(/^\/accounts\/\d+$/);
  });

  it("并发或过期响应不覆盖最新范围筛选结果，页面隐藏或卸载后停止轮询", async () => {
    let resolveSlow!: (v: unknown) => void;
    let resolveFast!: (v: unknown) => void;
    let equityCall = 0;

    vi.mocked(api.get).mockImplementation((endpoint: string) => {
      if (endpoint.includes("/equity")) {
        equityCall += 1;
        if (equityCall === 1) {
          return new Promise(resolve => { resolveSlow = resolve; }) as never;
        }
        if (equityCall === 2) {
          return new Promise(resolve => { resolveFast = resolve; }) as never;
        }
      }
      return Promise.resolve(mockResponse(endpoint)) as never;
    });

    const router = createTestRouter();
    await router.push("/accounts/2");
    await router.isReady();
    const wrapper = mount(AccountDetailView, { global: { plugins: [router] } });
    mounted.push(wrapper);
    await flushPromises();

    // Switch to equity tab to mount range controls, then switch range to 7d before 30d finishes
    await wrapper.get('[data-testid="account-tab-equity"]').trigger("click");
    await flushPromises();
    const btn7d = wrapper.findAll("button").find(b => b.text() === "7天");
    expect(btn7d).toBeDefined();
    await btn7d!.trigger("click");
    await flushPromises();

    // Resolve fast 7d response first, then slow 30d response late
    const eq7d = mockResponse("/api/accounts/2/equity?range=7d") as { summary: { peakEquity: number } };
    eq7d.summary.peakEquity = 77777;
    resolveFast(eq7d);
    await flushPromises();

    const eq30d = mockResponse("/api/accounts/2/equity?range=30d") as { range: string };
    resolveSlow(eq30d);
    await flushPromises();

    // Check hidden document stops 15s live polling
    const beforeHiddenCalls = vi.mocked(api.get).mock.calls.length;
    Object.defineProperty(document, "hidden", { value: true, configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
    vi.advanceTimersByTime(35_000);
    await flushPromises();
    expect(vi.mocked(api.get).mock.calls.length).toBe(beforeHiddenCalls);

    // Restore visibility & unmount stops polling too
    Object.defineProperty(document, "hidden", { value: false, configurable: true });
    wrapper.unmount();
    mounted.pop();
    const afterUnmountCalls = vi.mocked(api.get).mock.calls.length;
    vi.advanceTimersByTime(35_000);
    await flushPromises();
    expect(vi.mocked(api.get).mock.calls.length).toBe(afterUnmountCalls);
  });

  it("手动刷新调用 POST /detail/sync 并重新拉取详情分区", async () => {
    const router = createTestRouter();
    await router.push("/accounts/2");
    await router.isReady();
    const wrapper = mount(AccountDetailView, { global: { plugins: [router] } });
    mounted.push(wrapper);
    await flushPromises();

    await wrapper.get('[data-testid="sync-detail-btn"]').trigger("click");
    await flushPromises();
    expect(api.post).toHaveBeenCalledWith("/api/accounts/2/detail/sync");
    expect(wrapper.text()).toContain("详情数据已同步");
  });
});
