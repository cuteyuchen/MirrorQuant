import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import { api } from "@/api/client";
import { useAuthStore } from "@/stores/auth";
import { useI18n } from "@/i18n";
import AuthLayout from "@/components/AuthLayout.vue";
import LoginView from "@/views/LoginView.vue";
import SetupWizard from "@/views/SetupWizard.vue";
import ConnectionErrorView from "@/views/ConnectionErrorView.vue";
import NotFoundView from "@/views/NotFoundView.vue";

vi.mock("@/api/mode", () => ({ MOCK_MODE: true }));
const { t } = useI18n();
let pinia: ReturnType<typeof createPinia>;
const wrappers: ReturnType<typeof mount>[] = [];
beforeEach(() => {
  pinia = createPinia(); setActivePinia(pinia);
  // 保留真实微任务刷新，只控制共享时钟用于锁定倒计时。
  vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
  vi.setSystemTime(new Date("2026-11-15T08:00:00Z"));
});
afterEach(() => {
  for (const wrapper of wrappers.splice(0)) wrapper.unmount();
  vi.clearAllTimers(); vi.useRealTimers(); vi.restoreAllMocks();
});
async function render(component: typeof LoginView, path: string) {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/login", component: LoginView }, { path: "/setup", component: SetupWizard },
    { path: "/connection-error", component: ConnectionErrorView }, { path: "/missing", component: NotFoundView },
    { path: "/dashboard", component: { template: "<div />" } }, { path: "/public", component: { template: "<div />" } },
    { path: "/register", component: { template: "<div />" } },
  ] });
  await router.push(path);
  const wrapper = mount(component, { global: { plugins: [pinia, router] } });
  wrappers.push(wrapper);
  await vi.dynamicImportSettled();
  await flushPromises();
  return wrapper;
}

describe("认证示例预览", () => {
  it("登录示例显示验证码与锁定，但不修改全局会话或写入", async () => {
    const auth = useAuthStore();
    auth.authenticated = true; auth.initialized = true;
    const request = vi.spyOn(api, "request");
    const wrapper = await render(LoginView, "/login?preview=1");
    expect(wrapper.findComponent(AuthLayout).exists()).toBe(true);
    expect(wrapper.find("img").attributes("src")).toMatch(/^data:/);
    expect(wrapper.text()).toContain("0:45");
    expect(auth.lockRemainingSec).toBe(0);
    expect(auth.captchaRequired).toBe(false);
    await vi.advanceTimersByTimeAsync(45000);
    await wrapper.find("#login-password").setValue("sample-password");
    await wrapper.find("#login-captcha").setValue("8");
    await wrapper.find("#login-totp").setValue("123456");
    await wrapper.find("form").trigger("submit");
    expect(wrapper.find('[role="alert"]').text()).toBe(t("errors.mockReadonly"));
    expect(auth.authenticated).toBe(true);
    expect(request).not.toHaveBeenCalled();
  });
  it("初始化示例不发送授权令牌或创建管理员", async () => {
    const request = vi.spyOn(api, "request");
    const wrapper = await render(SetupWizard, "/setup?preview=1");
    await wrapper.find("#setup-password").setValue("sample-password");
    await wrapper.find("#setup-confirm-password").setValue("sample-password");
    await wrapper.find("#setup-token").setValue("sample-bootstrap");
    await wrapper.find("form").trigger("submit");
    expect(wrapper.find('[role="alert"]').text()).toBe(t("errors.mockReadonly"));
    expect(request).not.toHaveBeenCalled();
  });
  it("连接失败与404沿用同一认证布局", async () => {
    const connection = await render(ConnectionErrorView, "/connection-error?preview=1");
    expect(connection.findComponent(AuthLayout).exists()).toBe(true);
    expect(connection.find('[role="alert"]').text()).toBe(t("errors.network"));
    const missing = await render(NotFoundView, "/missing");
    expect(missing.findComponent(AuthLayout).exists()).toBe(true);
    expect(missing.text()).toContain("404");
  });
});
