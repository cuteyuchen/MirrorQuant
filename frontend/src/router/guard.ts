import type { RouteLocationNormalized, RouteLocationRaw } from "vue-router";
import { MOCK_MODE } from "@/api/mode";

export interface GuardAuth {
  initialized: boolean | null; authenticated: boolean; user?: { role?: string | null } | null;
  checkStatus: () => Promise<void>;
}
// 三态守卫独立于浏览器，可直接验证断网、未初始化和正常会话。
export async function guardRoute(to: Pick<RouteLocationNormalized, "path" | "fullPath" | "meta"> & { query?: RouteLocationNormalized["query"] }, auth: GuardAuth, mockMode = MOCK_MODE): Promise<RouteLocationRaw | true> {
  // 仅样例构建开放认证页预览，生产路由绝不以查询参数绕过守卫。
  if (mockMode && to.query?.preview === "1" && ["/login", "/register", "/setup", "/connection-error"].includes(to.path)) return true;
  if (to.path === "/connection-error" || to.meta.notFound) return true;
  if (auth.initialized === null) {
    try { await auth.checkStatus(); }
    catch { return { path: "/connection-error", query: { redirect: to.fullPath } }; }
  }
  if (auth.initialized === null) return { path: "/connection-error", query: { redirect: to.fullPath } };
  if (auth.initialized === false && to.path !== "/setup") return { path: "/setup" };
  if (auth.initialized && to.path === "/setup") return { path: auth.authenticated ? "/dashboard" : "/login" };
  if (to.meta.requiresAuth && !auth.authenticated) return { path: "/login", query: { redirect: to.fullPath } };
  // 管理员页只认 role === "admin"；非管理员（仍可看带单员列表）回看板。
  if (to.meta.requiresAdmin && auth.user?.role !== "admin") return { path: "/dashboard" };
  if ((to.path === "/login" || to.path === "/register") && auth.authenticated) return { path: "/dashboard" };
  return true;
}
export function safeRedirect(value: unknown): string {
  // 只允许站内路径，拒绝控制字符、编码反斜杠与认证回环。
  if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//") || /[\\\u0000-\u0020]/.test(value)) return "/dashboard";
  try {
    const path = decodeURIComponent(value.split(/[?#]/)[0] || "");
    if (path.startsWith("//") || /[\\\u0000-\u0020]/.test(path) || ["/login", "/register", "/setup", "/connection-error"].includes(path)) return "/dashboard";
  } catch { return "/dashboard"; }
  return value;
}
