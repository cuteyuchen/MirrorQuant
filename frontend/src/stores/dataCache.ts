import { defineStore } from "pinia";
import { ref } from "vue";
import { api } from "@/api/client";

interface CacheEntry<T> {
  data: T;
  fetchedAt: number;
}

export const useDataCacheStore = defineStore("dataCache", () => {
  const overview = ref<CacheEntry<any> | null>(null);
  const leads = ref<CacheEntry<any[]> | null>(null);
  const leadSnapshots = ref<Record<number, CacheEntry<any>>>({});
  const accounts = ref<CacheEntry<any[]> | null>(null);
  const bindings = ref<CacheEntry<{ items: any[]; globalKillSwitch: boolean }> | null>(null);
  const proxies = ref<CacheEntry<any[]> | null>(null);
  const settings = ref<CacheEntry<any> | null>(null);
  const auditLogs = ref<CacheEntry<any[]> | null>(null);
  const securityGuards = ref<CacheEntry<any[]> | null>(null);

  const inflight: Record<string, Promise<any> | undefined> = {};

  function isFresh(entry: CacheEntry<any> | null | undefined, ttlMs: number): boolean {
    if (!entry) return false;
    return Date.now() - entry.fetchedAt < ttlMs;
  }

  function dedup<T>(key: string, force: boolean, fn: () => Promise<T>): Promise<T> {
    if (!force && inflight[key]) return inflight[key] as Promise<T>;
    const p = fn().finally(() => {
      delete inflight[key];
    });
    inflight[key] = p;
    return p;
  }

  async function fetchOverview(force = false, ttlMs = 6000) {
    if (!force && isFresh(overview.value, ttlMs)) return overview.value!.data;
    return dedup("overview", force, async () => {
      const data = await api.get<any>("/api/dashboard/overview");
      overview.value = { data, fetchedAt: Date.now() };
      return data;
    });
  }

  async function fetchLeads(force = false, ttlMs = 12000) {
    if (!force && isFresh(leads.value, ttlMs)) return leads.value!.data;
    return dedup("leads", force, async () => {
      const res = await api.get<{ items: any[] }>("/api/leads");
      leads.value = { data: res.items || [], fetchedAt: Date.now() };
      return leads.value.data;
    });
  }

  async function fetchLeadSnapshot(id: number, force = false, ttlMs = 10000) {
    const existing = leadSnapshots.value[id];
    if (!force && isFresh(existing, ttlMs)) return existing.data;
    return dedup(`lead_${id}`, force, async () => {
      const res = await api.get<{ snapshot: any }>(`/api/leads/${id}/snapshot`);
      leadSnapshots.value[id] = { data: res.snapshot, fetchedAt: Date.now() };
      return res.snapshot;
    });
  }

  async function fetchAccounts(force = false, ttlMs = 15000) {
    if (!force && isFresh(accounts.value, ttlMs)) return accounts.value!.data;
    return dedup("accounts", force, async () => {
      const res = await api.get<{ items: any[] }>("/api/accounts");
      accounts.value = { data: res.items || [], fetchedAt: Date.now() };
      return accounts.value.data;
    });
  }

  async function fetchBindings(force = false, ttlMs = 12000) {
    if (!force && isFresh(bindings.value, ttlMs)) return bindings.value!.data;
    return dedup("bindings", force, async () => {
      const res = await api.get<{ items: any[]; globalKillSwitch: boolean }>("/api/bindings");
      bindings.value = { data: res, fetchedAt: Date.now() };
      return res;
    });
  }

  async function fetchProxies(force = false, ttlMs = 20000) {
    if (!force && isFresh(proxies.value, ttlMs)) return proxies.value!.data;
    return dedup("proxies", force, async () => {
      const res = await api.get<{ items: any[] }>("/api/proxies");
      proxies.value = { data: res.items || [], fetchedAt: Date.now() };
      return proxies.value.data;
    });
  }

  async function fetchSettings(force = false, ttlMs = 20000) {
    if (!force && isFresh(settings.value, ttlMs)) return settings.value!.data;
    return dedup("settings", force, async () => {
      const data = await api.get<any>("/api/settings");
      settings.value = { data, fetchedAt: Date.now() };
      return data;
    });
  }

  async function fetchAuditLogs(force = false, ttlMs = 20000) {
    if (!force && isFresh(auditLogs.value, ttlMs)) return auditLogs.value!.data;
    return dedup("auditLogs", force, async () => {
      const res = await api.get<{ items: any[] }>("/api/settings/audit-logs?limit=50");
      auditLogs.value = { data: res.items || [], fetchedAt: Date.now() };
      return auditLogs.value.data;
    });
  }

  async function fetchSecurityGuards(force = false, ttlMs = 15000) {
    if (!force && isFresh(securityGuards.value, ttlMs)) return securityGuards.value!.data;
    return dedup("securityGuards", force, async () => {
      const res = await api.get<{ items: any[] }>("/api/settings/security-guards");
      securityGuards.value = { data: res.items || [], fetchedAt: Date.now() };
      return securityGuards.value.data;
    });
  }

  function clearAll() {
    overview.value = null;
    leads.value = null;
    leadSnapshots.value = {};
    accounts.value = null;
    bindings.value = null;
    proxies.value = null;
    settings.value = null;
    auditLogs.value = null;
    securityGuards.value = null;
  }

  return {
    overview,
    leads,
    leadSnapshots,
    accounts,
    bindings,
    proxies,
    settings,
    auditLogs,
    securityGuards,
    isFresh,
    fetchOverview,
    fetchLeads,
    fetchLeadSnapshot,
    fetchAccounts,
    fetchBindings,
    fetchProxies,
    fetchSettings,
    fetchAuditLogs,
    fetchSecurityGuards,
    clearAll,
  };
});
