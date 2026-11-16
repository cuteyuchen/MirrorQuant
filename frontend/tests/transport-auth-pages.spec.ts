import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import { api } from "@/api/client";
import { useAuthStore } from "@/stores/auth";
import LoginView from "@/views/LoginView.vue";
import SetupWizard from "@/views/SetupWizard.vue";

let pinia: ReturnType<typeof createPinia>;
const wrappers: ReturnType<typeof mount>[] = [];
beforeEach(() => {
  pinia = createPinia(); setActivePinia(pinia);
  vi.useFakeTimers(); vi.setSystemTime(new Date("2026-11-15T08:00:00Z"));
});
afterEach(() => {
  // 卸载组件后释放共享时钟，不能让下一项测试继承消费者计数。
  for (const wrapper of wrappers.splice(0)) wrapper.unmount();
  vi.clearAllTimers(); vi.useRealTimers(); vi.restoreAllMocks();
});
async function renderLogin() {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: "/login", component: LoginView }, { path: "/dashboard", component: { template: "<div />" } },
    { path: "/public", component: { template: "<div />" } },
  ] });
  await router.push("/login");
  const wrapper = mount(LoginView, { global: { plugins: [pinia, router] } });
  wrappers.push(wrapper);
  return wrapper;
}

describe("认证页面安全", () => {
  it("验证码通过 img 解码而不是 v-html 插入", async () => {
    const auth = useAuthStore();
    auth.captchaRequired = true;
    auth.captchaData = { captchaToken: "fixture", svg: "<svg xmlns='http://www.w3.org/2000/svg'><text>1234</text></svg>", expiresInSec: 300 };
    const wrapper = await renderLogin();
    const image = wrapper.find("img");
    expect(image.exists()).toBe(true);
    expect(image.attributes("src")).toMatch(/^data:/);
    expect(wrapper.findAll("svg text")).toHaveLength(0);
    expect(wrapper.find('input[aria-label]').exists()).toBe(true);
  });
  it("锁定剩余时间每秒推进，结束后允许重试", async () => {
    const auth = useAuthStore(); auth.lockRemainingSec = 2;
    const wrapper = await renderLogin();
    expect(wrapper.find('button[type="submit"]').attributes("disabled")).toBeDefined();
    expect(wrapper.text()).toContain("0:02");
    await vi.advanceTimersByTimeAsync(1000);
    expect(wrapper.text()).toContain("0:01");
    await vi.advanceTimersByTimeAsync(1000);
    expect(auth.lockRemainingSec).toBe(0);
    expect(wrapper.find('button[type="submit"]').attributes("disabled")).toBeUndefined();
  });
  it("需要 TOTP 时使用一次性验证码自动填写语义", async () => {
    useAuthStore().totpRequired = true;
    const wrapper = await renderLogin();
    expect(wrapper.find("#login-totp").attributes("autocomplete")).toBe("one-time-code");
  });
  it("初始化输入错误留在表单里且不发送请求", async () => {
    const request = vi.spyOn(api, "request");
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: "/setup", component: SetupWizard }, { path: "/dashboard", component: { template: "<div />" } },
    ] });
    await router.push("/setup");
    const wrapper = mount(SetupWizard, { global: { plugins: [pinia, router] } });
    wrappers.push(wrapper);
    await wrapper.find("#setup-username").setValue("ab");
    await wrapper.find("form").trigger("submit");
    expect(wrapper.find('[role="alert"]').exists()).toBe(true);
    expect(request).not.toHaveBeenCalled();
  });
});
