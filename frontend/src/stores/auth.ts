import { defineStore } from "pinia";
import { ref } from "vue";
import { api } from "@/api/client";
import { useDataCacheStore } from "./dataCache";

export interface User {
  id: number;
  username: string;
  role: string;
  is_active: boolean;
  created_at: string;
  last_login_at?: string;
}

export const useAuthStore = defineStore("auth", () => {
  const initialized = ref<boolean | null>(null);
  const authenticated = ref<boolean>(false);
  const user = ref<User | null>(null);
  const loading = ref<boolean>(false);
  const error = ref<string | null>(null);

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

  async function login(username: string, password: string) {
    loading.value = true;
    error.value = null;
    try {
      const res = await api.post<{ user: User }>("/api/auth/login", { username, password });
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
    checkStatus,
    login,
    setup,
    logout,
  };
});
