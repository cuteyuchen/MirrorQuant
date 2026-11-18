import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import { api } from "@/api/client";
import { useI18n } from "@/i18n";
import { mockExecutions, mockIntents, mockResponse } from "@/mocks/board";
import { reasonText } from "@/utils/errors";
import { statusLabel, statusTone } from "@/utils/status";
import { executionGroup, executionOrder, readExecutionCounts, readExecutionPage } from "@/utils/executions";
import type { ExecutionRecord, ExecutionsResponse } from "@/types/api";
import ExecutionsView from "@/views/ExecutionsView.vue";

vi.mock("@/api/mode", () => ({ MOCK_MODE: false }));
const { t } = useI18n();
const wrappers: ReturnType<typeof mount>[] = [];

// 严格模拟真实接口的单状态过滤，不借用样例服务额外支持的逗号语法。
function strictResponse(endpoint: string, records = mockExecutions): ExecutionsResponse {
  const query = new URL(endpoint, "http://local.test").searchParams;
  const filtered = records.filter(record => (!query.get("status") || record.status === query.get("status"))
    && (!query.get("binding_id") || String(record.bindingId) === query.get("binding_id"))).sort(executionOrder);
  const offset = Number(query.get("offset") ?? 0);
  return { total: filtered.length, items: filtered.slice(offset, offset + Number(query.get("limit") ?? 20)) };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(success => { resolve = success; });
  return { promise, resolve };
}
let slowPage: ReturnType<typeof deferred<ExecutionsResponse>> | null;
let pageDelayed = false;
beforeEach(() => {
  setActivePinia(createPinia());
  slowPage = null; pageDelayed = false;
  vi.spyOn(api, "get").mockImplementation(async endpoint => {
    if (endpoint.startsWith("/api/dashboard/executions")) {
      const query = new URL(endpoint, "http://local.test").searchParams;
      if (slowPage && !pageDelayed && !query.get("status") && query.get("limit") === "20") {
        pageDelayed = true; return slowPage.promise;
      }
      return strictResponse(endpoint);
    }
    return mockResponse(endpoint);
  });
});
afterEach(() => {
  for (const wrapper of wrappers.splice(0)) wrapper.unmount();
  vi.restoreAllMocks();
});
async function render(path = "/executions") {
  const pinia = createPinia(); setActivePinia(pinia);
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/executions", component: ExecutionsView },
    { path: "/dashboard", component: { template: "<div />" } },
  ] });
  await router.push(path);
  const wrapper = mount(ExecutionsView, { attachTo: document.body, global: { plugins: [pinia, router], stubs: { Teleport: true } } });
  wrappers.push(wrapper);
  await flushPromises();
  return { wrapper, router };
}

describe("执行筛选与全量计数", () => {
  it.each([
    ["EXECUTED", "success"], ["rejected", "failed"], ["UNFILLED", "failed"], ["SKIPPED_STALE", "skipped"],
    ["BLOCKED_SOURCE_ORDERING", "blocked"], ["PARTIAL", "other"], [null, "other"],
  ] as const)("状态 %s 归入 %s", (status, group) => {
    expect(executionGroup(status)).toBe(group);
  });
  it("状态计数取后端全量 total，而不是当前页数量", async () => {
    const request = vi.fn(async (endpoint: string) => strictResponse(endpoint));
    const counts = await readExecutionCounts(null, request);
    expect(counts).toEqual({ all: 47, success: 12, failed: 12, skipped: 11, blocked: 6 });
    for (const [endpoint] of request.mock.calls) {
      const query = new URL(endpoint, "http://local.test").searchParams;
      expect(query.get("limit")).toBe("1");
      expect(query.get("status") ?? "").not.toContain(",");
    }
  });
  it("多枚举失败结果统一倒序后再分页", async () => {
    const records = Array.from({ length: 70 }, (_, index): ExecutionRecord => ({
      ...mockExecutions[0]!, id: index + 1, createdAt: 1800000000000 - index * 1000,
      status: index < 63 ? ["FAILED", "REJECTED", "UNFILLED"][index % 3]! : "EXECUTED",
    }));
    const request = vi.fn(async (endpoint: string) => strictResponse(endpoint, records));
    const result = await readExecutionPage({ filter: "failed", page: 1, pageSize: 20 }, request);
    expect(result.total).toBe(63);
    expect(result.items.map(record => record.id)).toEqual(records.slice(20, 40).map(record => record.id));
    expect(request.mock.calls.every(([endpoint]) => !new URL(endpoint, "http://local.test").searchParams.get("status")?.includes(","))).toBe(true);
  });
  it("深页仍遵循每次500条接口上限", async () => {
    const records = Array.from({ length: 1050 }, (_, index): ExecutionRecord => ({
      ...mockExecutions[0]!, id: index + 1, createdAt: 1800000000000 - index * 1000, status: "FAILED",
    }));
    const request = vi.fn(async (endpoint: string) => strictResponse(endpoint, records));
    const result = await readExecutionPage({ filter: "failed", page: 26, pageSize: 20 }, request);
    expect(result.items.map(record => record.id)).toEqual(records.slice(520, 540).map(record => record.id));
    const queries = request.mock.calls.map(([endpoint]) => new URL(endpoint, "http://local.test").searchParams);
    expect(queries.every(query => Number(query.get("limit")) <= 500)).toBe(true);
    expect(queries.some(query => query.get("offset") === "500")).toBe(true);
  });
  it("null 列表报错，null 总数保持未知而非零", async () => {
    await expect(readExecutionPage({ filter: "all", page: 0, pageSize: 20 }, async () => ({ items: null, total: null }))).rejects.toMatchObject({ detail: "INVALID_RESPONSE" });
    const counts = await readExecutionCounts(null, async () => ({ items: [], total: null }));
    expect(Object.values(counts).every(count => count === null)).toBe(true);
  });
});

