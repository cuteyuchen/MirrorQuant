import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mount, flushPromises, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import BaseModal from "@/components/BaseModal.vue";
import ConfirmDialog from "@/components/ConfirmDialog.vue";
import { useUiStore } from "@/stores/ui";

// 1.2.1 回归：确认框的层级必须高于页面弹窗，且与挂载顺序无关。
// 否则从其他路由进入账户页时，表单弹窗后挂载，会把确认框盖在下面，
// 实盘开户流程卡在“处理中”且永远等不到确认。
const wrappers: VueWrapper[] = [];
beforeEach(() => setActivePinia(createPinia()));
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount());
  document.body.innerHTML = "";
  document.body.style.overflow = "";
});

function overlayOf(dialog: Element): HTMLElement {
  return dialog.parentElement as HTMLElement;
}

describe("弹窗层叠顺序", () => {
  it("默认层级 50，确认框层级 60", async () => {
    const plain = mount(BaseModal, {
      props: { open: true, title: "表单" }, attachTo: document.body,
      slots: { default: "<button>表单按钮</button>" },
    });
    wrappers.push(plain);
    const ui = useUiStore();
    void ui.ask({ title: "实盘", message: "确认", environment: "live" });
    const dialog = mount(ConfirmDialog, { attachTo: document.body });
    wrappers.push(dialog);
    await flushPromises();
    const dialogs = Array.from(document.querySelectorAll('[role="dialog"]'));
    expect(dialogs).toHaveLength(2);
    const zs = dialogs.map(el => Number(getComputedStyle(overlayOf(el)).zIndex));
    expect(zs).toContain(50);
    expect(zs).toContain(60);
    ui.close();
  });

  it("页面弹窗后挂载时，确认框仍绘制在最上层", async () => {
    // 先挂载确认框、再挂载页面弹窗（从其他路由导航进入账户页的顺序）：
    // 确认框在 DOM 中居前，但层级更高，绘制顺序上仍在上。
    const ui = useUiStore();
    void ui.ask({ title: "实盘", message: "确认", environment: "live" });
    const dialog = mount(ConfirmDialog, { attachTo: document.body });
    wrappers.push(dialog);
    const plain = mount(BaseModal, {
      props: { open: true, title: "表单" }, attachTo: document.body,
      slots: { default: "<button>表单按钮</button>" },
    });
    wrappers.push(plain);
    await flushPromises();
    const dialogs = Array.from(document.querySelectorAll('[role="dialog"]'));
    expect(dialogs).toHaveLength(2);
    expect(dialogs[0].querySelector("h2")?.textContent).toBe("实盘");
    expect(dialogs[1].querySelector("h2")?.textContent).toBe("表单");
    const dialogZ = Number(getComputedStyle(overlayOf(dialogs[0])).zIndex);
    const plainZ = Number(getComputedStyle(overlayOf(dialogs[1])).zIndex);
    expect(dialogZ).toBeGreaterThan(plainZ);
    ui.close();
  });

  it("焦点陷阱跟随绘制顺序：焦点不被下层弹窗抢走", async () => {
    const ui = useUiStore();
    void ui.ask({ title: "实盘", message: "确认", environment: "live" });
    const dialog = mount(ConfirmDialog, { attachTo: document.body });
    wrappers.push(dialog);
    const plain = mount(BaseModal, {
      props: { open: true, title: "表单" }, attachTo: document.body,
      slots: { default: "<button>表单按钮</button>" },
    });
    wrappers.push(plain);
    await flushPromises();
    (document.activeElement as HTMLElement | null)?.blur?.();
    document.body.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
    await flushPromises();
    const dialogs = Array.from(document.querySelectorAll('[role="dialog"]'));
    // 焦点必须留在层级更高的确认框内，不能被后挂载的下层表单抢走。
    expect(dialogs[0].contains(document.activeElement)).toBe(true);
    expect(dialogs[1].contains(document.activeElement)).toBe(false);
    ui.close();
  });
});
