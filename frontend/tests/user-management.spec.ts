import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import App from "@/App.vue";
import RegisterView from "@/views/RegisterView.vue";
import UsersView from "@/views/UsersView.vue";
import { api, ApiError } from "@/api/client";
import { useAuthStore } from "@/stores/auth";
import { useBoardStore } from "@/stores/board";
import { useSocketStore } from "@/stores/socket";
import { useDataCacheStore } from "@/stores/dataCache";
import { useUiStore } from "@/stores/ui";
import { useI18n } from "@/i18n";
import { guardRoute, type GuardAuth } from "@/router/guard";
import { routes } from "@/router";
import type { AdminUserDetail, AdminUserSummary, InviteCode } from "@/types/api";

const { t } = useI18n();
vi.stubGlobal("__APP_VERSION__", "1.3.2");

const mockUsers: AdminUserSummary[] = [
  { id: 1, username: "alice", role: "admin", isActive: true, createdAt: "2026-11-01T00:00:00Z", lastLoginAt: "2026-11-20T10:00:00Z", accountCount: 2, bindingCount: 1, enabledBindingCount: 1, openPositionCount: 1, totalEquity: 395.95 },
  { id: 2, username: "bob", role: "user", isActive: false, createdAt: "2026-11-02T00:00:00Z", lastLoginAt: null, accountCount: 0, bindingCount: 0, enabledBindingCount: 0, openPositionCount: 0, totalEquity: null },
];
const mockInvites: InviteCode[] = [
  { id: 10, code: "INV-ABC123", createdByUserId: 1, createdByUsername: "alice", maxUses: 1, usedCount: 0, expiresAt: null, isRevoked: false, createdAt: "2026-11-20T00:00:00Z", status: "active" },
];
const mockDetail: AdminUserDetail = {
  user: mockUsers[0],
  accounts: [{ id: 5, name: "带单", exchange: "binance", environment: "live", status: "ok", isEnabled: true, equity: 395.95 }],
  bindings: [{ id: 3, name: "熬鹰—带单同步", leadId: 1, leadName: "熬鹰资本", accountId: 5, accountName: "带单", isEnabled: true, openPositions: [{ instId: "ZECUSDT", posSide: "SHORT", qty: 0.904 }] }],
  subscriptions: [{ leadId: 1, leadName: "熬鹰资本", isEnabled: true }],
};

let pinia: ReturnType<typeof createPinia>;
const wrappers: ReturnType<typeof mount>[] = [];
beforeEach(() => {
  pinia = createPinia(); setActivePinia(pinia);
  useAuthStore().initialized = true;
});
afterEach(() => {
  for (const wrapper of wrappers.splice(0)) wrapper.unmount();
  useUiStore().clear();
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});
function makeRouter(path: string) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/login", component: { template: "<div />" } },
      { path: "/register", component: RegisterView },
      { path: "/dashboard", component: { template: "<div />" } },
      { path: "/users", component: UsersView },
    ],
  });
  void router.push(path);
  return router;
}
async function renderRegister(path = "/register") {
  const router = makeRouter(path);
  await router.isReady();
  const wrapper = mount(RegisterView, { global: { plugins: [pinia, router], stubs: { Teleport: true } } });
  wrappers.push(wrapper);
  await flushPromises();
  return { wrapper, router };
}
async function renderUsers() {
  vi.spyOn(api, "get").mockImplementation(async endpoint => {
    if (endpoint === "/api/admin/users") return { items: structuredClone(mockUsers) };
    if (endpoint === "/api/admin/invite-codes") return { items: structuredClone(mockInvites) };
    if (endpoint === "/api/admin/users/1") return structuredClone(mockDetail);
    throw new ApiError(404, "NOT_FOUND");
  });
  const router = makeRouter("/users");
  await router.isReady();
  const wrapper = mount(UsersView, { global: { plugins: [pinia, router], stubs: { Teleport: true } } });
  wrappers.push(wrapper);
  await flushPromises();
  return wrapper;
}
async function renderApp(role: string) {
  const auth = useAuthStore();
  auth.authenticated = true;
  auth.user = { id: 1, username: role === "admin" ? "alice" : "bob", role };
  vi.spyOn(useBoardStore(), "refresh").mockResolvedValue(undefined);
  vi.spyOn(useDataCacheStore(), "fetchIntents").mockResolvedValue([]);
  vi.spyOn(useSocketStore(), "connect").mockImplementation(() => undefined);
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/dashboard", component: { template: "<div>board</div>" } },
      { path: "/users", component: { template: "<div>users</div>" } },
      { path: "/leads", component: { template: "<div>leads</div>" } },
      { path: "/executions", component: { template: "<div />" } },
      { path: "/accounts", component: { template: "<div />" } },
      { path: "/bindings", component: { template: "<div />" } },
      { path: "/proxies", component: { template: "<div />" } },
      { path: "/settings", component: { template: "<div />" } },
      { path: "/public", component: { template: "<div />" } },
      { path: "/login", component: { template: "<div />" } },
    ],
  });
  await router.push("/dashboard");
  await router.isReady();
  const wrapper = mount(App, { global: { plugins: [pinia, router], stubs: { Teleport: true } } });
  wrappers.push(wrapper);
  await flushPromises();
  return wrapper;
}