describe("执行记录页面", () => {
  it("默认20条、分段计数全量，失败原因直接出现在行内", async () => {
    const { wrapper } = await render();
    expect(wrapper.findAll("tbody > tr")).toHaveLength(20);
    expect(wrapper.find('[data-testid="filter-failed"]').text()).toContain("12");
    expect(wrapper.findAll("tbody > tr").some(row => row.text().includes(reasonText("INSUFFICIENT_MARGIN")))).toBe(true);
    await wrapper.find('[data-testid="filter-failed"]').trigger("click");
    await flushPromises();
    expect(wrapper.findAll("tbody > tr")).toHaveLength(12);
    expect(wrapper.findAll("tbody > tr").every(row => row.classes().includes("bg-danger/5"))).toBe(true);
  });
  it("切换绑定会刷新该绑定的全量计数和记录", async () => {
    const { wrapper } = await render();
    await wrapper.find("#execution-binding").setValue("12");
    await flushPromises();
    const expected = mockExecutions.filter(record => record.bindingId === 12);
    expect(wrapper.find('[data-testid="filter-all"]').text()).toContain(String(expected.length));
    expect(wrapper.findAll("tbody > tr").every(row => row.text().includes("Main Futures"))).toBe(true);
  });
  it("真实分页和订单详情抽屉均可操作", async () => {
    const { wrapper } = await render();
    await wrapper.find(`button[aria-label="${t("common.next")}"]`).trigger("click");
    await flushPromises();
    expect(wrapper.findAll("tbody > tr")).toHaveLength(20);
    expect(wrapper.text()).toContain(t("common.page", { page: 2, total: 47 }));
    await wrapper.find(`button[aria-label="${t("common.previous")}"]`).trigger("click");
    await flushPromises();
    await wrapper.find("tbody > tr").trigger("click");
    await flushPromises();
    const dialog = wrapper.find('[role="dialog"]');
    expect(dialog.exists()).toBe(true);
    expect(dialog.text()).toContain("sample-order-0");
    expect(dialog.text()).toContain("+0.0066%");
  });
  it("待处理订单深链接显示样例未知和提交中状态，不误判 net 为空头", async () => {
    const { wrapper } = await render("/executions?tab=intents");
    const rows = wrapper.findAll("tbody > tr");
    expect(rows).toHaveLength(mockIntents.length);
    const unknown = rows.find(row => row.text().includes("ETHUSDT"))!;
    expect(unknown.text()).toContain(t("side.UNKNOWN"));
    expect(unknown.text()).not.toContain(t("side.SHORT"));
    expect(wrapper.text()).toContain("sample-intent-1");
  });
  it("旧的全集响应不能覆盖更晚的失败筛选", async () => {
    slowPage = deferred<ExecutionsResponse>();
    const { wrapper } = await render();
    await wrapper.find('[data-testid="filter-failed"]').trigger("click");
    await flushPromises();
    expect(wrapper.findAll("tbody > tr")).toHaveLength(12);
    slowPage.resolve(strictResponse("/api/dashboard/executions?limit=20&offset=0"));
    await flushPromises();
    expect(wrapper.findAll("tbody > tr")).toHaveLength(12);
  });
  it("切到待处理页签会结束旧加载态，不永久禁用刷新", async () => {
    slowPage = deferred<ExecutionsResponse>();
    const { wrapper, router } = await render();
    await router.replace("/executions?tab=intents");
    await flushPromises();
    expect(wrapper.find(`button[aria-label="${t("common.refresh")}"]`).attributes("disabled")).toBeUndefined();
    slowPage.resolve(strictResponse("/api/dashboard/executions?limit=20&offset=0"));
    await flushPromises();
    expect(wrapper.findAll("tbody > tr")).toHaveLength(mockIntents.length);
  });
  it("执行记录表显示价差列", async () => {
    const { wrapper } = await render();
    expect(wrapper.text()).toContain(t("executions.spread"));
    expect(wrapper.text()).toContain("0.0012%");
  });
  it("运行中的带单员状态显示为运行中而非未知", () => {
    expect(statusLabel("running")).toBe("运行中");
    expect(statusTone("running")).toBe("success");
  });
});
