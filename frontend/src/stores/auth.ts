import { defineStore } from "pinia";
import { ref } from "vue";
import { api, ApiError } from "@/api/client";
import { useDataCacheStore } from "./dataCache";
import { useSocketStore } from "./socket";
import { useUiStore } from "./ui";
import { useI18n } from "@/i18n";
import { errorMessage } from "@/utils/errors";
import type { AuthStatus, CaptchaChallenge, User } from "@/types/api";
export type { User, CaptchaChallenge } from "@/types/api";

export const useAuthStore = defineStore("auth", () => {
  const initialized = ref<boolean | null>(null);
  const authenticated = ref(false);
  const user = ref<User | null>(null);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const captchaRequired = ref(false);
  const totpRequired = ref(false);
  const lockRemainingSec = ref(0);
  const captchaData = ref<CaptchaChallenge | null>(null);
  let sequence = 0;
  let captchaSequence = 0;
  let generation = 0;
  function clearSession() {
    sequence++; captchaSequence++; generation++;
    authenticated.value = false; user.value = null; error.value = null;
    loading.value = false;
    captchaRequired.value = false; totpRequired.value = false; captchaData.value = null; lockRemainingSec.value = 0;
    useDataCacheStore().clearAll(); useSocketStore().disconnect(); useUiStore().clear();
  }
  async function checkStatus() {
    const request = ++sequence;
    loading.value = true; error.value = null;
    try {
      const data = await api.get<AuthStatus>("/api/auth/status");
      if (request !== sequence) return;
      // 形状未知不是“尚未初始化”，避免网关错误响应进入初始化向导。
      if (typeof data?.initialized !== "boolean" || typeof data.authenticated !== "boolean" || (data.authenticated && !data.user)) {
        throw new ApiError(502, "INVALID_RESPONSE");
      }
      initialized.value = data.initialized;
      if (!data.authenticated && authenticated.value) clearSession();
      authenticated.value = data.authenticated; user.value = data.user;
    } catch (cause: unknown) {
      if (request !== sequence) return;
      // 未连接与未初始化是不同状态，断网绝不能触发初始化向导。
      initialized.value = null; error.value = errorMessage(cause);
    } finally { if (request === sequence) loading.value = false; }
  }
  async function fetchCaptcha() {
    const request = ++captchaSequence;
    const epoch = generation;
    try {
      const challenge = await api.get<CaptchaChallenge>("/api/auth/captcha");
      if (request === captchaSequence && epoch === generation) captchaData.value = challenge;
    } catch (cause: unknown) {
      if (request === captchaSequence && epoch === generation) { captchaData.value = null; error.value = errorMessage(cause); }
    }
  }
  async function login(username: string, password: string, captchaToken?: string, captchaAnswer?: string, totpCode?: string) {
    const request = ++sequence;
    loading.value = true; error.value = null;
    try {
      const response = await api.post<{ user: User }>("/api/auth/login", { username, password, captchaToken, captchaAnswer, totpCode });
      // 登出或另一轮认证已使本轮失效，迟到响应不得恢复本地会话。
      if (request !== sequence) return false;
      if (!response?.user) throw new ApiError(502, "INVALID_RESPONSE");
      initialized.value = true; authenticated.value = true; user.value = response.user;
      captchaRequired.value = false; totpRequired.value = false; lockRemainingSec.value = 0; captchaData.value = null;
      return true;
    } catch (cause: unknown) {
      if (request !== sequence) return false;
      const body = cause instanceof ApiError ? cause.data : null;
      const detail = body?.detail;
      const guards = detail && typeof detail === "object" && !Array.isArray(detail) ? detail : body;
      if (guards) {
        // 登录错误既可能使用顶层字段，也可能来自 FastAPI detail 对象。
        captchaRequired.value = guards.captchaRequired === true;
        totpRequired.value = guards.totpRequired === true;
        lockRemainingSec.value = typeof guards.lockRemainingSec === "number" ? Math.max(0, guards.lockRemainingSec) : 0;
        if (captchaRequired.value) {
          await fetchCaptcha();
          if (request !== sequence) return false;
        }
      }
      error.value = errorMessage(cause);
      return false;
    } finally { if (request === sequence) loading.value = false; }
  }
  async function register(username: string, password: string, inviteCode: string) {
    const request = ++sequence;
    loading.value = true; error.value = null;
    try {
      // 注册成功由后端设 auth cookie，会话只认 cookie，不在本地存 token。
      const response = await api.post<{ user: User }>("/api/auth/register", { username, password, inviteCode });
      if (request !== sequence) return false;
      if (!response?.user) throw new ApiError(502, "INVALID_RESPONSE");
      initialized.value = true; authenticated.value = true; user.value = response.user;
      captchaRequired.value = false; totpRequired.value = false; lockRemainingSec.value = 0; captchaData.value = null;
      return true;
    } catch (cause: unknown) {
      if (request !== sequence) return false;
      // 注册失败按状态码给本地化原因：400 邀请码问题、409 用户名已存在，其余沿用通用映射。
      const { t } = useI18n();
      if (cause instanceof ApiError && cause.status === 400) error.value = t("register.invalidInvite");
      else if (cause instanceof ApiError && cause.status === 409) error.value = t("register.usernameTaken");
      else error.value = errorMessage(cause);
      return false;
    } finally { if (request === sequence) loading.value = false; }
  }
  async function setup(username: string, password: string, bootstrapToken: string) {
    const request = ++sequence;
    loading.value = true; error.value = null;
    try {
      const response = await api.request<{ user: User }>("/api/auth/setup", {
        method: "POST", headers: { "X-MQ-Bootstrap-Token": bootstrapToken }, body: JSON.stringify({ username, password }),
      });
      if (request !== sequence) return false;
      if (!response?.user) throw new ApiError(502, "INVALID_RESPONSE");
      initialized.value = true; authenticated.value = true; user.value = response.user;
      return true;
    } catch (cause: unknown) { if (request === sequence) error.value = errorMessage(cause); return false; }
    finally { if (request === sequence) loading.value = false; }
  }
  async function logout() {
    // 请求失败也不保留本地会话；路由跳转由调用方在 finally 中完成。
    clearSession();
    try { await api.post("/api/auth/logout"); }
    catch (cause: unknown) { useUiStore().toast(errorMessage(cause), "warning"); }
    finally { clearSession(); }
  }
  return { initialized, authenticated, user, loading, error, captchaRequired, totpRequired, lockRemainingSec, captchaData, checkStatus, fetchCaptcha, login, register, setup, logout, clearSession };
});
