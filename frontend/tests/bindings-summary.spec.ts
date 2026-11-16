import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { api, ApiError, CancelledRequestError } from "@/api/client";
import { bindingPositionCount, bindingPresentation, bindingSummary, fetchTodayExecutionCounts } from "@/constants/bindingStatus";
import { mockAccounts, mockBindings, mockBoard, mockExecutions, mockIntents, mockLeads, mockSettings } from "@/mocks/board";
import { useDataCacheStore } from "@/stores/dataCache";
import { useI18n } from "@/i18n";
import type { Binding, BoardLead, DashboardBoard, ExecutionRecord } from "@/types/api";
import BindingsView from "@/views/BindingsView.vue";
import RiskPolicyFields from "@/components/RiskPolicyFields.vue";
import { DEFAULT_RISK, FLAT_REQUIRED_RISK_FIELDS } from "@/constants/validation";

vi.mock("@/api/client", async importOriginal => {
  const original = await importOriginal<typeof import("@/api/client")>();
  return { ...original, api: { get: vi.fn(async () => ({ items: [], total: 0 })), put: vi.fn(), post: vi.fn(), delete: vi.fn() } };
});

// 使用已知空仓快照作为基线，各用例仅改变正在验证的异常。
function binding(overrides: Partial<Binding> = {}): Binding {
  return { ...mockBindings[0], id: 1, leadId: 1, isEnabled: true, circuitBreakerTripped: false,
    circuitBreakerReason: null, baselineSeeded: true, pendingDebtUsd: 0, ...overrides };
}
function board(overrides: Partial<BoardLead> = {}): DashboardBoard {
  return { ...mockBoard, globalKillSwitch: false, leads: [{ ...mockBoard.leads![0], id: 1, enabled: true,
    stale: false, positionShow: true, positions: [], orphans: [], todayEvents: [], ...overrides }] };
}
function seed(bindings: Binding[], currentBoard: DashboardBoard, intents = mockIntents) {
  const cache = useDataCacheStore();
  const fetchedAt = Date.now();
  cache.bindings = { data: { items: bindings, globalKillSwitch: currentBoard.globalKillSwitch }, fetchedAt };
  cache.accounts = { data: mockAccounts, fetchedAt };
  cache.leads = { data: mockLeads, fetchedAt };
  cache.settings = { data: mockSettings, fetchedAt };
  cache.board = { data: currentBoard, fetchedAt };
  cache.intents = { data: intents, fetchedAt };
  vi.spyOn(cache, "fetchBindings").mockResolvedValue(cache.bindings.data);
  vi.spyOn(cache, "fetchAccounts").mockResolvedValue(mockAccounts);
  vi.spyOn(cache, "fetchLeads").mockResolvedValue(mockLeads);
  vi.spyOn(cache, "fetchSettings").mockResolvedValue(mockSettings);
  vi.spyOn(cache, "fetchBoard").mockResolvedValue(currentBoard);
  vi.spyOn(cache, "fetchIntents").mockResolvedValue(intents);
}
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.get).mockResolvedValue({ items: [], total: 0 });
  setActivePinia(createPinia());
});

