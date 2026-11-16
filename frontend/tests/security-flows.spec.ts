import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import SecuritySettingsPanel from "@/components/settings/SecuritySettingsPanel.vue";
import { api } from "@/api/client";
import { useAuthStore } from "@/stores/auth";
import { useDataCacheStore } from "@/stores/dataCache";
import { useUiStore } from "@/stores/ui";
import { useI18n } from "@/i18n";

// 安全流程分别保存草稿，后端未确认时不能登出或伪造验证步骤。
let wrapper: ReturnType<typeof mount> | undefined;
beforeEach(() => {
  setActivePinia(createPinia());
  const cache = useDataCacheStore();
  vi.spyOn(cache, "fetchAuditLogs").mockResolvedValue([]);
  vi.spyOn(cache, "fetchSecurityGuards").mockResolvedValue([]);
  const auth = useAuthStore();
  auth.authenticated = true; auth.user = { id: 1, username: "preview", role: "admin", totpEnabled: false };
});
afterEach(() => { wrapper?.unmount(); wrapper = undefined; useUiStore().clear(); vi.restoreAllMocks(); });
async function panel(enabled = false) {
  useAuthStore().user!.totpEnabled = enabled;
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/settings", component: { template: "<div />" } }, { path: "/login", component: { template: "<div />" } },
  ] });
  await router.push("/settings"); await router.isReady();
  wrapper = mount(SecuritySettingsPanel, { props: { security: { totpEnabled: enabled } }, global: { plugins: [router] } });
  await flushPromises(); return wrapper;
}
async function beginEnable(rendered: ReturnType<typeof mount>) {
  const button = rendered.findAll("button").find(item => item.text() === useI18n().t("security.enableTitle"))!;
  await button.trigger("click"); await flushPromises();
  await rendered.find('form input[autocomplete="current-password"]').setValue("sample-password");
}

describe("安全独立流程", () => {
  it("不完整TOTP准备响应仍留在密码步骤", async () => {
    vi.spyOn(api, "post").mockResolvedValue({ secret: null, qrSvgDataUri: null });
    const rendered = await panel(); await beginEnable(rendered);
    await rendered.find("form").trigger("submit"); await flushPromises();
    expect(rendered.find("img").exists()).toBe(false);
    expect((rendered.find('input[autocomplete="current-password"]').element as HTMLInputElement).value).toBe("sample-password");
    expect(rendered.text()).toContain(useI18n().t("errors.invalidResponse"));
    expect(useAuthStore().authenticated).toBe(true);
  });

  it("启用回执未确认时保留验证码步骤且不登出", async () => {
    const post = vi.spyOn(api, "post").mockImplementation(async endpoint => endpoint.endsWith("/setup")
      ? { secret: "SAMPLEONLY", qrSvgDataUri: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'/%3E" }
      : { ok: false });
    const rendered = await panel(); await beginEnable(rendered);
    await rendered.find("form").trigger("submit"); await flushPromises();
    await rendered.find('input[autocomplete="one-time-code"]').setValue("123456");
    await rendered.find("form").trigger("submit"); await flushPromises();
    expect(post).toHaveBeenLastCalledWith("/api/auth/2fa/enable", { code: "123456" });
    expect(rendered.find('input[autocomplete="one-time-code"]').exists()).toBe(true);
    expect(rendered.text()).toContain(useI18n().t("errors.invalidResponse"));
    expect(useAuthStore().authenticated).toBe(true);
  });

  it("停用两步验证使用自己的密码和验证码并等待强确认", async () => {
    const post = vi.spyOn(api, "post").mockResolvedValue({ ok: true });
    const rendered = await panel(true);
    const button = rendered.findAll("button").find(item => item.text() === useI18n().t("security.disableTitle"))!;
    await button.trigger("click"); await flushPromises();
    await rendered.find('input[autocomplete="current-password"]').setValue("sample-disable-password");
    await rendered.find('input[autocomplete="one-time-code"]').setValue("654321");
    await rendered.find("form").trigger("submit"); await flushPromises();
    expect(post).not.toHaveBeenCalled();
    expect(useUiStore().confirmation?.strong).toBe(true);
    useUiStore().accept(); await flushPromises();
    expect(post).toHaveBeenCalledWith("/api/auth/2fa/disable", { currentPassword: "sample-disable-password", code: "654321" });
    expect(useAuthStore().authenticated).toBe(false);
  });

  it("从两步验证切到改密码时取消确认会保留原输入", async () => {
    const rendered = await panel(); await beginEnable(rendered);
    const button = rendered.findAll("button").find(item => item.text() === useI18n().t("security.changePassword"))!;
    await button.trigger("click");
    expect(useUiStore().confirmation).not.toBeNull();
    useUiStore().close(); await flushPromises();
    expect((rendered.find('input[autocomplete="current-password"]').element as HTMLInputElement).value).toBe("sample-password");
    expect(rendered.findAll("form")).toHaveLength(1);
  });
});
