import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { defineComponent } from "vue";
import BaseModal from "@/components/BaseModal.vue";
import ConfirmDialog from "@/components/ConfirmDialog.vue";
import PageHeader from "@/components/PageHeader.vue";
import DataTable from "@/components/DataTable.vue";
import FreshnessStamp from "@/components/FreshnessStamp.vue";
import { useAsyncAction } from "@/composables/useAsyncAction";
import { useUiStore } from "@/stores/ui";

// 公共交互必须能独立验证，取消不能产生交易副作用。
const wrappers: VueWrapper[] = [];
beforeEach(() => setActivePinia(createPinia()));
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount());
  document.body.innerHTML = "";
  document.body.style.overflow = "";
  vi.useRealTimers();
});

describe("确认与模态框", () => {
  it("危险动作在确认前和取消后都不执行", async () => {
    const operation = vi.fn().mockResolvedValue(undefined);
    const wrapper = mount(defineComponent({ setup: () => useAsyncAction(), template: "<div />" }));
    wrappers.push(wrapper);
    const action = wrapper.vm as unknown as ReturnType<typeof useAsyncAction>;
    const result = action.run(operation, { confirm: { title: "危险操作", message: "确认" } });
    await flushPromises();
    expect(operation).not.toHaveBeenCalled();
    useUiStore().close();
    expect(await result).toEqual({ ok: false });
    expect(operation).not.toHaveBeenCalled();
  });

  it("接受后执行一次并展示成功，失败得到可见错误", async () => {
    const wrapper = mount(defineComponent({ setup: () => useAsyncAction(), template: "<div />" }));
    wrappers.push(wrapper);
    const action = wrapper.vm as unknown as ReturnType<typeof useAsyncAction>;
    const operation = vi.fn().mockResolvedValue(1);
    const result = action.run(operation, { confirm: { title: "确认", message: "确认" } });
    useUiStore().accept();
    expect(await result).toEqual({ ok: true, value: 1 });
    expect(operation).toHaveBeenCalledTimes(1);
    await action.run(async () => { throw new Error("offline"); });
    expect(useUiStore().toasts.at(-1)?.tone).toBe("danger");
  });

  it("接受确认后立即退出，不再发出尚未执行的危险动作", async () => {
    const wrapper = mount(defineComponent({ setup: () => useAsyncAction(), template: "<div />" }));
    wrappers.push(wrapper);
    const action = wrapper.vm as unknown as ReturnType<typeof useAsyncAction>;
    const operation = vi.fn().mockResolvedValue(undefined);
    const result = action.run(operation, { confirm: { title: "恢复交易", message: "确认恢复" } });
    const ui = useUiStore();
    ui.accept();
    ui.clear();
    expect(await result).toEqual({ ok: false });
    expect(operation).not.toHaveBeenCalled();
    expect(ui.toasts).toEqual([]);
  });

  it("旧会话成功响应不报成功、不提示，也不关闭新会话确认框", async () => {
    const wrapper = mount(defineComponent({ setup: () => useAsyncAction(), template: "<div />" }));
    wrappers.push(wrapper);
    const action = wrapper.vm as unknown as ReturnType<typeof useAsyncAction>;
    let resolve!: (value: number) => void;
    const response = new Promise<number>(accept => { resolve = accept; });
    const operation = vi.fn(() => response);
    const result = action.run(operation, { confirm: { title: "旧操作", message: "确认" } });
    const ui = useUiStore();
    ui.accept();
    await flushPromises();
    expect(operation).toHaveBeenCalledOnce();
    ui.clear();
    const newConfirmation = ui.ask({ title: "新操作", message: "重新确认" });
    resolve(1);
    expect(await result).toEqual({ ok: false });
    expect(ui.toasts).toEqual([]);
    expect(ui.confirmation?.title).toBe("新操作");
    expect(ui.confirming).toBe(false);
    ui.close();
    expect(await newConfirmation).toBe(false);
  });

  it("旧会话失败响应不调用旧 onError、不提示，也不关闭新确认框", async () => {
    const wrapper = mount(defineComponent({ setup: () => useAsyncAction(), template: "<div />" }));
    wrappers.push(wrapper);
    const action = wrapper.vm as unknown as ReturnType<typeof useAsyncAction>;
    let reject!: (cause: Error) => void;
    const response = new Promise<never>((_, fail) => { reject = fail; });
    const onError = vi.fn();
    const result = action.run(() => response, { confirm: { title: "旧操作", message: "确认" }, onError });
    const ui = useUiStore();
    ui.accept();
    await flushPromises();
    ui.clear();
    const newConfirmation = ui.ask({ title: "新操作", message: "重新确认" });
    reject(new Error("offline"));
    expect(await result).toEqual({ ok: false });
    expect(onError).not.toHaveBeenCalled();
    expect(action.error).toBe("");
    expect(ui.toasts).toEqual([]);
    expect(ui.confirmation?.title).toBe("新操作");
    ui.close();
    expect(await newConfirmation).toBe(false);
  });

  it("同一会话中旧确认结束后，迟到响应不影响后来确认", async () => {
    const wrapper = mount(defineComponent({ setup: () => useAsyncAction(), template: "<div />" }));
    wrappers.push(wrapper);
    const action = wrapper.vm as unknown as ReturnType<typeof useAsyncAction>;
    let resolve!: (value: number) => void;
    const response = new Promise<number>(accept => { resolve = accept; });
    const result = action.run(() => response, { confirm: { title: "旧操作", message: "确认" } });
    const ui = useUiStore();
    ui.accept();
    await flushPromises();
    ui.close();
    const newConfirmation = ui.ask({ title: "新操作", message: "重新确认" });
    resolve(1);
    expect(await result).toEqual({ ok: false });
    expect(ui.toasts).toEqual([]);
    expect(ui.confirmation?.title).toBe("新操作");
    ui.close();
    expect(await newConfirmation).toBe(false);
  });

  // 无需确认的保存与测试请求也不能把迟到结果带入下一次登录。
  it.each(["success", "failure"] as const)("退出后无需确认的 %s 响应被作废", async outcome => {
    const wrapper = mount(defineComponent({ setup: () => useAsyncAction(), template: "<div />" }));
    wrappers.push(wrapper);
    const action = wrapper.vm as unknown as ReturnType<typeof useAsyncAction>;
    let resolve!: (value: number) => void;
    let reject!: (cause: Error) => void;
    const response = new Promise<number>((accept, fail) => { resolve = accept; reject = fail; });
    const onError = vi.fn();
    const result = action.run(() => response, { onError });
    const ui = useUiStore();
    ui.clear();
    if (outcome === "success") resolve(1);
    else reject(new Error("offline"));
    expect(await result).toEqual({ ok: false });
    expect(action.error).toBe("");
    expect(onError).not.toHaveBeenCalled();
    expect(ui.toasts).toEqual([]);
  });

  it("跨组件并发确认不会接管或关闭已有操作", async () => {
    const firstWrapper = mount(defineComponent({ setup: () => useAsyncAction(), template: "<div />" }));
    const secondWrapper = mount(defineComponent({ setup: () => useAsyncAction(), template: "<div />" }));
    wrappers.push(firstWrapper, secondWrapper);
    const first = firstWrapper.vm as unknown as ReturnType<typeof useAsyncAction>;
    const second = secondWrapper.vm as unknown as ReturnType<typeof useAsyncAction>;
    let resolveFirst!: (value: number) => void;
    const firstResponse = new Promise<number>(accept => { resolveFirst = accept; });
    const firstOperation = vi.fn(() => firstResponse);
    const secondOperation = vi.fn().mockResolvedValue(2);
    const firstResult = first.run(firstOperation, { confirm: { title: "账户操作", message: "确认" } });
    const ui = useUiStore();
    const owner = ui.confirmationId;
    expect(await second.run(secondOperation, { confirm: { title: "绑定操作", message: "确认" } })).toEqual({ ok: false });
    expect(secondOperation).not.toHaveBeenCalled();
    expect(ui.confirmationId).toBe(owner);
    expect(ui.confirmation?.title).toBe("账户操作");
    ui.accept();
    await flushPromises();
    expect(ui.confirming).toBe(true);
    expect(await second.run(secondOperation, { confirm: { title: "绑定操作", message: "确认" } })).toEqual({ ok: false });
    expect(ui.confirmationId).toBe(owner);
    expect(ui.confirming).toBe(true);
    resolveFirst(1);
    expect(await firstResult).toEqual({ ok: true, value: 1 });
    const nextResult = second.run(secondOperation, { confirm: { title: "绑定操作", message: "确认" } });
    ui.accept();
    expect(await nextResult).toEqual({ ok: true, value: 2 });
    expect(secondOperation).toHaveBeenCalledOnce();
  });

  it("取消后新请求重新确认，不能沿用取消的许可", async () => {
    const wrapper = mount(defineComponent({ setup: () => useAsyncAction(), template: "<div />" }));
    wrappers.push(wrapper);
    const action = wrapper.vm as unknown as ReturnType<typeof useAsyncAction>;
    const operation = vi.fn().mockResolvedValue(1);
    const options = { confirm: { title: "启用绑定", message: "确认" } };
    const cancelled = action.run(operation, options);
    const ui = useUiStore();
    const previousOwner = ui.confirmationId;
    ui.close();
    expect(await cancelled).toEqual({ ok: false });
    const nextResult = action.run(operation, options);
    expect(ui.confirmationId).not.toBe(previousOwner);
    expect(operation).not.toHaveBeenCalled();
    ui.accept();
    expect(await nextResult).toEqual({ ok: true, value: 1 });
    expect(operation).toHaveBeenCalledOnce();
  });

  it("确认请求失败后释放 busy，重试仍需一次新的确认", async () => {
    const wrapper = mount(defineComponent({ setup: () => useAsyncAction(), template: "<div />" }));
    wrappers.push(wrapper);
    const action = wrapper.vm as unknown as ReturnType<typeof useAsyncAction>;
    const operation = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce(1);
    const options = { confirm: { title: "重置熔断", message: "确认" } };
    const failed = action.run(operation, options);
    const ui = useUiStore();
    const previousOwner = ui.confirmationId;
    ui.accept();
    expect(await failed).toEqual({ ok: false });
    expect(ui.confirmation).toBeNull();
    expect(ui.confirming).toBe(false);
    expect(action.pending).toBe(false);
    const retried = action.run(operation, options);
    expect(operation).toHaveBeenCalledOnce();
    expect(ui.confirmationId).not.toBe(previousOwner);
    expect(action.error).toBe("");
    ui.accept();
    expect(await retried).toEqual({ ok: true, value: 1 });
    expect(operation).toHaveBeenCalledTimes(2);
    expect(ui.confirmation).toBeNull();
  });

  // 普通请求与另一组件的危险确认可以并存，完成时只更新自己的结果。
  it("无需确认的请求完成不会关闭另一个组件的确认框", async () => {
    const wrapper = mount(defineComponent({ setup: () => useAsyncAction(), template: "<div />" }));
    wrappers.push(wrapper);
    const action = wrapper.vm as unknown as ReturnType<typeof useAsyncAction>;
    let resolve!: (value: number) => void;
    const response = new Promise<number>(accept => { resolve = accept; });
    const result = action.run(() => response, { success: false });
    const ui = useUiStore();
    const confirmation = ui.ask({ title: "实盘操作", message: "等待确认", environment: "live" });
    const owner = ui.confirmationId;
    resolve(1);
    expect(await result).toEqual({ ok: true, value: 1 });
    expect(ui.confirmationId).toBe(owner);
    expect(ui.confirmation?.title).toBe("实盘操作");
    ui.close();
    expect(await confirmation).toBe(false);
  });

  it("实盘确认只需在弹窗点击确认，不需要勾选或输入", async () => {
    const ui = useUiStore();
    const answer = ui.ask({ title: "实盘", message: "实盘操作", environment: "live" });
    const wrapper = mount(ConfirmDialog, { attachTo: document.body, global: { stubs: { Teleport: true } } });
    wrappers.push(wrapper);
    await flushPromises();
    expect(wrapper.text()).toContain("LIVE");
    expect(wrapper.find('input[type="checkbox"]').exists()).toBe(false);
    expect(wrapper.find('input[autocomplete="off"]').exists()).toBe(false);
    const button = wrapper.find("button.button-danger");
    expect(button.attributes("disabled")).toBeUndefined();
    await button.trigger("click");
    await expect(answer).resolves.toBe(true);
    ui.close();
  });

  it("Esc 和遮罩可关闭且焦点不越界，busy 时不关闭", async () => {
    const wrapper = mount(BaseModal, { props: { open: true, title: "测试" }, attachTo: document.body,
      slots: { default: '<button id="last-button">最后</button>' } });
    wrappers.push(wrapper);
    await flushPromises();
    const dialog = document.querySelector<HTMLElement>('[role="dialog"]')!;
    const last = document.getElementById("last-button")!;
    last.focus();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true }));
    expect(document.activeElement).toBe(dialog.querySelector("button"));
    await wrapper.setProps({ busy: true });
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(wrapper.emitted("close")).toBeUndefined();
    await wrapper.setProps({ busy: false });
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(wrapper.emitted("close")).toHaveLength(1);
    expect(dialog.className).toContain("max-h-[90vh]");
  });

  it("详情抽屉复用对话框语义和滚动高度", async () => {
    const wrapper = mount(BaseModal, { props: { open: true, title: "执行详情", drawer: true }, attachTo: document.body });
    wrappers.push(wrapper);
    await flushPromises();
    const dialog = document.querySelector<HTMLElement>('[role="dialog"]')!;
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(dialog.className).toContain("max-h-[90vh]");
    expect(dialog.className).toContain("h-[90vh]");
    expect(dialog.parentElement?.className).toContain("justify-end");
  });
});

