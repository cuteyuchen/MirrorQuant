import { defineStore } from "pinia";
import { shallowRef } from "vue";
import { api, ApiError, CancelledRequestError } from "@/api/client";
import type { Account, AuditLog, BindingsResponse, DashboardBoard, IntentsResponse, Lead, LeadSnapshot, LeadSubscription, ListResponse, OrderIntent, ProxyNode, SecurityGuard, SettingsResponse, UserPreferences } from "@/types/api";

interface CacheEntry<T> { data: T; fetchedAt: number }
// 列表缺失与真实空列表不同，失败时保留上次缓存并让页面显示错误态。
function requireItems<T>(response: ListResponse<T>): T[] {
  if (response.items == null) throw new ApiError(502, "INVALID_RESPONSE");
  return response.items;
}
export const useDataCacheStore = defineStore("dataCache", () => {
  const board = shallowRef<CacheEntry<DashboardBoard> | null>(null);
  const intents = shallowRef<CacheEntry<OrderIntent[]> | null>(null);
  const leads = shallowRef<CacheEntry<Lead[]> | null>(null);
  const leadSnapshots = shallowRef<Record<number, CacheEntry<LeadSnapshot | null>>>({});
  const accounts = shallowRef<CacheEntry<Account[]> | null>(null);
  const bindings = shallowRef<CacheEntry<BindingsResponse> | null>(null);
  const proxies = shallowRef<CacheEntry<ProxyNode[]> | null>(null);
  const settings = shallowRef<CacheEntry<SettingsResponse> | null>(null);
  const auditLogs = shallowRef<CacheEntry<AuditLog[]> | null>(null);
  const securityGuards = shallowRef<CacheEntry<SecurityGuard[]> | null>(null);
  const subscriptions = shallowRef<CacheEntry<LeadSubscription[]> | null>(null);
  const userPreferences = shallowRef<CacheEntry<UserPreferences> | null>(null);
  const inflight = new Map<string, Promise<unknown>>();
  const sequences = new Map<string, number>();
  let generation = 0;
  function isFresh<T>(entry: CacheEntry<T> | null | undefined, ttlMs: number) {
    return !!entry && Date.now() - entry.fetchedAt < ttlMs;
  }
  function load<T>(key: string, get: () => CacheEntry<T> | null | undefined, set: (entry: CacheEntry<T>) => void, fetcher: () => Promise<T>, force: boolean, ttlMs: number): Promise<T> {
    if (!force && inflight.has(key)) return inflight.get(key) as Promise<T>;
    if (!force && isFresh(get(), ttlMs)) return Promise.resolve(get()!.data);
    const sequence = (sequences.get(key) || 0) + 1;
    const epoch = generation;
    sequences.set(key, sequence);
    const current = () => generation === epoch && sequences.get(key) === sequence;
    const latest = (): Promise<T> => {
      if (generation !== epoch) return Promise.reject(new CancelledRequestError());
      const pending = inflight.get(key);
      if (pending) return pending as Promise<T>;
      const cached = get();
      return cached ? Promise.resolve(cached.data) : Promise.reject(new CancelledRequestError());
    };
    // 旧请求既不能覆盖缓存，也不能把旧值再交给视图。
    const request = fetcher().then(data => {
      if (!current()) return latest();
      set({ data, fetchedAt: Date.now() });
      return data;
    }, error => {
      if (!current()) return latest();
      throw error;
    }).finally(() => {
      if (current()) inflight.delete(key);
    });
    inflight.set(key, request);
    return request;
  }
  const fetchBoard = (force = false, ttlMs = 5000) => load("board", () => board.value, value => { board.value = value; }, () => api.get<DashboardBoard>("/api/dashboard/board"), force, ttlMs);
  const fetchIntents = (force = false, ttlMs = 5000) => load("intents", () => intents.value, value => { intents.value = value; }, async () => {
    const response = await api.get<IntentsResponse>("/api/dashboard/intents?state=UNKNOWN,SUBMITTING");
    // 未知列表不能被导航角标误报为零笔待处理订单。
    return requireItems(response);
  }, force, ttlMs);
  const fetchLeads = (force = false, ttlMs = 12000) => load("leads", () => leads.value, value => { leads.value = value; }, async () => requireItems(await api.get<ListResponse<Lead>>("/api/leads")), force, ttlMs);
  const fetchAccounts = (force = false, ttlMs = 15000) => load("accounts", () => accounts.value, value => { accounts.value = value; }, async () => requireItems(await api.get<ListResponse<Account>>("/api/accounts")), force, ttlMs);
  const fetchBindings = (force = false, ttlMs = 12000) => load("bindings", () => bindings.value, value => { bindings.value = value; }, async () => {
    const response = await api.get<BindingsResponse>("/api/bindings");
    requireItems(response); return response;
  }, force, ttlMs);
  const fetchProxies = (force = false, ttlMs = 20000) => load("proxies", () => proxies.value, value => { proxies.value = value; }, async () => requireItems(await api.get<ListResponse<ProxyNode>>("/api/proxies")), force, ttlMs);
  const fetchSettings = (force = false, ttlMs = 20000) => load("settings", () => settings.value, value => { settings.value = value; }, () => api.get<SettingsResponse>("/api/settings"), force, ttlMs);
  const fetchAuditLogs = (force = false, ttlMs = 20000) => load("auditLogs", () => auditLogs.value, value => { auditLogs.value = value; }, async () => requireItems(await api.get<ListResponse<AuditLog>>("/api/settings/audit-logs?limit=50")), force, ttlMs);
  const fetchSecurityGuards = (force = false, ttlMs = 15000) => load("securityGuards", () => securityGuards.value, value => { securityGuards.value = value; }, async () => requireItems(await api.get<ListResponse<SecurityGuard>>("/api/settings/security-guards")), force, ttlMs);
  const fetchLeadSnapshot = (id: number, force = false, ttlMs = 10000) => load(`lead_${id}`, () => leadSnapshots.value[id], value => { leadSnapshots.value = { ...leadSnapshots.value, [id]: value }; }, async () => (await api.get<{ snapshot: LeadSnapshot | null }>(`/api/leads/${id}/snapshot`)).snapshot, force, ttlMs);
  const fetchSubscriptions = (force = false, ttlMs = 12000) => load("subscriptions", () => subscriptions.value, value => { subscriptions.value = value; }, async () => requireItems(await api.get<ListResponse<LeadSubscription>>("/api/notifications/subscriptions")), force, ttlMs);
  const fetchUserPreferences = (force = false, ttlMs = 20000) => load("userPreferences", () => userPreferences.value, value => { userPreferences.value = value; }, () => api.get<UserPreferences>("/api/notifications/preferences"), force, ttlMs);
  function clearAll() {
    // 登出作废全部在途结果，不允许已退出账户的数据重新进入缓存。
    generation++;
    sequences.clear(); inflight.clear();
    board.value = null; intents.value = null; leads.value = null; leadSnapshots.value = {}; accounts.value = null;
    bindings.value = null; proxies.value = null; settings.value = null;
    auditLogs.value = null; securityGuards.value = null;
    subscriptions.value = null; userPreferences.value = null;
  }
  return { board, intents, leads, leadSnapshots, accounts, bindings, proxies, settings, auditLogs, securityGuards, subscriptions, userPreferences, isFresh, fetchBoard, fetchIntents, fetchLeads, fetchLeadSnapshot, fetchAccounts, fetchBindings, fetchProxies, fetchSettings, fetchAuditLogs, fetchSecurityGuards, fetchSubscriptions, fetchUserPreferences, clearAll };
});