describe("绑定状态与页面摘要", () => {
  it("独立统计熔断、停用、待处理与未知，不把未知当正常", () => {
    const items = [binding(), binding({ id: 2, circuitBreakerTripped: true }), binding({ id: 3, isEnabled: false }),
      binding({ id: 4 }), binding({ id: 5, isEnabled: null })];
    const intents = [{ ...mockIntents[0], bindingId: 4 }, { ...mockIntents[0], bindingId: 999 }];
    expect(bindingSummary(items, board(), intents, false)).toEqual({ total: 5, tripped: 1, disabled: 1, pending: 1, unknown: 1 });
    expect(bindingPresentation(items[0], board(), [], false).state).toBe("HEALTHY");
    expect(bindingPresentation(items[4], board(), [], false).state).toBe("UNKNOWN");
  });
  it("急停中也保留熔断和遗留仓位的更具体原因", () => {
    expect(bindingPresentation(binding(), board(), [], true).state).toBe("KILL_SWITCH");
    expect(bindingPresentation(binding({ circuitBreakerTripped: true }), board(), [], true).state).toBe("CIRCUIT_BREAKER");
    const orphan = { ...mockBoard.leads![0].orphans![0], bindingId: 1, reason: "ORPHAN_POSITION" };
    expect(bindingPresentation(binding(), board({ orphans: [orphan] }), [], true)).toMatchObject({ state: "BLOCKED", tone: "danger", reasons: ["ORPHAN_POSITION"] });
  });
  it("缺失持仓、隐藏持仓、缺失订单列表均不能当成已空仓并正常运行", () => {
    expect(bindingPositionCount(binding(), board({ positions: null }))).toBeNull();
    expect(bindingPositionCount(binding(), board({ positionShow: false }))).toBeNull();
    expect(bindingPresentation(binding(), board(), null, false).state).toBe("UNKNOWN");
    expect(bindingPresentation(binding(), board({ positions: null }), [], false).state).toBe("UNKNOWN");
    expect(bindingPresentation(binding(), board(), [], null).state).toBe("UNKNOWN");
    expect(bindingPresentation(binding({ baselineSeeded: null }), board(), [], false).state).toBe("UNKNOWN");
  });
  it("真实持仓包含遗留仓；未知数量不计成零", () => {
    const position = { ...mockBoard.leads![0].positions![0], mine: [{ ...mockBoard.leads![0].positions![0].mine![0], bindingId: 1, qty: 0.2 }] };
    const orphan = { ...mockBoard.leads![0].orphans![0], bindingId: 1, qty: 12 };
    expect(bindingPositionCount(binding(), board({ positions: [position], orphans: [orphan] }))).toBe(2);
    expect(bindingPositionCount(binding(), board({ positions: [{ ...position, mine: [{ ...position.mine[0], qty: null }] }] }))).toBeNull();
    expect(bindingPositionCount(binding({ isEnabled: false }), board({ positions: [{ ...position, mine: [] }] }))).toBeNull();
  });
  it.each([null, Number.NaN])("已对齐但数量为 %s 时仍待确认，摘要不能标成正常", qty => {
    const position = { ...mockBoard.leads![0].positions![0],
      mine: [{ ...mockBoard.leads![0].positions![0].mine![0], bindingId: 1, state: "aligned" as const, qty }] };
    const current = board({ positions: [position] });
    expect(bindingPresentation(binding(), current, [], false)).toMatchObject({ state: "UNKNOWN", tone: "warning", positionCount: null });
    expect(bindingSummary([binding()], current, [], false).unknown).toBe(1);
  });
  it("渲染一句话摘要和全局暂停横幅，异常行高亮", async () => {
    const current = { ...board(), globalKillSwitch: true };
    const items = [binding(), binding({ id: 2, circuitBreakerTripped: true }), binding({ id: 3, isEnabled: false })];
    seed(items, current, []);
    const page = mount(BindingsView, { global: { stubs: { BaseModal: true, RouterLink: true } } });
    await flushPromises();
    const { t } = useI18n();
    expect(page.get('[data-testid="page-summary"]').text()).toBe(t("bind.summary", { n: 3, tripped: 1, disabled: 1, pending: 0, unknown: 0 }));
    expect(page.get('[data-testid="binding-kill-banner"]').text()).toContain(t("bind.killBanner"));
    expect(page.get("tbody tr").classes()).toContain("bg-danger/10");
    expect(page.findAll("colgroup col")[1].attributes("style")).toContain("68px");
    expect(page.findAll("colgroup col").at(-1)?.attributes("style")).toContain("68px");
    page.unmount();
  });
  it("仅有敞口限制的保护项锁定，倍率和普通风控仍可编辑", () => {
    const fields = mount(RiskPolicyFields, { props: { modelValue: DEFAULT_RISK, defaults: DEFAULT_RISK, disabledFields: FLAT_REQUIRED_RISK_FIELDS } });
    expect(fields.get('select[name="tp_sl_mode"]').attributes("disabled")).toBeDefined();
    expect(fields.get('input[name="stop_loss_pct"]').attributes("disabled")).toBeDefined();
    expect(fields.get('input[name="max_drawdown_pct"]').attributes("disabled")).toBeUndefined();
    expect(fields.get('select[name="order_mode"]').attributes("disabled")).toBeUndefined();
    fields.unmount();
  });
});

describe("真实今日执行统计", () => {
  const cutoff = 1_800_000_100_000;
  const dayStart = cutoff - 3_600_000;
  function execution(id: number, status = "EXECUTED", time = cutoff - 1_000): ExecutionRecord {
    return { ...mockExecutions[0], id, bindingId: 1, status, createdAt: time };
  }
  it("翻过500条继续读取，并去重重复记录、排除前一天与快照后记录", async () => {
    vi.mocked(api.get).mockResolvedValueOnce({ items: Array.from({ length: 500 }, (_, index) => execution(index + 1)), total: 900 });
    vi.mocked(api.get).mockResolvedValueOnce({ items: [execution(500), execution(501, "UNFILLED"),
      execution(502, "FAILED", dayStart - 1), execution(503, "FAILED", cutoff + 1)], total: 900 });
    expect(await fetchTodayExecutionCounts(dayStart, cutoff)).toEqual({ 1: { success: 500, failed: 1 } });
    expect(api.get).toHaveBeenNthCalledWith(2, "/api/dashboard/executions?limit=500&offset=500");
  });
  it("响应缺失时间时统计失败关闭，而不是展示成功0/失败0", async () => {
    vi.mocked(api.get).mockResolvedValueOnce({ items: [{ ...execution(1), createdAt: null }], total: 1 });
    await expect(fetchTodayExecutionCounts(dayStart, cutoff)).rejects.toBeInstanceOf(ApiError);
  });
  it("离开页面后取消在途统计，不再接受旧结果", async () => {
    let active = true;
    vi.mocked(api.get).mockImplementationOnce(async () => { active = false; return { items: [], total: 0 }; });
    await expect(fetchTodayExecutionCounts(dayStart, cutoff, () => active)).rejects.toBeInstanceOf(CancelledRequestError);
  });
});