describe("页面骨架", () => {
  it("页头展示首要摘要和主操作", () => {
    const wrapper = mount(PageHeader, { props: { title: "绑定", summary: "5 个绑定，1 个异常" }, slots: { actions: "<button>新建</button>" } });
    wrappers.push(wrapper);
    expect(wrapper.find("h1").text()).toBe("绑定");
    expect(wrapper.text()).toContain("1 个异常");
    expect(wrapper.find("button").text()).toBe("新建");
  });

  it("表格加载、空和错误状态互相区分并可重试", async () => {
    const wrapper = mount(DataTable, { props: { rows: [], columns: [], rowKey: () => 1, label: "列表", loading: true } });
    wrappers.push(wrapper);
    expect(wrapper.attributes("aria-busy")).toBe("true");
    await wrapper.setProps({ loading: false });
    expect(wrapper.text()).toContain("暂无数据");
    await wrapper.setProps({ error: "请求失败" });
    expect(wrapper.find('[role="alert"]').text()).toContain("请求失败");
    await wrapper.find("button").trigger("click");
    expect(wrapper.emitted("retry")).toHaveLength(1);
  });

  it("新鲜度由共享时钟推进，阈值与后端stale均可见", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(1800000000000);
    const wrapper = mount(FreshnessStamp, { props: { seconds: 28, receivedAt: Date.now() } });
    wrappers.push(wrapper);
    expect(wrapper.text()).toContain("28");
    await vi.advanceTimersByTimeAsync(2000);
    expect(wrapper.text()).toContain("30");
    expect(wrapper.classes()).toContain("text-warning");
    await wrapper.setProps({ stale: true });
    expect(wrapper.classes()).toContain("text-danger");
    expect(wrapper.text()).toContain("已过期");
    await wrapper.setProps({ seconds: null, stale: false });
    expect(wrapper.text()).toBe("—");
  });
});
