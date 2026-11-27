import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import AccountsView from "@/views/AccountsView.vue";
import ProxiesView from "@/views/ProxiesView.vue";
import NotificationSettingsPanel from "@/components/settings/NotificationSettingsPanel.vue";
import { api, ApiError } from "@/api/client";
import { useDataCacheStore } from "@/stores/dataCache";
import { useBoardStore } from "@/stores/board";
import { useUiStore } from "@/stores/ui";
import { useI18n } from "@/i18n";
import { mockAccounts, mockBindings, mockBoard, mockLeads, mockProxies, mockSettings } from "@/mocks/board";
import type { Account } from "@/types/api";

// 用同一份验收样例检查列表状态、未知数值与代理健康结果。
let wrapper: ReturnType<typeof mount> | undefined;
beforeEach(() => {
  setActivePinia(createPinia());
  const cache = useDataCacheStore();
  cache.accounts = { data: structuredClone(mockAccounts), fetchedAt: Date.now() };
  cache.proxies = { data: structuredClone(mockProxies), fetchedAt: Date.now() };
  cache.leads = { data: structuredClone(mockLeads), fetchedAt: Date.now() };
  cache.bindings = { data: { items: structuredClone(mockBindings), globalKillSwitch: false }, fetchedAt: Date.now() };
  vi.spyOn(cache, "fetchAccounts").mockResolvedValue(cache.accounts.data);
  vi.spyOn(cache, "fetchProxies").mockResolvedValue(cache.proxies.data);
  vi.spyOn(cache, "fetchLeads").mockResolvedValue(cache.leads.data);
  vi.spyOn(cache, "fetchBindings").mockResolvedValue(cache.bindings.data);
  cache.board = { data: structuredClone(mockBoard), fetchedAt: Date.now() };
  vi.spyOn(useBoardStore(), "refresh").mockResolvedValue(undefined);
  // 数量口径：账户验收断言数量文本与数量单位说明，显示单位固定为数量模式。
  const settings = structuredClone(mockSettings);
  settings.notifications!.display_unit = "quantity";
  cache.settings = { data: settings, fetchedAt: Date.now() };
  vi.spyOn(cache, "fetchSettings").mockResolvedValue(settings);
});
afterEach(() => { wrapper?.unmount(); wrapper = undefined; useUiStore().clear(); vi.restoreAllMocks(); document.body.innerHTML = ""; });
async function render(component: typeof AccountsView | typeof ProxiesView) {
  wrapper = mount(component, { global: { stubs: { Teleport: true } } });
  await flushPromises(); return wrapper;
}

