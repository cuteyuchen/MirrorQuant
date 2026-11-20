import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import NotificationSettingsPanel from "@/components/settings/NotificationSettingsPanel.vue";
import { api } from "@/api/client";
import type { LeadSubscription, NotificationSettings } from "@/types/api";

function defaultSettings(): NotificationSettings {
  return {
    display_unit: "notional",
    webhook_enabled: false,
    webhook_url: "",
    telegram_enabled: false,
    telegram_bot_token: "",
    telegram_chat_id: "",
    wecom_enabled: false,
    wecom_webhook_url: "",
    dingtalk_enabled: false,
    dingtalk_webhook_url: "",
    outbox_enabled: false,
    outbox_path: "",
    notify_on_execution: true,
    notify_on_lead_order: true,
    notify_on_risk_block: true,
    notify_on_security_alert: true,
  };
}

describe("Notification subscriptions and per-lead routing", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders lead subscriptions with copying vs watching badges and toggles state", async () => {
    const mockSubscriptions: LeadSubscription[] = [
      {
        id: 1,
        leadId: 10,
        leadName: "Alpha Trader",
        leadPlatform: "binance_bapi",
        isEnabled: true,
        hasBinding: true,
        bindingCount: 1,
      },
      {
        id: 2,
        leadId: 20,
        leadName: "Beta Watcher",
        leadPlatform: "binance_bapi",
        isEnabled: false,
        hasBinding: false,
        bindingCount: 0,
      },
    ];

    vi.spyOn(api, "get").mockImplementation(async (url: string) => {
      if (url === "/api/notifications/subscriptions") {
        return { items: mockSubscriptions };
      }
      return { items: [] };
    });

    const putSpy = vi.spyOn(api, "put").mockImplementation(async (url: string, body: unknown) => {
      if (url === "/api/notifications/subscriptions") {
        const payload = body as { subscriptions: { leadId: number; isEnabled: boolean }[] };
        const updated = mockSubscriptions.map(s => {
          const match = payload.subscriptions.find(p => p.leadId === s.leadId);
          return match ? { ...s, isEnabled: match.isEnabled } : s;
        });
        return { items: updated };
      }
      return {};
    });

    const wrapper = mount(NotificationSettingsPanel, {
      props: {
        modelValue: defaultSettings(),
        errors: {},
        dirty: false,
      },
    });

    await flushPromises();

    // Check that Alpha Trader and Beta Watcher are rendered
    expect(wrapper.text()).toContain("Alpha Trader");
    expect(wrapper.text()).toContain("Beta Watcher");

    // Check badges
    expect(wrapper.text()).toContain("跟单中（成交自动汇总）");
    expect(wrapper.text()).toContain("仅接收带单员动态");

    // Find the toggle checkboxes for subscriptions
    const subCheckboxes = wrapper.findAll('input[type="checkbox"]');
    // The last two checkboxes correspond to the two subscriptions
    const betaCheckbox = subCheckboxes[subCheckboxes.length - 1];
    expect(betaCheckbox.element.checked).toBe(false);

    // Toggle Beta Watcher
    await betaCheckbox.setValue(true);
    await flushPromises();

    expect(putSpy).toHaveBeenCalledWith("/api/notifications/subscriptions", {
      subscriptions: [{ leadId: 20, isEnabled: true }],
    });
  });
});
