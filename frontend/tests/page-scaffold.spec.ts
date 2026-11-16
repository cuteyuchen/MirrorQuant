import { afterEach, describe, expect, it } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import DataTable from "@/components/DataTable.vue";
import PageHeader from "@/components/PageHeader.vue";

// 这里验证布局约束与三态结构，不代替真实浏览器的像素验收。
const wrappers: VueWrapper[] = [];
afterEach(() => wrappers.splice(0).forEach(wrapper => wrapper.unmount()));
function table(props: Record<string, unknown> = {}) {
  const wrapper = mount(DataTable, {
    props: { label: "账户列表", rows: [], columns: [{ key: "equity", label: "权益", numeric: true }], rowKey: (row: { id: number }) => row.id, ...props },
    slots: { "cell-equity": "<strong>$123,456,789,012,345.67</strong>" },
  });
  wrappers.push(wrapper);
  return wrapper;
}

describe("页面骨架", () => {
  it("页首具有明确标题、汇总与主操作区", () => {
    const wrapper = mount(PageHeader, { props: { title: "账户", summary: "3 个账户，1 个异常" }, slots: { actions: "<button>新建账户</button>" } });
    wrappers.push(wrapper);
    expect(wrapper.find("h1").text()).toBe("账户");
    expect(wrapper.text()).toContain("3 个账户，1 个异常");
    expect(wrapper.find("button").text()).toBe("新建账户");
  });

  it("表格分别呈现加载、空、错误与重试状态", async () => {
    const wrapper = table({ loading: true });
    expect(wrapper.attributes("aria-busy")).toBe("true");
    expect(wrapper.find('[role="status"]').exists()).toBe(true);
    await wrapper.setProps({ loading: false, empty: "尚无账户" });
    expect(wrapper.text()).toContain("尚无账户");
    await wrapper.setProps({ error: "连接失败" });
    expect(wrapper.find('[role="alert"]').text()).toContain("连接失败");
    await wrapper.find("button").trigger("click");
    expect(wrapper.emitted("retry")).toHaveLength(1);
  });

  it("手机单元格限制最小宽度并允许长数字换行", () => {
    const wrapper = table({ rows: [{ id: 1 }] });
    const mobile = wrapper.find("article");
    const cell = mobile.find(".number");
    expect(cell.classes()).toContain("min-w-0");
    expect(cell.classes()).toContain("[overflow-wrap:anywhere]");
    expect(cell.text()).toContain("$123,456,789,012,345.67");
    expect(wrapper.find("td").classes()).toContain("number");
  });
});