describe("注册", () => {
  it("store 注册成功后置位会话，不存 token", async () => {
    const post = vi.spyOn(api, "post").mockResolvedValue({ user: { id: 7, username: "carol", role: "user" }, token: "opaque" });
    const store = useAuthStore();
    expect(await store.register("carol", "example-password", "INV-ABC123")).toBe(true);
    expect(post).toHaveBeenCalledWith("/api/auth/register", { username: "carol", password: "example-password", inviteCode: "INV-ABC123" });
    expect(store.authenticated).toBe(true);
    expect(store.user?.username).toBe("carol");
  });
  it("邀请码无效报 400、用户名已存在报 409，各有本地化原因", async () => {
    const store = useAuthStore();
    vi.spyOn(api, "post")
      .mockRejectedValueOnce(new ApiError(400, "Invalid invite code"))
      .mockRejectedValueOnce(new ApiError(409, "Username exists"));
    expect(await store.register("carol", "example-password", "BAD")).toBe(false);
    expect(store.error).toBe(t("register.invalidInvite"));
    expect(store.authenticated).toBe(false);
    expect(await store.register("carol", "example-password", "INV-ABC123")).toBe(false);
    expect(store.error).toBe(t("register.usernameTaken"));
  });

  it("注册页提交后进入看板", async () => {
    const post = vi.spyOn(api, "post").mockResolvedValue({ user: { id: 7, username: "carol", role: "user" } });
    const { wrapper, router } = await renderRegister();
    await wrapper.find("#register-username").setValue("carol");
    await wrapper.find("#register-password").setValue("example-password");
    await wrapper.find("#register-inviteCode").setValue("INV-ABC123");
    await wrapper.find("form").trigger("submit");
    await flushPromises();
    expect(post).toHaveBeenCalledWith("/api/auth/register", { username: "carol", password: "example-password", inviteCode: "INV-ABC123" });
    expect(router.currentRoute.value.path).toBe("/dashboard");
  });
  it("注册页缺邀请码不发请求并标记字段", async () => {
    const post = vi.spyOn(api, "post");
    const { wrapper } = await renderRegister();
    await wrapper.find("#register-username").setValue("carol");
    await wrapper.find("#register-password").setValue("example-password");
    await wrapper.find("form").trigger("submit");
    await flushPromises();
    expect(post).not.toHaveBeenCalled();
    expect(wrapper.find("#register-inviteCode-error").text()).toBe(t("validation.required"));
  });
  it("注册页用户名过短、密码过短不发请求", async () => {
    const post = vi.spyOn(api, "post");
    const { wrapper } = await renderRegister();
    await wrapper.find("#register-username").setValue("ab");
    await wrapper.find("#register-password").setValue("short");
    await wrapper.find("#register-inviteCode").setValue("INV-ABC123");
    await wrapper.find("form").trigger("submit");
    await flushPromises();
    expect(post).not.toHaveBeenCalled();
    expect(wrapper.find("#register-username-error").text()).toBe(t("auth.usernameLength"));
    expect(wrapper.find("#register-password-error").text()).toBe(t("auth.passwordLength"));
  });
});

