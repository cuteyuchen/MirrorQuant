import { defineStore } from "pinia";
import { ref } from "vue";
import { api, ApiError } from "@/api/client";
import { useDataCacheStore } from "./dataCache";

export interface User {
  id: number;
  username: string;
  role: string;
  is_active: boolean;
  totp_enabled?: boolean;
  created_at: string;
  last_login_at?: string;
}

export interface CaptchaChallenge {
  captchaToken: string;
  svg: string;
  expiresInSec: number;
}

export const useAuthStore = defineStore("auth", () => {
  const initialized = ref<boolean | null>(null);
  const authenticated = ref<boolean>(false);
  const user = ref<User | null>(null);
  const loading = ref<boolean>(false);
  const error = ref<string | null>(null);
  const captchaRequired = ref<boolean>(false);
  const totpRequired = ref<boolean>(false);
  const lockRemainingSec = ref<number>(0);
  const captchaData = ref<CaptchaChallenge | null>(null);

  async function checkStatus() {
    loading.value = true;
    error.value = null;
    try {
      const data = await api.get<{ initialized: boolean; authenticated: boolean; user: User | null }>(
        "/api/auth/status"
      );
      initialized.value = data.initialized;
      authenticated.value = data.authenticated;
      user.value = data.user;
    } catch (err: any) {
      error.value = err.message;
    } finally {
      loading.value = false;
    }
  }

  async function fetchCaptcha() {
    try {
      captchaData.value = await api.get<CaptchaChallenge>("/api/auth/captcha");
    } catch {
      captchaData.value = null;
    }
  }

  async function login(
    username: string,
    password: string,
    captchaToken?: string,
    captchaAnswer?: string,
    totpCode?: string
  ) {
    loading.value = true;
    error.value = null;
    try {
      const res = await api.post<{ user: User }>("/api/auth/login", {
        username,
        password,
        captchaToken: captchaToken || undefined,
        captchaAnswer: captchaAnswer || undefined,
        totpCode: totpCode || undefined,
      });
      authenticated.value = true;
      user.value = res.user;
      captchaRequired.value = false;
      totpRequired.value = false;
      lockRemainingSec.value = 0;
      captchaData.value = null;
      return true;
    } catch (err: any) {
      if (err instanceof ApiError && err.data) {
        const detail = typeof err.data.detail === "object" ? err.data.detail : err.data;
        if (detail?.captchaRequired) {
          captchaRequired.value = true;
          await fetchCaptcha();
        }
        if (detail?.totpRequired) {
          totpRequired.value = true;
        }
        if (typeof detail?.lockRemainingSec === "number" && detail.lockRemainingSec > 0) {
          lockRemainingSec.value = detail.lockRemainingSec;
        }
        error.value = detail?.message || err.detail;
      } else {
        error.value = err.message;
      }
      return false;
    } finally {
      loading.value = false;
    }
  }

  async function setup(username: string, password: string) {
    loading.value = true;
    error.value = null;
    try {
      const res = await api.post<{ user: User }>("/api/auth/setup", { username, password });
      initialized.value = true;
      authenticated.value = true;
      user.value = res.user;
      return true;
    } catch (err: any) {
      error.value = err.message;
      return false;
    } finally {
      loading.value = false;
    }
  }

  async function logout() {
    try {
      await api.post("/api/auth/logout");
    } finally {
      authenticated.value = false;
      user.value = null;
      try {
        useDataCacheStore().clearAll();
      } catch {
        // ignore
      }
    }
  }

  return {
    initialized,
    authenticated,
    user,
    loading,
    error,
    captchaRequired,
    totpRequired,
    lockRemainingSec,
    captchaData,
    checkStatus,
    fetchCaptcha,
    login,
    setup,
    logout,
  };
});