describe("账户列表", () => {
  it("桌面和手机账户名都有收缩与无空格换行约束", async () => {
    const cache = useDataCacheStore();
    const name = "A".repeat(120);
    const account: Account = { ...cache.accounts!.data[0], name };
    cache.accounts!.data = [account];
    vi.mocked(cache.fetchAccounts).mockResolvedValue([account]);
    const rendered = await render(AccountsView);
    // 同一插槽同时出现在桌面表格和手机卡片，两个布局都不能漏掉约束。
    const names = rendered.findAll("span").filter(span => span.text() === name);
    expect(names).toHaveLength(2);
    for (const span of names) {
      expect(span.classes()).toEqual(expect.arrayContaining(["min-w-0", "flex-1", "[overflow-wrap:anywhere]"]));
    }
  });

  it("异常行优先且未知资金不补零，展开显示真实持仓比例", async () => {
    const rendered = await render(AccountsView);
    const rows = rendered.find("table").findAll("tbody > tr");
    expect(rows[0].classes().join(" ")).toMatch(/bg-(danger|warning)/);
    const awaiting = rows.find(row => row.text().includes("Awaiting Sync"))!;
    expect(awaiting.text()).toContain(useI18n().t("accts.syncMissing"));
    expect(awaiting.text()).not.toContain("$0.00");
    const paper = rows.find(row => row.text().includes("Paper Alpha"))!;
    await paper.trigger("click");
    expect(rendered.find("table").text()).toContain("+4.66%");
    expect(rendered.find("table").text()).toContain(useI18n().t("accts.contractQty"));
  });

  it("支持 ?account= 参数直达展开持仓、跟随看板刷新同步持仓及内联操作按钮", async () => {
    const cache = useDataCacheStore();
    const board = useBoardStore();
    const { t } = useI18n();
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: "/accounts", component: AccountsView }],
    });
    await router.push("/accounts?account=2");
    await router.isReady();
    wrapper = mount(AccountsView, { global: { plugins: [router], stubs: { Teleport: true } } });
    await flushPromises();

    // 自动展开 2 号账户持仓（ADAUSDT）且直接展示内联图标+短字按钮
    const tableText = wrapper.find("table").text();
    expect(tableText).toContain("ADAUSDT");
    expect(tableText).toContain(t("accts.testShort"));
    expect(tableText).toContain(t("accts.syncShort"));
    expect(tableText).toContain(t("common.delete"));

    // 看板时间戳推进时，账户持仓随账户盈亏一起重新拉取
    const callsBefore = vi.mocked(cache.fetchAccounts).mock.calls.length;
    cache.board = { data: structuredClone(mockBoard), fetchedAt: Date.now() + 5000 };
    await flushPromises();
    expect(vi.mocked(cache.fetchAccounts).mock.calls.length).toBeGreaterThan(callsBefore);
    expect(vi.mocked(cache.fetchAccounts)).toHaveBeenLastCalledWith(true);
    void board;
  });

  it("取消启用确认时checkbox与服务器现态一致", async () => {
    const put = vi.spyOn(api, "put");
    const rendered = await render(AccountsView);
    const row = rendered.find("table").findAll("tbody > tr").find(item => item.text().includes("Reserve Paper"))!;
    const toggle = row.find('input[role="switch"]');
    await toggle.setValue(true);
    expect(put).not.toHaveBeenCalled();
    useUiStore().close(); await flushPromises();
    expect((toggle.element as HTMLInputElement).checked).toBe(false);
  });

  it("负数量净仓显示空头，小数量不舍入成零", async () => {
    const cache = useDataCacheStore();
    const account: Account = { ...cache.accounts!.data[0], positions: [
      { instId: "BTCUSDC", posSide: "bOtH", pos: -0.0000012, avgPx: null, curPx: null, upl: null, uplRatio: null },
    ] };
    cache.accounts!.data = [account];
    vi.mocked(cache.fetchAccounts).mockResolvedValue([account]);
    const rendered = await render(AccountsView);
    await rendered.find("table").find("tbody > tr").trigger("click");
    const detail = rendered.find("table").find(".divide-y");
    expect(detail.text()).toContain(useI18n().t("side.SHORT"));
    expect(detail.text()).toContain("0.0000012");
    expect(detail.text()).not.toContain("$0.00");
  });
});

describe("通知渠道", () => {
  it("未知启用状态显示警示且不误报停用，已知状态仍正确显示", async () => {
    const post = vi.spyOn(api, "post");
    const notification = { ...structuredClone(mockSettings.notifications!), webhook_enabled: null };
    wrapper = mount(NotificationSettingsPanel, { props: { modelValue: notification, errors: {}, dirty: false } });
    const summary = () => wrapper!.findAll("summary")[0];
    expect(summary().text()).toContain(useI18n().t("state.UNKNOWN"));
    expect(summary().text()).not.toContain(useI18n().t("common.disable"));
    expect(summary().findAll("span")[1].classes()).toContain("text-warning");
    expect(wrapper.findAll("details")[0].find("button").attributes("disabled")).toBeDefined();
    expect(post).not.toHaveBeenCalled();
    await wrapper.setProps({ modelValue: { ...notification, webhook_enabled: false } });
    expect(summary().text()).toContain(useI18n().t("common.disable"));
    expect(summary().text()).not.toContain(useI18n().t("state.UNKNOWN"));
    await wrapper.setProps({ modelValue: { ...notification, webhook_enabled: true } });
    expect(summary().text()).toContain(useI18n().t("common.enable"));
    expect(summary().text()).not.toContain(useI18n().t("state.UNKNOWN"));
  });
  it("明确停用的渠道不展示配置项与测试按钮", () => {
    const notification = { ...structuredClone(mockSettings.notifications!), webhook_enabled: false };
    wrapper = mount(NotificationSettingsPanel, { props: { modelValue: notification, errors: {}, dirty: false } });
    const first = wrapper.findAll("details")[0];
    expect(first.find('input[name="notifications.webhook_url"]').exists()).toBe(false);
    expect(first.find("button").exists()).toBe(false);
    expect(first.find('input[type="checkbox"]').exists()).toBe(true);
  });
});

