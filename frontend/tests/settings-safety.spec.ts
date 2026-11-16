import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter, RouterView, type Router } from "vue-router";
import SettingsView from "@/views/SettingsView.vue";
import { api, ApiError } from "@/api/client";
import { useDataCacheStore } from "@/stores/dataCache";
import { useUiStore } from "@/stores/ui";
import { useAuthStore } from "@/stores/auth";
import { DEFAULT_RESILIENCE, DEFAULT_RISK } from "@/constants/validation";
import { useI18n } from "@/i18n";
import type { SettingsResponse } from "@/types/api";

// 通过真实表单保存验证白名单与后端字段错误，不只测试独立工具函数。
function fixture(): SettingsResponse {
  return {
    global_kill_switch: true, public_monitoring_enabled: true,
    risk_defaults: { ...DEFAULT_RISK }, resilience: { ...DEFAULT_RESILIENCE },
    notifications: {
      webhook_enabled: false, webhook_url: "", telegram_enabled: false,
      telegram_bot_token: "", telegram_chat_id: "", wecom_enabled: false,
      wecom_webhook_url: "", dingtalk_enabled: false, dingtalk_webhook_url: "",
      outbox_enabled: false, outbox_path: "", notify_on_execution: true,
      notify_on_lead_order: true, notify_on_risk_block: true, notify_on_security_alert: true,
    },
    security: { totpEnabled: false },
  };
}
let wrapper: ReturnType<typeof mount> | undefined;
let testRouter: Router | undefined;
beforeEach(() => { setActivePinia(createPinia()); });
afterEach(() => { wrapper?.unmount(); wrapper = undefined; useUiStore().clear(); vi.restoreAllMocks(); });
async function view() {
  const pinia = createPinia(); setActivePinia(pinia);
  const cache = useDataCacheStore();
  cache.settings = { data: fixture(), fetchedAt: Date.now() };
  vi.spyOn(cache, "fetchSettings").mockResolvedValue(fixture());
  // 保留真实确认编号；接受流程不能用直接返回 true 绕过所有权保护。
  vi.spyOn(useUiStore(), "ask");
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/settings", component: SettingsView }, { path: "/away", component: { template: "<div />" } },
  ] });
  testRouter = router;
  await router.push("/settings"); await router.isReady();
  wrapper = mount(RouterView, { global: { plugins: [pinia, router] } });
  await flushPromises();
  return wrapper;
}

describe("设置资金安全", () => {
  it("保存通知页签绝不回写本地急停或只读安全信息", async () => {
    const put = vi.spyOn(api, "put").mockResolvedValue(fixture());
    const rendered = await view();
    await rendered.findAll('input[type="checkbox"]')[0].setValue(false);
    await rendered.find("form").trigger("submit");
    expect(put).not.toHaveBeenCalled();
    expect(useUiStore().confirmation).not.toBeNull();
    useUiStore().accept();
    await flushPromises();
    expect(put).toHaveBeenCalledOnce();
    const payload = put.mock.calls[0][1] as Partial<SettingsResponse>;
    expect(Object.keys(payload)).toEqual(["notifications"]);
    expect(payload).not.toHaveProperty("global_kill_switch");
    expect(payload).not.toHaveProperty("security");
  });

  it("422的detail数组落到对应风控字段", async () => {
    vi.spyOn(api, "put").mockRejectedValue(new ApiError(422, "HTTP 422", {
      detail: [{ loc: ["body", "risk_defaults", "maxDrawdownPct"], type: "less_than_equal", msg: "invalid", ctx: { le: 100 } }],
    }));
    const rendered = await view();
    await rendered.findAll('[role="tab"]')[1].trigger("click");
    await rendered.find('input[name="risk_defaults.max_drawdown_pct"]').setValue("12");
    await rendered.find("form").trigger("submit"); await flushPromises();
    expect(rendered.find('input[name="risk_defaults.max_drawdown_pct"]').attributes("aria-invalid")).toBe("true");
    expect(rendered.text()).toContain(useI18n().t("validation.max", { n: 100 }));
  });

  it("安全页签没有通用保存按钮", async () => {
    const rendered = await view();
    vi.spyOn(useDataCacheStore(), "fetchAuditLogs").mockResolvedValue([]);
    vi.spyOn(useDataCacheStore(), "fetchSecurityGuards").mockResolvedValue([]);
    await rendered.findAll('[role="tab"]')[3].trigger("click"); await flushPromises();
    expect(rendered.find("form").exists()).toBe(false);
    expect(rendered.find("footer").exists()).toBe(false);
  });

  it("未保存草稿切换页签需要确认，取消后仍保留原页和草稿", async () => {
    const rendered = await view();
    const ui = useUiStore(); vi.mocked(ui.ask).mockResolvedValue(false);
    await rendered.findAll('input[type="checkbox"]')[0].setValue(false);
    await rendered.findAll('[role="tab"]')[1].trigger("click"); await flushPromises();
    expect(rendered.findAll('[role="tab"]')[0].attributes("aria-selected")).toBe("true");
    expect((rendered.findAll('input[type="checkbox"]')[0].element as HTMLInputElement).checked).toBe(false);
    expect(ui.ask).toHaveBeenCalledWith(expect.objectContaining({ message: useI18n().t("set.discardConfirm") }));
  });

  it("韧性数字清空变成null并阻止提交空串", async () => {
    const put = vi.spyOn(api, "put");
    const rendered = await view();
    await rendered.findAll('[role="tab"]')[2].trigger("click");
    await rendered.find('input[name="resilience.max_consecutive_failures"]').setValue("");
    await rendered.find("form").trigger("submit"); await flushPromises();
    expect(put).not.toHaveBeenCalled();
    expect(rendered.find('input[name="resilience.max_consecutive_failures"]').attributes("aria-invalid")).toBe("true");
  });

  it("真实路由离开守卫保留未保存草稿，取消不会跳走", async () => {
    const rendered = await view();
    useAuthStore().authenticated = true;
    useAuthStore().user = { id: 1, username: "preview", role: "admin", totpEnabled: false };
    const ui = useUiStore(); vi.mocked(ui.ask).mockResolvedValue(false);
    await rendered.findAll('input[type="checkbox"]')[0].setValue(false);
    await testRouter!.push("/away"); await flushPromises();
    expect(testRouter!.currentRoute.value.path).toBe("/settings");
    expect(ui.ask).toHaveBeenCalledOnce();
    expect(rendered.text()).toContain(useI18n().t("set.unsaved"));
  });

  it("刷新页面也受未保存修改保护", async () => {
    const rendered = await view();
    useAuthStore().authenticated = true;
    useAuthStore().user = { id: 1, username: "preview", role: "admin", totpEnabled: false };
    await rendered.findAll('input[type="checkbox"]')[0].setValue(false);
    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
  });
});
