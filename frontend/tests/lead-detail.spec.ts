import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import { useDataCacheStore } from "@/stores/dataCache";
import { useUiStore } from "@/stores/ui";
import { api } from "@/api/client";
import { useI18n } from "@/i18n";
import { mockBindings, mockBoard, mockLeads } from "@/mocks/board";
import type { LeadSnapshot } from "@/types/api";
import LeadDetailView from "@/views/LeadDetailView.vue";
import LeadsView from "@/views/LeadsView.vue";

vi.mock("@/api/client", async importOriginal => {
  const original = await importOriginal<typeof import("@/api/client")>();
  return { ...original, api: { post: vi.fn(), put: vi.fn(), delete: vi.fn(), resolveUrl: vi.fn((path: string) => path) } };
});
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.post).mockResolvedValue({ ok: true });
  setActivePinia(createPinia());
});
function seed() {
  const cache = useDataCacheStore();
  const leads = [{ ...mockLeads[0], id: 1, name: "Lead A" }, { ...mockLeads[0], id: 2, name: "Lead B" }];
  const currentBoard = { ...mockBoard, leads: leads.map(lead => ({ ...mockBoard.leads![0], id: lead.id, name: lead.name })) };
  cache.leads = { data: leads, fetchedAt: Date.now() };
  cache.board = { data: currentBoard, fetchedAt: Date.now() };
  cache.bindings = { data: { items: mockBindings, globalKillSwitch: false }, fetchedAt: Date.now() };
  cache.proxies = { data: [], fetchedAt: Date.now() };
  vi.spyOn(cache, "fetchLeads").mockResolvedValue(leads);
  vi.spyOn(cache, "fetchBoard").mockResolvedValue(currentBoard);
  vi.spyOn(cache, "fetchBindings").mockResolvedValue(cache.bindings.data);
  vi.spyOn(cache, "fetchProxies").mockResolvedValue([]);
  vi.spyOn(cache, "fetchLeadSnapshot").mockResolvedValue({});
  return cache;
}
function router() {
  return createRouter({ history: createMemoryHistory(), routes: [
    { path: "/leads/:id", component: { template: "<div />" } },
    { path: "/leads", component: { template: "<div />" } },
  ] });
}

