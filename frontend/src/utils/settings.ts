import type { SettingsResponse } from "@/types/api";

export type SettingsTab = "notifications" | "risk_defaults" | "resilience" | "security" | "public_monitoring_enabled";
export const SETTINGS_TABS: SettingsTab[] = ["notifications", "risk_defaults", "resilience", "security", "public_monitoring_enabled"];
export type EditableSettings = Pick<SettingsResponse, "notifications" | "risk_defaults" | "resilience" | "public_monitoring_enabled">;

// 使用白名单按页签构造 payload，旧急停状态和只读安全信息永远不能回写。
export function settingsPayload(settings: SettingsResponse, tab: SettingsTab): Partial<EditableSettings> {
  if (tab === "security") return {};
  return { [tab]: settings[tab] };
}
export function cloneSettings(settings: SettingsResponse): SettingsResponse {
  // Vue 响应式代理不能交给 structuredClone；接口数据本身为 JSON。
  return JSON.parse(JSON.stringify(settings)) as SettingsResponse;
}
export function settingsDirty(before: SettingsResponse | null, after: SettingsResponse | null, tab?: SettingsTab): boolean {
  if (!before || !after) return false;
  return (tab ? [tab] : SETTINGS_TABS).some(key => key !== "security" && JSON.stringify(before[key]) !== JSON.stringify(after[key]));
}
