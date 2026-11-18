import { beforeEach, describe, expect, it } from "vitest";
import { defineComponent, h } from "vue";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { resolveDisplayUnit, useDisplayUnit } from "@/composables/useDisplayUnit";
import { useDataCacheStore } from "@/stores/dataCache";
import { mockSettings } from "@/mocks/board";

describe("resolveDisplayUnit", () => {
  it("defaults unknown values to notional", () => {
    expect(resolveDisplayUnit(undefined)).toBe("notional");
    expect(resolveDisplayUnit("bogus")).toBe("notional");
    expect(resolveDisplayUnit("quantity")).toBe("quantity");
    expect(resolveDisplayUnit("both")).toBe("both");
  });
});

function probe() {
  return defineComponent({
    setup() {
      const { unit, showQuantity, showNotional, sizeText } = useDisplayUnit();
      return () => h("div", [
        h("span", { "data-testid": "unit" }, unit.value),
        h("span", { "data-testid": "qty" }, String(showQuantity.value)),
        h("span", { "data-testid": "notional" }, String(showNotional.value)),
        h("span", { "data-testid": "size" }, sizeText(2, 200, 100)),
        h("span", { "data-testid": "derived" }, sizeText(2, null, 100)),
      ]);
    },
  });
}

describe("useDisplayUnit", () => {
  beforeEach(() => { setActivePinia(createPinia()); });
  function seed(unit: "notional" | "quantity" | "both") {
    const settings = structuredClone(mockSettings);
    settings.notifications!.display_unit = unit;
    useDataCacheStore().settings = { data: settings, fetchedAt: Date.now() };
  }
  it("reads the configured unit from cached settings", () => {
    seed("quantity");
    const wrapper = mount(probe());
    expect(wrapper.find('[data-testid="unit"]').text()).toBe("quantity");
    expect(wrapper.find('[data-testid="qty"]').text()).toBe("true");
    expect(wrapper.find('[data-testid="notional"]').text()).toBe("false");
    expect(wrapper.find('[data-testid="size"]').text()).toBe("2");
    wrapper.unmount();
  });
  it("defaults to notional when settings are absent", () => {
    const wrapper = mount(probe());
    expect(wrapper.find('[data-testid="unit"]').text()).toBe("notional");
    expect(wrapper.find('[data-testid="size"]').text()).toBe("$200.00");
    expect(wrapper.find('[data-testid="derived"]').text()).toBe("$200.00");
    wrapper.unmount();
  });
  it("shows both units in both mode", () => {
    seed("both");
    const wrapper = mount(probe());
    expect(wrapper.find('[data-testid="size"]').text()).toBe("2 ($200.00)");
    wrapper.unmount();
  });
});
