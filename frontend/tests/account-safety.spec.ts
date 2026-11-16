import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import AccountFormModal from "@/components/AccountFormModal.vue";
import { api } from "@/api/client";
import { useUiStore } from "@/stores/ui";
import type { Account, AccountTestResult } from "@/types/api";

// 新账户测试必须停用保存，已有账户编辑不能回写凭证或身份字段。
const account: Account = {
  id: 41, name: "Sample", exchange: "okx", environment: "live", proxyId: null,
  isEnabled: false, status: "connected",
};
let wrapper: ReturnType<typeof mount> | undefined;
beforeEach(() => { setActivePinia(createPinia()); });
afterEach(() => { wrapper?.unmount(); wrapper = undefined; useUiStore().clear(); vi.restoreAllMocks(); document.body.innerHTML = ""; });
function form(editing: Account | null = null) {
  wrapper = mount(AccountFormModal, {
    props: { open: true, account: editing, proxies: [] }, attachTo: document.body,
    global: { stubs: { Teleport: true } },
  });
  return wrapper;
}
async function newAccountForm() {
  const rendered = form();
  await rendered.find('input[name="name"]').setValue("Test paper");
  await rendered.find('input[name="api_key"]').setValue("sample-key");
  await rendered.find('input[name="secret_key"]').setValue("sample-secret");
  await rendered.find('input[name="passphrase"]').setValue("sample-pass");
  return rendered;
}
// 延迟网络响应制造注销时序，验证没有跨会话续跑或界面回写。
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(complete => { resolve = complete; });
  return { promise, resolve };
}
describe("账户表单", () => {
  it("新建只提供OKX和币安", () => {
    const rendered = form();
    const values = rendered.findAll("select")[0].findAll("option").map(option => option.element.value);
    expect(values).toEqual(["okx", "binance"]);
  });

  it("LIVE编辑等待确认且只回写name/proxy", async () => {
    const put = vi.spyOn(api, "put").mockResolvedValue({ item: account });
    const ui = useUiStore();
    const rendered = form(account);
    await rendered.find('input[name="name"]').setValue("Renamed");
    await rendered.find("form").trigger("submit");
    expect(put).not.toHaveBeenCalled();
    expect(ui.confirmation?.environment).toBe("live");
    ui.accept(); await flushPromises();
    expect(put).toHaveBeenCalledWith("/api/accounts/41", { name: "Renamed", proxyId: null });
    expect(rendered.emitted("saved")).toHaveLength(1);
  });

  it("新建并测试只创建一次且保持停用，结果留在表单", async () => {
    const post = vi.spyOn(api, "post").mockImplementation(async (endpoint: string) =>
      endpoint === "/api/accounts" ? { item: { ...account, environment: "demo" } }
        : { ok: true, totalEquity: 1500, posMode: "hedge" });
    vi.spyOn(api, "put").mockResolvedValue({ item: { ...account, environment: "demo" } });
    const rendered = await newAccountForm();
    await rendered.find('[data-testid="test-account"]').trigger("click"); await flushPromises();
    expect(post.mock.calls[0][1]).toHaveProperty("isEnabled", false);
    expect(post.mock.calls.map(call => call[0])).toEqual(["/api/accounts", "/api/accounts/41/test"]);
    expect(rendered.find('[role="status"]').exists()).toBe(true);
    expect(rendered.emitted("close")).toBeUndefined();
    await rendered.find('[data-testid="test-account"]').trigger("click");
    expect(useUiStore().confirmation).not.toBeNull();
    useUiStore().accept(); await flushPromises();
    expect(post.mock.calls.filter(call => call[0] === "/api/accounts")).toHaveLength(1);
    expect(post.mock.calls.filter(call => call[0] === "/api/accounts/41/test")).toHaveLength(2);
  });

  it("退出后迟到的创建响应不触发测试或污染新会话", async () => {
    const creation = deferred<{ item: Account }>();
    const post = vi.spyOn(api, "post").mockImplementation(async endpoint =>
      endpoint === "/api/accounts" ? creation.promise : { ok: true, totalEquity: 1500 });
    const rendered = await newAccountForm();
    await rendered.find('[data-testid="test-account"]').trigger("click"); await flushPromises();
    expect(post).toHaveBeenCalledOnce();
    useUiStore().clear();
    creation.resolve({ item: { ...account, environment: "demo" } });
    await flushPromises();
    expect(post.mock.calls.map(call => call[0])).toEqual(["/api/accounts"]);
    expect(rendered.emitted("saved")).toBeUndefined();
    expect(rendered.emitted("close")).toBeUndefined();
    expect(rendered.find('[role="status"]').exists()).toBe(false);
    expect(rendered.find('input[name="api_key"]').exists()).toBe(true);
    expect((rendered.find('input[name="api_key"]').element as HTMLInputElement).value).toBe("");
    expect(useUiStore().toasts).toHaveLength(0);
  });

  it("退出后迟到的连接结果不回写或再次发出saved", async () => {
    const connection = deferred<AccountTestResult>();
    const post = vi.spyOn(api, "post").mockImplementation(async endpoint =>
      endpoint === "/api/accounts" ? { item: { ...account, environment: "demo" } } : connection.promise);
    const rendered = await newAccountForm();
    await rendered.find('[data-testid="test-account"]').trigger("click"); await flushPromises();
    expect(post.mock.calls.map(call => call[0])).toEqual(["/api/accounts", "/api/accounts/41/test"]);
    expect(rendered.emitted("saved")).toHaveLength(1);
    useUiStore().clear();
    connection.resolve({ ok: true, totalEquity: 1500 });
    await flushPromises();
    expect(rendered.emitted("saved")).toHaveLength(1);
    expect(rendered.find('[role="status"]').exists()).toBe(false);
    expect(rendered.find('input[name="api_key"]').exists()).toBe(true);
    expect(useUiStore().toasts).toHaveLength(0);
  });

  it("退出后迟到的保存响应不发出saved或close", async () => {
    const creation = deferred<{ item: Account }>();
    vi.spyOn(api, "post").mockReturnValue(creation.promise);
    const rendered = await newAccountForm();
    await rendered.find("form").trigger("submit"); await flushPromises();
    useUiStore().clear();
    creation.resolve({ item: { ...account, environment: "demo" } });
    await flushPromises();
    expect(rendered.emitted("saved")).toBeUndefined();
    expect(rendered.emitted("close")).toBeUndefined();
    expect(useUiStore().toasts).toHaveLength(0);
  });

  it("取消测试前的确认不会伪造失败结果", async () => {
    const post = vi.spyOn(api, "post");
    const rendered = form(account);
    await rendered.find('[data-testid="test-account"]').trigger("click");
    expect(post).not.toHaveBeenCalled();
    useUiStore().close(); await flushPromises();
    expect(rendered.find('[role="status"]').exists()).toBe(false);
  });

  it("代理列表不可用时保留已有代理，不显示为直连", () => {
    const rendered = form({ ...account, proxyId: 91 });
    const select = rendered.find("select");
    expect((select.element as HTMLSelectElement).value).toBe("91");
    expect(select.findAll("option")[1].text()).toContain("#91");
  });
});
