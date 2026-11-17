import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import RiskPolicyFields from "@/components/RiskPolicyFields.vue";
import { DEFAULT_RISK } from "@/constants/validation";
import { useI18n } from "@/i18n";

describe("默认风控页按功能分区", () => {
  it("全部模式分四个区，标题顺序固定，字段齐全", () => {
    const page = mount(RiskPolicyFields, { props: { modelValue: { ...DEFAULT_RISK }, prefix: "risk_defaults." } });
    const { t } = useI18n();
    expect(page.findAll("legend").map(el => el.text())).toEqual([
      t("riskGroup.execution"), t("riskGroup.leverage"), t("riskGroup.breaker"), t("riskGroup.protection"),
    ]);
    expect(page.findAll("input[type='checkbox']")).toHaveLength(5);
    expect(page.findAll("select")).toHaveLength(3);
    page.unmount();
  });
  it("section 模式保持单区无标题", () => {
    const page = mount(RiskPolicyFields, { props: { modelValue: { ...DEFAULT_RISK }, section: "risk", prefix: "risk." } });
    expect(page.findAll("legend")).toHaveLength(0);
    expect(page.findAll("select")).toHaveLength(0);
    expect(page.findAll("input[type='checkbox']")).toHaveLength(2);
    page.unmount();
  });
});
