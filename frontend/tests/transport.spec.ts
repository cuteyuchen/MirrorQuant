import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { api, ApiError, CancelledRequestError, REQUEST_TIMEOUT_MS, setUnauthorizedHandler } from "@/api/client";
import { useAuthStore } from "@/stores/auth";
import { useDataCacheStore } from "@/stores/dataCache";
import { HEARTBEAT_TIMEOUT_MS, useSocketStore } from "@/stores/socket";
import { guardRoute, safeRedirect, type GuardAuth } from "@/router/guard";
import type { Account, AuthStatus, User } from "@/types/api";

vi.mock("@/api/mode", () => ({ MOCK_MODE: false }));

// 可控承诺让测试明确复现旧请求先后返回，而不依赖真实网络。
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((success, failure) => { resolve = success; reject = failure; });
  return { promise, resolve, reject };
}

class FakeSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  static instances: FakeSocket[] = [];
  readyState = FakeSocket.CONNECTING;
  onopen: ((event: Event) => unknown) | null = null;
  onclose: ((event: CloseEvent) => unknown) | null = null;
  onmessage: ((event: MessageEvent) => unknown) | null = null;
  onerror: ((event: Event) => unknown) | null = null;
  send = vi.fn();
  constructor(public url: string) { FakeSocket.instances.push(this); }
  open() { this.readyState = FakeSocket.OPEN; this.onopen?.(new Event("open")); }
  close() { this.serverClose(1000); }
  serverClose(code: number) { this.readyState = FakeSocket.CLOSED; this.onclose?.({ code } as CloseEvent); }
  message(data: unknown) { this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent); }
}

const user: User = { id: 1, username: "demo", role: "admin" };

beforeEach(() => {
  setActivePinia(createPinia());
  vi.useFakeTimers();
  FakeSocket.instances = [];
  vi.stubGlobal("WebSocket", FakeSocket);
  setUnauthorizedHandler(() => {});
});
afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  setUnauthorizedHandler(() => {});
});

describe("HTTP 安全", () => {
  it("401 响应体损坏也先通知清登录态", async () => {
    const unauthorized = vi.fn();
    setUnauthorizedHandler(unauthorized);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 401, json: () => Promise.reject(new Error("bad json")) }));
    await expect(api.get("/api/private")).rejects.toMatchObject({ status: 401 });
    expect(unauthorized).toHaveBeenCalledOnce();
  });
  it("超时主动终止请求并返回可翻译错误码", async () => {
    let signal: AbortSignal | null = null;
    vi.stubGlobal("fetch", vi.fn((_url: string, options: RequestInit) => {
      signal = options.signal ?? null;
      return new Promise((_resolve, reject) => { signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))); });
    }));
    const result = api.get("/api/slow").catch(error => error);
    await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS);
    expect(signal?.aborted).toBe(true);
    expect(await result).toMatchObject({ status: 408, detail: "REQUEST_TIMEOUT" });
  });
  it("调用方主动取消不会误报为断网", async () => {
    vi.stubGlobal("fetch", vi.fn((_url: string, options: RequestInit) => new Promise((_resolve, reject) => {
      options.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    })));
    const controller = new AbortController();
    const result = api.request("/api/slow", { signal: controller.signal }).catch(error => error);
    controller.abort();
    expect(await result).toBeInstanceOf(CancelledRequestError);
  });
});

