import { computed, onActivated, onDeactivated, onMounted, onUnmounted, reactive, ref, watch, type Ref } from "vue";
import { api, isCancelled } from "@/api/client";
import type {
  AccountCashflowsResponse,
  AccountDetailResponse,
  AccountDetailSyncResponse,
  AccountEquityCurveResponse,
  AccountOrdersResponse,
  AccountPerformanceResponse,
  AccountTradesResponse,
} from "@/types/api";

export type DetailRange = "24h" | "7d" | "30d" | "90d" | "1y" | "all";
export type EquitySeriesKey = "equity" | "cumulativeTradingPnl" | "twrPct";

const POLL_INTERVAL_MS = 15_000;

export function useAccountDetail(accountId: Ref<number | null>) {
  const range = ref<DetailRange>("30d");
  const equitySeries = ref<EquitySeriesKey>("equity");
  const includeEstimated = ref(false);

  const detail = ref<AccountDetailResponse | null>(null);
  const equity = ref<AccountEquityCurveResponse | null>(null);
  const openOrders = ref<AccountOrdersResponse | null>(null);
  const cashflows = ref<AccountCashflowsResponse | null>(null);
  const trades = ref<AccountTradesResponse | null>(null);
  const orderHistory = ref<AccountOrdersResponse | null>(null);
  const performance = ref<AccountPerformanceResponse | null>(null);

  const syncing = ref(false);
  const syncResult = ref<AccountDetailSyncResponse | null>(null);
  const syncError = ref("");

  const loading = reactive({
    detail: false,
    equity: false,
    openOrders: false,
    cashflows: false,
    trades: false,
    orderHistory: false,
    performance: false,
  });

  const errors = reactive({
    detail: "",
    equity: "",
    openOrders: "",
    cashflows: "",
    trades: "",
    orderHistory: "",
    performance: "",
  });

  const cashflowFilters = reactive({
    category: "",
    asset: "",
    symbol: "",
    cursor: null as string | null,
    cursorStack: [] as (string | null)[],
  });

  const tradeFilters = reactive({
    recordType: "",
    source: "",
    symbol: "",
    bindingId: "",
    cursor: null as string | null,
    cursorStack: [] as (string | null)[],
  });

  const orderHistoryFilters = reactive({
    category: "",
    symbol: "",
    source: "",
    bindingId: "",
    cursor: null as string | null,
    cursorStack: [] as (string | null)[],
  });

  const openOrderCategory = ref("");

  const perfFilters = reactive({
    source: "",
    symbol: "",
    bindingId: "",
  });

  type ResourceKey = keyof typeof loading;
  const controllers: Partial<Record<ResourceKey, AbortController>> = {};
  const seqs: Record<ResourceKey, number> = {
    detail: 0,
    equity: 0,
    openOrders: 0,
    cashflows: 0,
    trades: 0,
    orderHistory: 0,
    performance: 0,
  };

  let timer: ReturnType<typeof setInterval> | null = null;
  const active = ref(false);
  const stopAutoPoll = ref(false);

  function abortKey(key: ResourceKey) {
    controllers[key]?.abort();
    delete controllers[key];
  }

  function abortAll() {
    (Object.keys(seqs) as ResourceKey[]).forEach(abortKey);
  }

  async function guardedFetch<T>(
    key: ResourceKey,
    endpointFn: (id: number) => string,
    target: Ref<T | null>,
    options: { stopPollOnAuthError?: boolean } = {},
  ): Promise<void> {
    const id = accountId.value;
    if (id == null || !Number.isInteger(id) || id <= 0) return;

    abortKey(key);
    const controller = new AbortController();
    controllers[key] = controller;
    const seq = ++seqs[key];
    loading[key] = true;
    errors[key] = "";

    try {
      const res = await api.get<T>(endpointFn(id), { signal: controller.signal });
      if (seq !== seqs[key] || controller.signal.aborted) return;
      target.value = res;
    } catch (err: unknown) {
      if (isCancelled(err) || seq !== seqs[key]) return;
      const status = (err as { status?: number })?.status;
      if (options.stopPollOnAuthError && (status === 403 || status === 404)) {
        stopAutoPoll.value = true;
        stopPolling();
      }
      errors[key] = (err as { detail?: string })?.detail || (err instanceof Error ? err.message : "Failed to load");
    } finally {
      if (seq === seqs[key]) {
        loading[key] = false;
        if (controllers[key] === controller) delete controllers[key];
      }
    }
  }

  function buildQuery(params: Record<string, string | number | boolean | null | undefined>) {
    const usp = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v === null || v === undefined || v === "") continue;
      usp.set(k, String(v));
    }
    const qs = usp.toString();
    return qs ? `?${qs}` : "";
  }

  const fetchDetail = () =>
    guardedFetch(
      "detail",
      id => `/api/accounts/${id}/detail${buildQuery({ range: range.value })}`,
      detail,
      { stopPollOnAuthError: true },
    );

  const fetchEquity = () =>
    guardedFetch(
      "equity",
      id =>
        `/api/accounts/${id}/equity${buildQuery({
          range: range.value,
          currency: "USDT",
          includeEstimated: includeEstimated.value ? "true" : "false",
        })}`,
      equity,
    );

  const fetchOpenOrders = () =>
    guardedFetch(
      "openOrders",
      id =>
        `/api/accounts/${id}/orders${buildQuery({
          state: "open",
          category: openOrderCategory.value || undefined,
          limit: 100,
        })}`,
      openOrders,
    );

  const fetchCashflows = () =>
    guardedFetch(
      "cashflows",
      id =>
        `/api/accounts/${id}/cashflows${buildQuery({
          category: cashflowFilters.category || undefined,
          asset: cashflowFilters.asset || undefined,
          symbol: cashflowFilters.symbol || undefined,
          cursor: cashflowFilters.cursor || undefined,
          limit: 50,
        })}`,
      cashflows,
    );

  const fetchTrades = () =>
    guardedFetch(
      "trades",
      id =>
        `/api/accounts/${id}/trades${buildQuery({
          recordType: tradeFilters.recordType || undefined,
          source: tradeFilters.source || undefined,
          symbol: tradeFilters.symbol || undefined,
          bindingId: tradeFilters.bindingId || undefined,
          cursor: tradeFilters.cursor || undefined,
          limit: 50,
        })}`,
      trades,
    );

  const fetchOrderHistory = () =>
    guardedFetch(
      "orderHistory",
      id =>
        `/api/accounts/${id}/orders${buildQuery({
          state: "history",
          category: orderHistoryFilters.category || undefined,
          symbol: orderHistoryFilters.symbol || undefined,
          source: orderHistoryFilters.source || undefined,
          bindingId: orderHistoryFilters.bindingId || undefined,
          cursor: orderHistoryFilters.cursor || undefined,
          limit: 50,
        })}`,
      orderHistory,
    );

  const fetchPerformance = () =>
    guardedFetch(
      "performance",
      id =>
        `/api/accounts/${id}/performance${buildQuery({
          range: range.value,
          source: perfFilters.source || undefined,
          symbol: perfFilters.symbol || undefined,
          bindingId: perfFilters.bindingId || undefined,
        })}`,
      performance,
    );

  async function refreshLive() {
    await Promise.all([fetchDetail(), fetchEquity(), fetchOpenOrders()]);
  }

  async function refreshAll() {
    stopAutoPoll.value = false;
    await Promise.all([
      fetchDetail(),
      fetchEquity(),
      fetchOpenOrders(),
      fetchCashflows(),
      fetchTrades(),
      fetchOrderHistory(),
      fetchPerformance(),
    ]);
  }

  async function triggerManualSync() {
    const id = accountId.value;
    if (id == null || syncing.value) return;
    syncing.value = true;
    syncError.value = "";
    try {
      syncResult.value = await api.post<AccountDetailSyncResponse>(`/api/accounts/${id}/detail/sync`);
      await refreshAll();
    } catch (err: unknown) {
      syncError.value = (err as { detail?: string })?.detail || (err instanceof Error ? err.message : "Sync failed");
    } finally {
      syncing.value = false;
    }
  }

  function stopPolling() {
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  }

  function syncPollingState() {
    const canPoll =
      active.value &&
      !stopAutoPoll.value &&
      accountId.value != null &&
      typeof document !== "undefined" &&
      !document.hidden;
    if (!canPoll) {
      stopPolling();
      return;
    }
    if (timer === null) {
      timer = setInterval(() => {
        if (!document.hidden && active.value && !stopAutoPoll.value) {
          void refreshLive();
        }
      }, POLL_INTERVAL_MS);
    }
  }

  function handleVisibilityChange() {
    if (typeof document !== "undefined" && document.hidden) {
      stopPolling();
    } else {
      syncPollingState();
      if (active.value && !stopAutoPoll.value) void refreshLive();
    }
  }

  // Range switches affect detail PnL, equity curve, and performance stats cleanly
  watch([range, includeEstimated], () => {
    if (!active.value) return;
    void Promise.all([fetchDetail(), fetchEquity(), fetchPerformance()]);
  });

  watch(openOrderCategory, () => {
    if (!active.value) return;
    void fetchOpenOrders();
  });

  watch(accountId, newId => {
    detail.value = null;
    equity.value = null;
    openOrders.value = null;
    cashflows.value = null;
    trades.value = null;
    orderHistory.value = null;
    performance.value = null;
    syncResult.value = null;
    syncError.value = "";
    stopAutoPoll.value = false;
    cashflowFilters.cursor = null;
    cashflowFilters.cursorStack = [];
    tradeFilters.cursor = null;
    tradeFilters.cursorStack = [];
    orderHistoryFilters.cursor = null;
    orderHistoryFilters.cursorStack = [];
    abortAll();
    if (newId != null && active.value) {
      void refreshAll();
    }
    syncPollingState();
  });

  onMounted(() => {
    active.value = true;
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", handleVisibilityChange);
    }
    if (accountId.value != null) {
      void refreshAll();
    }
    syncPollingState();
  });

  onActivated(() => {
    active.value = true;
    if (accountId.value != null && !detail.value) {
      void refreshAll();
    }
    syncPollingState();
  });

  onDeactivated(() => {
    active.value = false;
    stopPolling();
    abortAll();
  });

  onUnmounted(() => {
    active.value = false;
    stopPolling();
    abortAll();
    if (typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    }
  });

  const hasFatalError = computed(() => !detail.value && !!errors.detail);

  return {
    range,
    equitySeries,
    includeEstimated,
    detail,
    equity,
    openOrders,
    cashflows,
    trades,
    orderHistory,
    performance,
    loading,
    errors,
    syncing,
    syncResult,
    syncError,
    hasFatalError,
    cashflowFilters,
    tradeFilters,
    orderHistoryFilters,
    openOrderCategory,
    perfFilters,
    fetchDetail,
    fetchEquity,
    fetchOpenOrders,
    fetchCashflows,
    fetchTrades,
    fetchOrderHistory,
    fetchPerformance,
    refreshLive,
    refreshAll,
    triggerManualSync,
  };
}