describe("代理列表", () => {
  it("隐藏URL账号密码并展示异常、引用账户与带单员", async () => {
    const cache = useDataCacheStore();
    cache.proxies!.data[0].url = "http://sample-user:sample-pass@proxy-a.invalid:8080";
    const rendered = await render(ProxiesView);
    const table = rendered.find("table");
    expect(table.find("tbody > tr").text()).toContain("Backup Egress");
    expect(table.text()).not.toContain("sample-user");
    expect(table.text()).not.toContain("sample-pass");
    expect(table.text()).toContain("Main Futures");
    expect(table.text()).toContain("Delta Range");
    expect(table.text()).toContain(useI18n().t("errors.timeout"));
  });

  it("LIVE账户引用代理的启停必须等待确认", async () => {
    const put = vi.spyOn(api, "put");
    const rendered = await render(ProxiesView);
    const row = rendered.find("table").findAll("tbody > tr").find(item => item.text().includes("Primary Egress"))!;
    const toggle = row.find('input[role="switch"]');
    await toggle.setValue(false);
    expect(put).not.toHaveBeenCalled();
    expect(useUiStore().confirmation?.environments).toContain("live");
    useUiStore().close(); await flushPromises();
    expect((toggle.element as HTMLInputElement).checked).toBe(true);
  });

  it("全部检测统计真实健康结果，部分失败不报成功", async () => {
    const post = vi.spyOn(api, "post").mockImplementation(async endpoint => {
      if (endpoint === "/api/proxies/3/test") throw new ApiError(408, "REQUEST_TIMEOUT");
      return { item: null, test: { healthy: endpoint === "/api/proxies/1/test", latencyMs: 80, error: "REQUEST_TIMEOUT" } };
    });
    const rendered = await render(ProxiesView);
    const button = rendered.findAll("button").find(item => item.text() === useI18n().t("proxy.testAll"))!;
    await button.trigger("click"); await flushPromises();
    expect(post).toHaveBeenCalledTimes(3);
    expect(rendered.find('[role="status"]').text()).toBe(useI18n().t("proxy.batchResult", { n: 3, passed: 1, failed: 2 }));
    expect(useUiStore().toasts.some(toast => toast.tone === "danger")).toBe(true);
    expect(useUiStore().toasts.some(toast => toast.tone === "success")).toBe(false);
  });

  it("编辑不可见地址的代理只保存其它字段", async () => {
    const put = vi.spyOn(api, "put").mockResolvedValue({});
    const rendered = await render(ProxiesView);
    const row = rendered.find("table").findAll("tbody > tr").find(item => item.text().includes("Unverified Egress"))!;
    await row.find(`button[aria-label="${useI18n().t("common.edit")}"]`).trigger("click");
    await rendered.find('input[name="name"]').setValue("Renamed egress");
    await rendered.find("form").trigger("submit");
    // 操作先进入真实待决确认，再由接受动作放行请求。
    expect(put).not.toHaveBeenCalled();
    expect(useUiStore().confirmation).not.toBeNull();
    useUiStore().accept(); await flushPromises();
    expect(put).toHaveBeenCalledWith("/api/proxies/3", { name: "Renamed egress", region: "AUTO", priority: 3 });
    expect(put.mock.calls[0][1]).not.toHaveProperty("url");
  });
});