describe("实时连接", () => {
  it("主动断开不会在 onclose 后重连", async () => {
    const store = useSocketStore();
    store.connect(); FakeSocket.instances[0]!.open();
    store.disconnect();
    await vi.advanceTimersByTimeAsync(60000);
    expect(FakeSocket.instances).toHaveLength(1);
    expect(store.state).toBe("disconnected");
  });
  it("有待执行重连时登出也会取消重连", async () => {
    const store = useSocketStore();
    store.connect(); FakeSocket.instances[0]!.serverClose(1006);
    store.disconnect();
    await vi.advanceTimersByTimeAsync(60000);
    expect(FakeSocket.instances).toHaveLength(1);
  });
  it("1008 清登录态且不会重连", async () => {
    const unauthorized = vi.fn();
    setUnauthorizedHandler(unauthorized);
    const store = useSocketStore();
    store.connect(); FakeSocket.instances[0]!.open();
    FakeSocket.instances[0]!.serverClose(1008);
    await vi.advanceTimersByTimeAsync(60000);
    expect(unauthorized).toHaveBeenCalledOnce();
    expect(store.state).toBe("disconnected");
    expect(FakeSocket.instances).toHaveLength(1);
  });
  it("连接失败按指数退避，延迟包含可控抖动", async () => {
    vi.spyOn(Math, "random").mockReturnValue(0.5);
    const store = useSocketStore();
    store.connect(); FakeSocket.instances[0]!.serverClose(1006);
    await vi.advanceTimersByTimeAsync(999);
    expect(FakeSocket.instances).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(FakeSocket.instances).toHaveLength(2);
    FakeSocket.instances[1]!.serverClose(1006);
    await vi.advanceTimersByTimeAsync(1999);
    expect(FakeSocket.instances).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(FakeSocket.instances).toHaveLength(3);
  });
  it("心跳失效后主动重连，不等待关闭握手", async () => {
    vi.spyOn(Math, "random").mockReturnValue(0.5);
    const store = useSocketStore();
    store.connect(); FakeSocket.instances[0]!.open();
    await vi.advanceTimersByTimeAsync(HEARTBEAT_TIMEOUT_MS + 5000);
    expect(store.state).toBe("reconnecting");
    await vi.advanceTimersByTimeAsync(1000);
    expect(FakeSocket.instances).toHaveLength(2);
  });
  it("事件数组满 50 条后序号仍继续增长", () => {
    const store = useSocketStore();
    store.connect(); FakeSocket.instances[0]!.open();
    for (let id = 0; id < 55; id++) FakeSocket.instances[0]!.message({ type: "execution", data: { id } });
    expect(store.recentEvents).toHaveLength(50);
    expect(store.eventSequence).toBe(55);
  });
});

describe("缓存竞态", () => {
  it("force 新请求优先，旧视图拿到的也必须是新结果", async () => {
    const first = deferred<{ items: Account[] }>();
    const second = deferred<{ items: Account[] }>();
    vi.spyOn(api, "get").mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const store = useDataCacheStore();
    const oldRequest = store.fetchAccounts();
    const latestRequest = store.fetchAccounts(true);
    const latest: Account[] = [];
    first.resolve({ items: [] });
    second.resolve({ items: latest });
    expect(await latestRequest).toBe(latest);
    expect(await oldRequest).toBe(latest);
    expect(store.accounts?.data).toBe(latest);
  });
  it("登出后在途结果不会重新写回缓存", async () => {
    const pending = deferred<{ items: Account[] }>();
    vi.spyOn(api, "get").mockReturnValue(pending.promise);
    const store = useDataCacheStore();
    const oldRequest = store.fetchAccounts().catch(error => error);
    store.clearAll(); pending.resolve({ items: [] });
    expect(await oldRequest).toBeInstanceOf(CancelledRequestError);
    expect(store.accounts).toBeNull();
  });
  it("null 列表报错并保留旧缓存，不冒充零个账户", async () => {
    vi.spyOn(api, "get").mockResolvedValue({ items: null });
    const store = useDataCacheStore();
    const previous: Account[] = [{ id: 1, name: "fixture", exchange: "okx", environment: "demo", proxyId: null, isEnabled: true, status: "idle" }];
    store.accounts = { data: previous, fetchedAt: Date.now() };
    await expect(store.fetchAccounts(true)).rejects.toMatchObject({ detail: "INVALID_RESPONSE" });
    expect(store.accounts?.data).toBe(previous);
  });
});