describe("用户管理页", () => {
  it("列表展示跟单摘要与邀请码", async () => {
    const wrapper = await renderUsers();
    const table = wrapper.find("table");
    expect(table.text()).toContain("alice");
    expect(table.text()).toContain("bob");
    expect(table.text()).toContain("$395.95");
    expect(table.text()).toContain(t("users.roleAdmin"));
    expect(wrapper.text()).toContain("INV-ABC123");
    expect(wrapper.text()).toContain(t("users.summary", { n: 2 }));
  });
  it("点一行展开详情：账户、绑定持仓与带单员订阅", async () => {
    const wrapper = await renderUsers();
    const row = wrapper.find("table").findAll("tbody > tr").find(item => item.text().includes("alice"))!;
    await row.trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("带单");
    expect(wrapper.text()).toContain("熬鹰—带单同步");
    expect(wrapper.text()).toContain("ZECUSDT");
    expect(wrapper.text()).toContain("0.904");
    expect(wrapper.text()).toContain("熬鹰资本");
  });
  it("删除被 409 挡住时原样展示后端未关项，用户名仍在列表", async () => {
    const detail = "绑定 #3 仍启用\nZECUSDT 空 0.904 未平";
    const remove = vi.spyOn(api, "delete").mockRejectedValue(new ApiError(409, "Has open positions", { detail }));
    const wrapper = await renderUsers();
    const row = wrapper.find("table").findAll("tbody > tr").find(item => item.text().includes("alice"))!;
    await row.find(`button[aria-label="${t("common.delete")}"]`).trigger("click");
    await flushPromises();
    expect(remove).not.toHaveBeenCalled();
    useUiStore().accept();
    await flushPromises();
    expect(remove).toHaveBeenCalledWith("/api/admin/users/1");
    expect(wrapper.text()).toContain(t("users.deleteBlockedTitle"));
    expect(wrapper.text()).toContain("绑定 #3 仍启用");
    expect(wrapper.text()).toContain("ZECUSDT 空 0.904 未平");
    expect(wrapper.find("table").text()).toContain("alice");
  });
  it("重置密码弹框提交新密码", async () => {
    const post = vi.spyOn(api, "post").mockResolvedValue({ ok: true });
    const wrapper = await renderUsers();
    const row = wrapper.find("table").findAll("tbody > tr").find(item => item.text().includes("bob"))!;
    await row.find(`button[aria-label="${t("users.resetPassword")}"]`).trigger("click");
    await flushPromises();
    await wrapper.find("#reset-password").setValue("new-password-123");
    await wrapper.find('[role="dialog"] form').trigger("submit");
    await flushPromises();
    expect(post).toHaveBeenCalledWith("/api/admin/users/2/reset-password", { newPassword: "new-password-123" });
  });
  it("停用/启用与改角色走 PATCH 并经确认", async () => {
    const request = vi.spyOn(api, "request").mockImplementation(async () => ({ user: { ...mockUsers[1], isActive: true } }));
    const wrapper = await renderUsers();
    const enable = wrapper.findAll("button").find(item => item.text() === t("common.enable"))!;
    await enable.trigger("click");
    await flushPromises();
    expect(request).not.toHaveBeenCalled();
    useUiStore().accept();
    await flushPromises();
    expect(request).toHaveBeenCalledWith("/api/admin/users/2", { method: "PATCH", body: JSON.stringify({ isActive: true }) });
    expect(wrapper.find("table").text()).toContain(t("users.active"));
  });
  it("生成邀请码带次数与过期时间，作废走 revoke", async () => {
    const post = vi.spyOn(api, "post").mockImplementation(async endpoint => {
      if (endpoint === "/api/admin/invite-codes") {
        return { item: { ...mockInvites[0], id: 11, code: "INV-NEW999", maxUses: 5, usedCount: 0, expiresAt: "2026-12-01T00:00:00.000Z" } };
      }
      return { item: { ...mockInvites[0], isRevoked: true, status: "revoked" } };
    });
    const wrapper = await renderUsers();
    await wrapper.find('input[name="maxUses"]').setValue(5);
    await wrapper.find('input[name="expiresAt"]').setValue("2026-12-01T00:00");
    await wrapper.find("section form").trigger("submit");
    await flushPromises();
    expect(post).toHaveBeenCalledWith("/api/admin/invite-codes", { maxUses: 5, expiresAt: new Date("2026-12-01T00:00").toISOString() });
    expect(wrapper.text()).toContain("INV-NEW999");
    const revoke = wrapper.findAll("button").find(item => item.text() === t("invites.revoke"))!;
    await revoke.trigger("click");
    await flushPromises();
    useUiStore().accept();
    await flushPromises();
    expect(post).toHaveBeenCalledWith("/api/admin/invite-codes/11/revoke");
  });
});

