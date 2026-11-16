import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import BindingsView from "@/views/BindingsView.vue";
import NumberField from "@/components/NumberField.vue";
import { api } from "@/api/client";
import { useDataCacheStore } from "@/stores/dataCache";
import { useUiStore } from "@/stores/ui";
import { DEFAULT_RISK, RISK_RULES, RISK_SCALE_RULE, riskReductions, validateFields, validateNumber } from "@/constants/validation";
import { mockAccounts, mockBindings, mockBoard, mockIntents, mockLeads, mockSettings } from "@/mocks/board";
import { useI18n } from "@/i18n";
import { formatUsd } from "@/utils/format";

vi.mock("@/api/client", async importOriginal => {
  const original = await importOriginal<typeof import("@/api/client")>();
  return { ...original, api: { get: vi.fn(async () => ({ items: [], total: 0 })), put: vi.fn(async () => ({})), post: vi.fn(async () => ({})), delete: vi.fn(async () => ({})) } };
});

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.get).mockResolvedValue({ items: [], total: 0 });
  setActivePinia(createPinia());
});

function seed() {
  const cache = useDataCacheStore();
  const fetchedAt = Date.now();
  cache.bindings = { data: { items: [{ ...mockBindings[0], isEnabled: true, circuitBreakerTripped: true, pendingDebtUsd: 12.4 }], globalKillSwitch: false }, fetchedAt };
  cache.accounts = { data: mockAccounts, fetchedAt };
  cache.leads = { data: mockLeads, fetchedAt };
  cache.settings = { data: mockSettings, fetchedAt };
  cache.board = { data: mockBoard, fetchedAt };
  cache.intents = { data: mockIntents, fetchedAt };
  vi.spyOn(cache, "fetchBindings").mockResolvedValue(cache.bindings.data);
  vi.spyOn(cache, "fetchAccounts").mockResolvedValue(mockAccounts);
  vi.spyOn(cache, "fetchLeads").mockResolvedValue(mockLeads);
  vi.spyOn(cache, "fetchSettings").mockResolvedValue(mockSettings);
  vi.spyOn(cache, "fetchBoard").mockResolvedValue(mockBoard);
  vi.spyOn(cache, "fetchIntents").mockResolvedValue(mockIntents);
  return cache;
}

describe("绑定安全边界", () => {
  it("清空数字输入只产生 null，且保存校验拒绝空值", async () => {
    const field = mount(NumberField, { props: { modelValue: 1, rule: RISK_SCALE_RULE } });
    await field.find("input").setValue("");
    expect(field.emitted("update:modelValue")?.[0]).toEqual([null]);
    expect(validateNumber(null, RISK_SCALE_RULE)).not.toBe("");
    expect(validateNumber("", RISK_SCALE_RULE)).not.toBe("");
    expect(validateNumber(0, RISK_SCALE_RULE)).not.toBe("");
    expect(validateNumber(20, RISK_SCALE_RULE)).toBe("");
    expect(validateNumber(20.1, RISK_SCALE_RULE)).not.toBe("");
    field.unmount();
  });

  it("绑定和设置使用相同的服务端风控范围", () => {
    expect(validateFields(DEFAULT_RISK, RISK_RULES)).toEqual({});
    expect(validateNumber(0, RISK_RULES.max_slippage_pct)).not.toBe("");
    expect(validateNumber(50, RISK_RULES.max_slippage_pct)).toBe("");
    expect(validateNumber(125, RISK_RULES.max_instrument_leverage)).toBe("");
    expect(validateNumber(125.5, RISK_RULES.max_instrument_leverage)).not.toBe("");
    expect(validateNumber(0, RISK_RULES.max_drawdown_pct)).toBe("");
  });

  it("新建时关闭默认保护和切换止盈止损模式不会漏过确认", () => {
    const { t } = useI18n();
    expect(riskReductions(null, { ...DEFAULT_RISK, enable_circuit_breaker: false })).toContain(t("risk.circuitBreaker"));
    expect(riskReductions(DEFAULT_RISK, { ...DEFAULT_RISK, tp_sl_mode: "independent" })).toContain(t("set.tpslMode"));
    expect(riskReductions(DEFAULT_RISK, { ...DEFAULT_RISK, max_drawdown_pct: 0 })).toContain(t("set.mddBreaker"));
  });

  it("启停绑定在确认前不调用接口，取消后也不调用", async () => {
    seed();
    const page = mount(BindingsView, { global: { stubs: { BaseModal: true, RouterLink: true } } });
    await flushPromises();
    const ui = useUiStore();
    await page.find('button[role="switch"]').trigger("click");
    expect(ui.confirmation).not.toBeNull();
    expect(api.put).not.toHaveBeenCalled();
    ui.close();
    await flushPromises();
    expect(api.put).not.toHaveBeenCalled();
    await page.find('button[role="switch"]').trigger("click");
    ui.accept();
    await flushPromises();
    expect(api.put).toHaveBeenCalledWith(`/api/bindings/${mockBindings[0].id}`, { isEnabled: false });
    page.unmount();
    ui.clear();
  });
  it("删除、重置熔断与重置欠单都必须先确认，取消不执行", async () => {
    seed();
    const { t } = useI18n();
    const page = mount(BindingsView, { global: { stubs: { BaseModal: true, RouterLink: true } } });
    await flushPromises();
    const ui = useUiStore();
    for (const label of [t("common.delete"), t("bind.resetBreaker"), t("bind.clearDebt", { amt: formatUsd(12.4) })]) {
      const button = page.findAll("button").find(button => button.text() === label)!;
      await button.trigger("click");
      expect(ui.confirmation).not.toBeNull();
      expect(api.post).not.toHaveBeenCalled();
      expect(api.delete).not.toHaveBeenCalled();
      ui.close();
      await flushPromises();
    }
    expect(api.post).not.toHaveBeenCalled();
    expect(api.delete).not.toHaveBeenCalled();
    page.unmount();
    ui.clear();
  });
  it("新建 LIVE 绑定默认停用，并在提交前展示实盘确认", async () => {
    seed();
    const { t } = useI18n();
    const live = mockAccounts.find(account => account.environment === "live")!;
    const page = mount(BindingsView, { global: { stubs: { RouterLink: true,
      BaseModal: { props: ["open"], template: '<section v-if="open"><slot /></section>' } } } });
    await flushPromises();
    await page.findAll("button").find(button => button.text() === t("bind.newBtn"))!.trigger("click");
    await page.get('input[name="name"]').setValue("New Live Binding");
    const selects = page.findAll("select");
    await selects[0].setValue(String(mockLeads[0].id));
    await selects[1].setValue(String(live.id));
    await page.get("form").trigger("submit");
    const ui = useUiStore();
    expect(ui.confirmation).toMatchObject({ environment: "live" });
    expect(api.post).not.toHaveBeenCalled();
    ui.accept();
    await flushPromises();
    expect(api.post).toHaveBeenCalledWith("/api/bindings", expect.objectContaining({ accountId: live.id, isEnabled: false }));
    page.unmount();
    ui.clear();
  });
});