describe("带单员独立详情", () => {
  it("旧详情书签跳到独立路由，并保留无关筛选参数", async () => {
    seed();
    const navigation = router();
    await navigation.push("/leads?detail=2&filter=enabled");
    await navigation.isReady();
    const page = mount(LeadsView, { global: { plugins: [navigation], stubs: { BaseModal: true } } });
    await flushPromises();
    expect(navigation.currentRoute.value.path).toBe("/leads/2");
    expect(navigation.currentRoute.value.query).toEqual({ filter: "enabled" });
    page.unmount();
  });
  it("快速切换 A/B 时，A 后到的快照不能覆盖 B", async () => {
    const cache = seed();
    const navigation = router();
    await navigation.push("/leads/1");
    await navigation.isReady();
    let resolveA!: (snapshot: LeadSnapshot) => void;
    let resolveB!: (snapshot: LeadSnapshot) => void;
    const a = new Promise<LeadSnapshot>(resolve => { resolveA = resolve; });
    const b = new Promise<LeadSnapshot>(resolve => { resolveB = resolve; });
    vi.spyOn(cache, "fetchLeadSnapshot").mockImplementation(async id => {
      const snapshot = await (id === 1 ? a : b);
      cache.leadSnapshots = { ...cache.leadSnapshots, [id]: { data: snapshot, fetchedAt: Date.now() } };
      return snapshot;
    });
    const page = mount(LeadDetailView, { global: { plugins: [navigation], stubs: {
      LeadComparison: { props: ["lead"], template: '<section data-testid="comparison">{{ lead.name }}</section>' },
      LeadTelemetryPanel: { props: ["snapshot"], template: '<section data-testid="snapshot">{{ snapshot.profile?.nickname }}</section>' },
    } } });
    await flushPromises();
    await navigation.push("/leads/2");
    await flushPromises();
    resolveB({ profile: { nickname: "Snapshot B" } });
    await flushPromises();
    expect(page.get('[data-testid="snapshot"]').text()).toBe("Snapshot B");
    resolveA({ profile: { nickname: "Snapshot A" } });
    await flushPromises();
    expect(page.get('[data-testid="comparison"]').text()).toBe("Lead B");
    expect(page.get('[data-testid="snapshot"]').text()).toBe("Snapshot B");
    page.unmount();
  });
  it("无效详情 ID 有明确空状态，不显示上一位带单员的数据", async () => {
    seed();
    const navigation = router();
    await navigation.push("/leads/not-an-id");
    await navigation.isReady();
    const page = mount(LeadDetailView, { global: { plugins: [navigation] } });
    await flushPromises();
    expect(page.findComponent(LeadDetailView).exists()).toBe(true);
    expect(page.find('[data-testid="mark-price"]').exists()).toBe(false);
    expect(page.find(".empty-state").exists()).toBe(true);
    page.unmount();
  });
  it("今日没有动态时按需补取最近原始订单，保留真实开平方向", async () => {
    const cache = seed();
    const last = 1_800_000_000_000;
    cache.leads!.data[0].lastTradeTime = last;
    cache.leads!.data[1].lastTradeTime = null;
    cache.board!.data.leads!.forEach(lead => { lead.todayEvents = []; });
    vi.mocked(cache.fetchLeadSnapshot).mockImplementation(async id => {
      const snapshot: LeadSnapshot = { profile: { lastTradeTime: last }, orders: [
        { time: last, symbol: "ETHUSDT", dir: "CLOSE_SHORT", qty: 0.1, price: 2200 },
      ] };
      cache.leadSnapshots = { ...cache.leadSnapshots, [id]: { data: snapshot, fetchedAt: Date.now() } };
      return snapshot;
    });
    const navigation = router();
    await navigation.push("/leads");
    await navigation.isReady();
    const page = mount(LeadsView, { global: { plugins: [navigation], stubs: { BaseModal: true } } });
    await flushPromises();
    expect(cache.fetchLeadSnapshot).toHaveBeenCalledWith(1, false, 60_000);
    expect(page.text()).toContain("ETHUSDT");
    expect(page.text()).toContain(useI18n().t("dir.CLOSE_SHORT"));
    page.unmount();
  });
  it("看板和真实仓位列表缺失时不把历史摘要的零显示为空仓", async () => {
    const cache = seed();
    cache.board!.data.leads = [];
    cache.leads!.data.forEach(lead => { lead.lastTradeTime = null; lead.snapshot = null; lead.openPositionsCount = 0; });
    const navigation = router();
    await navigation.push("/leads");
    await navigation.isReady();
    const page = mount(LeadsView, { global: { plugins: [navigation], stubs: { BaseModal: true } } });
    await flushPromises();
    expect(page.get("tbody tr").findAll("td")[2].text()).toBe(useI18n().t("common.unknown"));
    page.unmount();
  });
  it.each([
    { name: "列表", component: LeadsView, path: "/leads" },
    { name: "详情", component: LeadDetailView, path: "/leads/1" },
  ])("$name 手动同步先确认，取消不请求，业务失败不报成功", async ({ component, path }) => {
    seed();
    const navigation = router();
    await navigation.push(path);
    await navigation.isReady();
    const page = mount(component, { global: { plugins: [navigation],
      stubs: { BaseModal: true, LeadComparison: true, LeadTelemetryPanel: true } } });
    await flushPromises();
    const { t } = useI18n();
    const ui = useUiStore();
    const sync = () => page.findAll("button").find(button => button.text() === t("leads.syncNow"))!;
    await sync().trigger("click");
    expect(ui.confirmation?.message).toContain(t("confirm.syncLead", { name: "Lead A" }));
    expect(api.post).not.toHaveBeenCalled();
    ui.close();
    await flushPromises();
    expect(api.post).not.toHaveBeenCalled();
    await sync().trigger("click");
    vi.mocked(api.post).mockResolvedValueOnce({ ok: false, error: "LEAD_SYNC_FAILED" });
    ui.accept();
    await flushPromises();
    expect(api.post).toHaveBeenCalledWith("/api/leads/1/sync");
    expect(page.text()).toContain(t("reason.LEAD_SYNC_FAILED"));
    await sync().trigger("click");
    vi.mocked(api.post).mockResolvedValueOnce(null);
    ui.accept();
    await flushPromises();
    expect(page.text()).toContain(t("reason.LEAD_SYNC_FAILED"));
    expect(page.text()).not.toContain("Cannot read properties");
    page.unmount();
    ui.clear();
  });
});