describe("会话请求竞态", () => {
  it("旧状态请求不能清掉新请求的 loading", async () => {
    const first = deferred<AuthStatus>();
    const second = deferred<AuthStatus>();
    vi.spyOn(api, "get").mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const store = useAuthStore();
    const old = store.checkStatus(); const current = store.checkStatus();
    first.resolve({ initialized: false, authenticated: false, user: null }); await old;
    expect(store.loading).toBe(true);
    second.resolve({ initialized: true, authenticated: false, user: null }); await current;
    expect(store.initialized).toBe(true);
    expect(store.loading).toBe(false);
  });
  it("缺失 initialized 的响应保持未知状态", async () => {
    vi.spyOn(api, "get").mockResolvedValue({});
    const store = useAuthStore();
    await store.checkStatus();
    expect(store.initialized).toBeNull();
    expect(store.error).toBeTruthy();
  });
  it("退出后迟到的登录成功不能恢复会话", async () => {
    const pending = deferred<{ user: User }>();
    vi.spyOn(api, "post").mockReturnValue(pending.promise);
    const store = useAuthStore();
    const login = store.login("demo", "example-password");
    store.clearSession(); pending.resolve({ user });
    expect(await login).toBe(false);
    expect(store.authenticated).toBe(false);
    expect(store.user).toBeNull();
  });
  it("登录错误中的顶层锁定和验证码字段接入流程", async () => {
    vi.spyOn(api, "post").mockRejectedValue(new ApiError(429, "ACCOUNT_LOCKED", {
      detail: "ACCOUNT_LOCKED", captchaRequired: true, totpRequired: true, lockRemainingSec: 60,
    }));
    vi.spyOn(api, "get").mockResolvedValue({ captchaToken: "fixture", svg: "<svg/>", expiresInSec: 300 });
    const store = useAuthStore();
    expect(await store.login("demo", "example-password")).toBe(false);
    expect(store.captchaRequired).toBe(true);
    expect(store.totpRequired).toBe(true);
    expect(store.lockRemainingSec).toBe(60);
    expect(store.captchaData?.captchaToken).toBe("fixture");
  });
  it("登出请求失败也清会话和缓存", async () => {
    vi.spyOn(api, "post").mockRejectedValue(new ApiError(0, "NETWORK_ERROR"));
    const store = useAuthStore();
    store.authenticated = true; store.user = user;
    await store.logout();
    expect(store.authenticated).toBe(false);
    expect(store.user).toBeNull();
    expect(useDataCacheStore().accounts).toBeNull();
  });
});

describe("路由三态", () => {
  const privateRoute = { path: "/accounts", fullPath: "/accounts?tab=positions", meta: { requiresAuth: true } };
  it.each([
    [null, false, "/connection-error"],
    [false, false, "/setup"],
    [true, false, "/login"],
  ] as const)("初始化=%s、登录=%s 时进入 %s", async (initialized, authenticated, path) => {
    const auth: GuardAuth = { initialized, authenticated, checkStatus: vi.fn().mockResolvedValue(undefined) };
    expect(await guardRoute(privateRoute, auth)).toMatchObject({ path });
  });
  it("已初始化并登录时进入目标页", async () => {
    expect(await guardRoute(privateRoute, { initialized: true, authenticated: true, checkStatus: vi.fn() })).toBe(true);
  });
  it("状态请求异常也进入连接失败页，不进入初始化", async () => {
    expect(await guardRoute(privateRoute, {
      initialized: null, authenticated: false, checkStatus: vi.fn().mockRejectedValue(new Error("offline")),
    })).toMatchObject({ path: "/connection-error", query: { redirect: privateRoute.fullPath } });
  });
  it("404 与连接重试页不陷入守卫循环", async () => {
    const auth: GuardAuth = { initialized: null, authenticated: false, checkStatus: vi.fn() };
    expect(await guardRoute({ path: "/missing", fullPath: "/missing", meta: { notFound: true } }, auth)).toBe(true);
    expect(await guardRoute({ path: "/connection-error", fullPath: "/connection-error", meta: {} }, auth)).toBe(true);
    expect(auth.checkStatus).not.toHaveBeenCalled();
  });
  it("redirect 只接受内部路径且不回到认证流程", () => {
    expect(safeRedirect("/accounts?tab=positions")).toBe("/accounts?tab=positions");
    for (const value of ["//other.example", "/%5cother", "/login#form", "/setup?step=1", "/%0aother", "/%2fother", "https://other.example", "/%broken"]) {
      expect(safeRedirect(value)).toBe("/dashboard");
    }
  });
  it("认证预览只在 mock 构建开放，生产查询参数不能绕过守卫", async () => {
    const route = { path: "/login", fullPath: "/login?preview=1", meta: {}, query: { preview: "1" } };
    const auth: GuardAuth = { initialized: true, authenticated: true, checkStatus: vi.fn() };
    expect(await guardRoute(route, auth, true)).toBe(true);
    expect(await guardRoute(route, auth, false)).toMatchObject({ path: "/dashboard" });
  });
});