describe("管理员路由与导航守门", () => {
  it("路由表：/users 仅管理员、/register 全屏不要求登录", () => {
    const users = routes.find(route => route.path === "/users");
    expect(users?.meta).toMatchObject({ requiresAuth: true, requiresAdmin: true });
    const register = routes.find(route => route.path === "/register");
    expect(register?.meta).toMatchObject({ fullscreen: true });
    expect(register?.meta?.requiresAuth).toBeUndefined();
  });
  it("非管理员进 /users 回看板，管理员放行，未登录先去登录", async () => {
    const adminRoute = { path: "/users", fullPath: "/users", meta: { requiresAuth: true, requiresAdmin: true } };
    const admin: GuardAuth = { initialized: true, authenticated: true, user: { id: 1, username: "alice", role: "admin" }, checkStatus: vi.fn() };
    expect(await guardRoute(adminRoute, admin)).toBe(true);
    const plain: GuardAuth = { initialized: true, authenticated: true, user: { id: 2, username: "bob", role: "user" }, checkStatus: vi.fn() };
    expect(await guardRoute(adminRoute, plain)).toMatchObject({ path: "/dashboard" });
    const guest: GuardAuth = { initialized: true, authenticated: false, user: null, checkStatus: vi.fn() };
    expect(await guardRoute(adminRoute, guest)).toMatchObject({ path: "/login" });
  });
  it("已登录进 /register 回看板，未登录放行", async () => {
    const route = { path: "/register", fullPath: "/register", meta: {} };
    expect(await guardRoute(route, { initialized: true, authenticated: true, checkStatus: vi.fn() })).toMatchObject({ path: "/dashboard" });
    expect(await guardRoute(route, { initialized: true, authenticated: false, checkStatus: vi.fn() })).toBe(true);
  });
  it("管理员导航有用户管理与带单员", async () => {
    const adminApp = await renderApp("admin");
    expect(adminApp.text()).toContain(t("nav.users"));
    expect(adminApp.text()).toContain(t("nav.leads"));
  });
  it("普通用户导航隐藏管理员项，只剩看板、账户、绑定、执行记录与公开监控", async () => {
    const userApp = await renderApp("user");
    expect(userApp.text()).not.toContain(t("nav.users"));
    expect(userApp.text()).not.toContain(t("nav.leads"));
    expect(userApp.text()).toContain(t("nav.dashboard"));
    expect(userApp.text()).toContain(t("nav.accounts"));
    expect(userApp.text()).toContain(t("nav.bindings"));
    expect(userApp.text()).toContain(t("nav.executions"));
    expect(userApp.text()).toContain(t("nav.publicFeed"));
  });
});
