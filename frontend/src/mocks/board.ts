import type {
  Account,
  AccountCashflowsResponse,
  AccountDetailResponse,
  AccountDetailSyncResponse,
  AccountEquityCurveResponse,
  AccountOrdersResponse,
  AccountPerformanceResponse,
  AccountTradesResponse,
  AuditLog,
  Binding,
  BoardEvent,
  BoardLead,
  BoardMine,
  CaptchaChallenge,
  DashboardBoard,
  ExecutionRecord,
  Lead,
  LeadSnapshot,
  LegacyPosition,
  NotificationSettings,
  OrderIntent,
  ProxyNode,
  SecurityGuard,
  SettingsResponse,
} from "@/types/api";
import { BINDING_RISK_FIELDS, DEFAULT_RESILIENCE, DEFAULT_RISK } from "@/constants/validation";

// 样例只在 VITE_MOCK=1 时读取；覆盖异常、空仓、多账户和未知数值。
const now = Date.now();
const dayStart = Math.floor((now + 8 * 3600000) / 86400000) * 86400000 - 8 * 3600000;
const todayTime = dayStart + Math.floor((now - dayStart) / 2);
function mine(overrides: Partial<BoardMine> = {}): BoardMine {
  return { bindingId: 11, bindingName: "Atlas / Paper", accountId: 1, accountName: "Paper Alpha",
    environment: "demo", bindingEnabled: true, breakerTripped: false, state: "aligned",
    qty: 0.02, entryPrice: 68420.5, pnl: 21.59, pnlPct: 1.58, expectedQty: 0.02, reason: null, ...overrides };
}
function event(id: number, action: BoardEvent["action"], offset: number, status: string, details: string | null): BoardEvent {
  return { id, time: todayTime + offset, symbol: "BTCUSDT", side: "LONG", action,
    qty: 0.2, price: 68420.5, realizedPnl: action === "CLOSE" ? 146.8 : null,
    mine: [{ bindingId: 11, accountName: "Paper Alpha", status,
      qty: status === "EXECUTED" ? 0.02 : null, price: status === "EXECUTED" ? 68425 : null,
      slippagePct: status === "EXECUTED" ? 0.0066 : null, spreadPct: status === "EXECUTED" ? 0.0012 : null, details }] };
}
export const mockBoard: DashboardBoard = {
  serverTime: now, dayStart, globalKillSwitch: false,
  alerts: [
    { level: "critical", code: "ORPHAN_POSITION", message: "ORPHAN_POSITION", bindingId: 12, leadId: 1, accountId: 2 },
    { level: "warning", code: "LEAD_STALE", message: "LEAD_STALE", bindingId: null, leadId: 2, accountId: null },
    { level: "info", code: "WAITING_EXECUTION", message: "WAITING_EXECUTION", bindingId: 11, leadId: 1, accountId: 1 },
  ],
  accounts: [
    { id: 1, name: "Paper Alpha", exchange: "okx", environment: "demo", status: "connected",
      equity: 10426.5, available: 8602.4, unrealizedPnl: 126.5, todayPnl: 226.5, todayPnlPct: 2.22,
      lastSyncAt: now - 4000, riskLegacyUnverified: false, ownerUserId: 1 },
    { id: 2, name: "Main Futures", exchange: "binance", environment: "live", status: "connected",
      equity: 24782.6, available: 20190.3, unrealizedPnl: -82.7, todayPnl: -217.4, todayPnlPct: -0.87,
      lastSyncAt: now - 7000, riskLegacyUnverified: true, ownerUserId: 1 },
    { id: 3, name: "Reserve Paper", exchange: "okx", environment: "demo", status: "error",
      equity: 5020, available: null, unrealizedPnl: null, todayPnl: null, todayPnlPct: null,
      lastSyncAt: now - 180000, riskLegacyUnverified: false, ownerUserId: 2 },
    { id: 4, name: "Awaiting Sync", exchange: "binance", environment: "demo", status: "pending",
      equity: null, available: null, unrealizedPnl: null, todayPnl: null, todayPnlPct: null,
      lastSyncAt: null, riskLegacyUnverified: null, ownerUserId: 2 },
  ],
  leads: [
    { id: 1, name: "Atlas Momentum", avatarUrl: null, status: "healthy", enabled: true,
      stale: false, snapshotAgeSec: 5, positionShow: true, positionsConfirmed: true, boundCount: 2, activeBoundCount: 1,
      todayOpenCount: 3, todayCloseCount: 1,
      todayRealizedPnl: 146.8,
      orphans: [{ ...mine({ bindingId: 12, accountId: 2, accountName: "Main Futures", environment: "live",
        state: "blocked", qty: 120, entryPrice: 0.452, pnl: -8.4, expectedQty: 0, reason: "ORPHAN_POSITION" }), symbol: "ADAUSDT", side: "LONG" }],
      positions: [
        { key: "BTCUSDT:LONG", symbol: "BTCUSDT", side: "LONG", qty: 0.2, entryPrice: 68420.5, markPrice: 69500,
          pnl: 215.9, pnlPct: 1.58, leverage: 3, notional: 13900, openedAt: todayTime, openedToday: true,
          takeProfitPrice: 72100, stopLossPrice: 67200, mine: [mine(), mine({ bindingId: 12, bindingName: "Atlas / Live",
            accountId: 2, accountName: "Main Futures", environment: "live", state: "partial", qty: 0.01, expectedQty: 0.04,
            entryPrice: 68438, pnl: 10.62, reason: "INSUFFICIENT_MARGIN" })] },
        { key: "ETHUSDT:SHORT", symbol: "ETHUSDT", side: "SHORT", qty: 4, entryPrice: 3698.1, markPrice: 3721.6,
          pnl: -94, pnlPct: -0.64, leverage: 2, notional: 14886.4, openedAt: todayTime - 1000, openedToday: true,
          takeProfitPrice: 3500, stopLossPrice: 3790, mine: [mine({ state: "blocked", breakerTripped: true,
            qty: 0, entryPrice: null, pnl: null, pnlPct: null, expectedQty: 0.4, reason: "CIRCUIT_BREAKER" })] },
        { key: "SOLUSDT:LONG", symbol: "SOLUSDT", side: "LONG", qty: 20, entryPrice: 167.8, markPrice: 170.35,
          pnl: 51, pnlPct: 1.52, leverage: 2, notional: 3407, openedAt: now - 86400000, openedToday: false,
          takeProfitPrice: null, stopLossPrice: 162, mine: [mine({ state: "missing", qty: 0, entryPrice: null,
            pnl: null, pnlPct: null, expectedQty: 2, reason: "SYMBOL_BLACKLIST" })] },
        { key: "BTCUSDC:LONG", symbol: "BTCUSDC", side: "LONG", qty: 0.0000012, entryPrice: 68000, markPrice: null,
          pnl: null, pnlPct: null, leverage: null, notional: null, openedAt: null, openedToday: false,
          takeProfitPrice: null, stopLossPrice: null, mine: [mine({ state: "unknown", qty: null,
            entryPrice: null, pnl: null, pnlPct: null, expectedQty: null, reason: "ACCOUNT_STALE" })] },
      ],
      todayEvents: [
        event(104, "OPEN", 3000, "EXECUTED", null), event(103, "ADD", 2000, "FAILED", "INSUFFICIENT_MARGIN"),
        event(102, "REDUCE", 1000, "SKIPPED", "BINDING_DISABLED"), event(101, "CLOSE", 0, "EXECUTED", null),
      ] },
    // 可见空仓与下一个未知仓位样例分开，过期快照仍显示独立警示。
    { id: 2, name: "Delta Range", avatarUrl: null, status: "stale", enabled: true, stale: true,
      snapshotAgeSec: 145, positionShow: true, positionsConfirmed: true, boundCount: 1, activeBoundCount: 1,
      todayOpenCount: 0, todayCloseCount: 2,
      todayRealizedPnl: -34.8, positions: [], orphans: [], todayEvents: [] },
    { id: 3, name: "New Portfolio", avatarUrl: null, status: null, enabled: null, stale: null,
      snapshotAgeSec: null, positionShow: null, positionsConfirmed: false, boundCount: 0, activeBoundCount: 0,
      todayOpenCount: null, todayCloseCount: null,
      todayRealizedPnl: null, positions: null, orphans: null, todayEvents: null },
  ],
};
const notifications: NotificationSettings = {
  display_unit: "notional",
  webhook_enabled: true, webhook_url: "", webhook_url_configured: true,
  telegram_enabled: false, telegram_bot_token: "", telegram_chat_id: "",
  wecom_enabled: false, wecom_webhook_url: "", dingtalk_enabled: false, dingtalk_webhook_url: "", dingtalk_secret: "",
  outbox_enabled: false, outbox_path: "", notify_on_execution: true, notify_on_lead_order: true,
  notify_on_risk_block: true, notify_on_security_alert: true,
};
export const mockSettings: SettingsResponse = {
  global_kill_switch: false, public_monitoring_enabled: true, risk_defaults: { ...DEFAULT_RISK },
  resilience: { ...DEFAULT_RESILIENCE }, notifications,
  security: { encryptionAlgorithm: "AES-256-GCM", passwordHashing: "Argon2id", authMode: "HttpOnly", totpEnabled: false },
};
export const mockAccounts: Account[] = (mockBoard.accounts || []).map(account => ({
  id: account.id!, name: account.name, exchange: account.exchange, environment: account.environment,
  ownerUserId: account.ownerUserId ?? null,
  proxyId: account.id === 3 ? 2 : account.id === 2 ? 1 : null,
  isEnabled: account.id === 3 ? false : true, status: account.status, totalEquity: account.equity,
  availEquity: account.available, unrealizedPnl: account.unrealizedPnl, lastSyncAt: account.lastSyncAt,
  marginMode: "cross", positionMode: "hedge", riskEquityLegacyUnverified: account.riskLegacyUnverified,
  hwmEquity: account.id === 1 ? 10800 : account.id === 2 ? 26000 : account.id === 3 ? 5020 : null,
  dayStartEquity: account.id === 1 ? 10200 : account.id === 2 ? 25000 : null,
  totalNotional: account.id === 1 ? 1368.4 : account.id === 2 ? 1417.08 : null,
  lastError: account.id === 3 ? "NETWORK_ERROR" : null,
  positions: account.id === 1 ? [
    { instId: "BTC-USDT-SWAP", posSide: "Long", pos: 0.02, avgPx: 68420.5, curPx: 69500,
      markPx: 69499, upl: 21.59, uplRatio: 0.0466, lever: 3, notionalUsd: 1390, marginUsd: 463.33,
      cTime: todayTime, uTime: now - 4000, mgnMode: "cross", priceSource: "mark" },
  ] : account.id === 2 ? [
    { instId: "BTCUSDT", posSide: "both", pos: 0.01, avgPx: 68438, curPx: 69500, upl: 10.62,
      uplRatio: 0.0306, lever: 2, notionalUsd: 695, marginUsd: 347.5, cTime: todayTime, uTime: now - 7000, mgnMode: "cross" },
    { instId: "ADAUSDT", posSide: "LONG", pos: 120, avgPx: 0.452, curPx: 0.382, upl: -8.4,
      uplRatio: -0.3665, lever: 2, notionalUsd: 45.84, marginUsd: 22.92, cTime: now - 86400000, uTime: now - 7000, mgnMode: "cross" },
  ] : account.id === 3 ? [] : null,
}));
const riskFields = Object.fromEntries(Object.entries(BINDING_RISK_FIELDS).map(([key, field]) => [field, DEFAULT_RISK[key as keyof typeof DEFAULT_RISK]])) as Pick<Binding, typeof BINDING_RISK_FIELDS[keyof typeof BINDING_RISK_FIELDS]>;
export const mockBindings = [1, 2, 3].map((accountId): Binding => ({
  ...riskFields, id: accountId + 10, name: accountId === 1 ? "Atlas / Paper" : "Atlas / Live",
  leadId: 1, leadName: "Atlas Momentum", accountId, accountName: mockAccounts[accountId - 1].name,
  accountExchange: mockAccounts[accountId - 1].exchange, accountEnvironment: mockAccounts[accountId - 1].environment,
  isEnabled: accountId === 1, riskScale: 1, symbolWhitelist: [], symbolBlacklist: ["SOLUSDT"], symbolMap: {},
  circuitBreakerTripped: accountId === 1, circuitBreakerReason: accountId === 1 ? "CIRCUIT_BREAKER" : null,
  consecutiveFailures: 1, baselineSeeded: true, adoptionCompleted: false, pendingDebtUsd: 12.4,
}));
mockBindings[2] = { ...mockBindings[2], name: "Delta / Reserve", leadId: 2, leadName: "Delta Range",
  isEnabled: true, riskScale: 0.5, circuitBreakerTripped: false, circuitBreakerReason: null,
  consecutiveFailures: 0, pendingDebtUsd: 0 };
export const mockLeads: Lead[] = (mockBoard.leads || []).map(lead => ({
  id: lead.id!, name: lead.name, portfolioId: `sample-${lead.id}`, proxyId: lead.id === 2 ? 2 : null, isEnabled: lead.enabled,
  isPublicDashboard: true, pollIntervalSec: 15, status: lead.status, lastSyncAt: lead.snapshotAgeSec == null ? null : now - lead.snapshotAgeSec * 1000,
  stale: lead.stale, snapshotAgeSec: lead.snapshotAgeSec, marginBalance: lead.id === 3 ? null : 45000,
  openPositionsCount: lead.positions?.length ?? null, openUnrealizedPnl: lead.todayRealizedPnl,
  lastTradeTime: lead.todayEvents?.[0]?.time ?? null, positionShow: lead.positionShow,
  lastError: lead.id === 2 ? "LEAD_STALE" : null,
}));
export const mockProxies: ProxyNode[] = [
  { id: 1, name: "Primary Egress", url: "http://proxy-a.invalid:8080", region: "SG", priority: 1,
    isEnabled: true, isHealthy: true, latencyMs: 82, lastCheckedAt: now - 12000, lastError: null },
  { id: 2, name: "Backup Egress", url: "http://proxy-b.invalid:8080", region: "JP", priority: 2,
    isEnabled: true, isHealthy: false, latencyMs: null, lastCheckedAt: now - 180000, lastError: "REQUEST_TIMEOUT" },
  { id: 3, name: "Unverified Egress", url: null, url_configured: true, region: null, priority: 3,
    isEnabled: false, isHealthy: null, latencyMs: null, lastCheckedAt: null, lastError: null },
];
// 1 号带单员各持仓的补充口径：占比、维持保证金、资金费率、建仓明细与 PA / SMC 结构分析。
// SOLUSDT 不带结构分析，BTCUSDC 什么都不补，分别用来展示“等待分析”和未知数值。
const sampleDetails: Record<string, Partial<LegacyPosition>> = {
  BTCUSDT: {
    entryValue: 13684.1, entryLeverage: 0.3, notionalLeverage: 0.31, sharePct: 43.2, maintMargin: 55.6, maintMarginRoePct: 0.12,
    mmr: { maxLev: 125, mmrPct: 0.4, tier: 1, exact: true }, fundingRate: 0.0001, priceDiffPct: 1.58, priceEdgePct: 1.58,
    lastAddTime: todayTime + 2000, entryOrdersCount: 2,
    entryOrders: [
      { id: "sample-entry-1", time: todayTime, dir: "OPEN_LONG", qty: 0.1, avgPrice: 68380 },
      { id: "sample-entry-2", time: todayTime + 2000, dir: "OPEN_LONG", qty: 0.1, avgPrice: 68461 },
    ],
    analysis: {
      ok: true, verdict: "Trend-aligned entry", verdictZh: "顺势占优", verdictLevel: "good",
      trend1h: "Bullish", trend1hZh: "多头", trend4h: "Bullish", trend4hZh: "多头",
      entryZone: "Discount", entryZoneZh: "折价区", scalingStyle: "Pyramiding", scalingStyleZh: "顺势加仓",
      summary: "Entered in the discount half of the 72h range with both timeframes trending up; invalidation sits below the last higher low.",
      summaryZh: "在 72 小时区间的折价区顺势开多，1H 与 4H 同向向上；失效位在前一个抬高的低点下方。",
      entryRangePct: 33.7, curRangePct: 62.3, low72h: 67150, high72h: 70920, eq72h: 69035,
      keyResistance: 70920, keySupport: 68100, invalidationLevel: 67200, target1: 70900, target2: 72100,
      rrFromEntry: 2.1, rsi1h: 58.4, rsi4h: 61.2, atr1hPct: 0.42, entryVsEma21Atr: -0.6,
      nearestBearFvg: [70350, 70620], nearestBullFvg: [68240, 68510],
    },
  },
  ETHUSDT: {
    entryValue: 14792.4, entryLeverage: 0.33, notionalLeverage: 0.33, sharePct: 46.2, maintMargin: 74.43, maintMarginRoePct: 0.17,
    mmr: { maxLev: 100, mmrPct: 0.5, tier: 1, exact: true }, fundingRate: -0.00005, priceDiffPct: 0.64, priceEdgePct: -0.64,
    entryOrdersCount: 1,
    analysis: {
      ok: true, verdict: "Neutral range play", verdictZh: "中性博弈", verdictLevel: "warn",
      trend1h: "Ranging", trend1hZh: "震荡", trend4h: "Bullish", trend4hZh: "多头",
      entryZone: "Equilibrium", entryZoneZh: "均衡区", scalingStyle: "Single entry", scalingStyleZh: "一次建仓",
      summary: "Short opened near the middle of the range against the 4h trend; price is now trading above the entry.",
      summaryZh: "在区间中部逆 4H 趋势开空，现价已高于开仓均价。",
      entryRangePct: 48.9, curRangePct: 62, low72h: 3610, high72h: 3790, eq72h: 3700,
      keyResistance: 3790, keySupport: 3610, invalidationLevel: 3790, target1: 3610, target2: 3500,
      rrFromEntry: 1.1, rsi1h: 54.1, rsi4h: 57.8, atr1hPct: 0.55, entryVsEma21Atr: 0.3,
      nearestBearFvg: [3742, 3761], nearestBullFvg: null,
    },
  },
  SOLUSDT: {
    entryValue: 3356, entryLeverage: 0.07, notionalLeverage: 0.08, sharePct: 10.6, maintMargin: 22.15, maintMarginRoePct: 0.05,
    mmr: { maxLev: 75, mmrPct: 0.65, tier: 1, exact: false }, priceDiffPct: 1.52, priceEdgePct: 1.52, entryOrdersCount: 1,
  },
};
// 遥测快照样例：1 号字段给全，用来验收每个区块；2 号是过期的公开空仓；3 号全部未知。
function snapshot(lead: BoardLead): LeadSnapshot {
  // 时间戳按调用时刻生成：长时间开着预览时，样例数据不会自己“变旧”。
  const generated = lead.snapshotAgeSec == null ? null : Date.now() - lead.snapshotAgeSec * 1000;
  const full = lead.id === 1;
  const known = lead.id !== 3;
  return {
    profile: { nickname: lead.name, avatarUrl: null, positionShow: lead.positionShow,
      lastTradeTime: lead.todayEvents?.[0]?.time ?? null, badgeName: full ? "Top Trader" : null,
      signTradFi: full ? true : known ? false : null,
      tags: full ? ["Trend following", "Low leverage", "BTC / ETH"] : known ? ["Range trading"] : null,
      description: full ? "Follows the 4h trend on majors, scales in on pullbacks and keeps account leverage under 1x." : null,
      aumAmount: known ? 980000 : null, copyAum: full ? 612400 : known ? 148000 : null, marginBalance: known ? 45000 : null,
      profitSharingRate: known ? 10 : null, copierPnl: full ? 128450.6 : known ? -3210.4 : null,
      currentCopyCount: full ? 300 : known ? 86 : null, maxCopyCount: known ? 300 : null, sharpRatio: full ? 1.7 : null },
    meta: { portfolioId: `sample-${lead.id}`, generatedAt: generated, priceUpdatedAt: generated, stale: lead.stale,
      snapshotAgeSec: lead.snapshotAgeSec, fetchMs: known ? 412 : null, orderStoreSize: full ? 1280 : known ? 342 : null },
    performance: known ? {
      "7D": { roi: 2.6, pnl: 1170, copierPnl: 3980.5, mdd: 1.4, winRate: 62, winOrders: 31, totalOrder: 50, sharpRatio: 2.1 },
      "30D": { roi: 7.9, pnl: 3555, copierPnl: 14210.3, mdd: 3.1, winRate: 60.4, winOrders: 119, totalOrder: 197, sharpRatio: 1.9 },
      "90D": { roi: -1.8, pnl: -810, copierPnl: -2964.7, mdd: 6.8, winRate: 54.2, winOrders: 289, totalOrder: 533, sharpRatio: 0.4 },
      "180D": { roi: 16.3, pnl: 7335, copierPnl: 52180.9, mdd: 8.5, winRate: 58.9, winOrders: 531, totalOrder: 902, sharpRatio: 1.3 },
      "365D": { roi: 38.5, pnl: 17325, copierPnl: 128450.6, mdd: 9.2, winRate: 61.8, winOrders: 816, totalOrder: 1320, sharpRatio: 1.7 },
    } : null,
    openPositions: lead.positions?.map(position => ({ symbol: position.symbol, positionSide: position.side, qty: position.qty,
      avgEntry: position.entryPrice, currentPrice: position.markPrice, uPnL: position.pnl, uPnLRoePct: position.pnlPct,
      markValue: position.notional, openTime: position.openedAt, ...(full ? sampleDetails[position.symbol ?? ""] : null) })) ?? null,
    openPositionsSummary: full ? { marginBalance: 45000, availableMargin: 31392.9, positionCount: 4, longCount: 3, shortCount: 1,
      totalMarkNotional: 32193.4, totalEntryNotional: 31832.5, totalNotionalLeverage: 0.72, maxPositionLeverage: 0.33,
      estMaintenanceMarginRatioPct: 0.47, marginCoveragePct: 139.8, adverseMoveBufferPct: 139.31,
      totalUnrealizedPnl: 172.9, totalMarginRoePct: 0.38, mmrSource: "binance-official-brackets", priceAsOf: generated }
      : known ? { marginBalance: 45000, availableMargin: 45000, positionCount: 0, longCount: 0, shortCount: 0,
        totalMarkNotional: 0, totalEntryNotional: 0, totalNotionalLeverage: 0, totalUnrealizedPnl: 0, priceAsOf: generated } : null,
    openPositionsMeta: { source: full ? "api" : known ? "stale-fallback" : null },
    roiChart365D: known ? Array.from({ length: 90 }, (_, index) => ({ t: now - (89 - index) * 86400000,
      v: Math.round((index * 0.43 + Math.sin(index / 5) * 4 - (index > 40 && index < 58 ? 6 : 0)) * 100) / 100 })) : null,
    // 7 个资产：前 5 个各占一种分类色，其余并入“其他”。
    allocation: full ? { updateTime: now - 3600000, items: [{ asset: "BTC", volume: 38 }, { asset: "ETH", volume: 24 }, { asset: "SOL", volume: 12 },
      { asset: "BNB", volume: 9 }, { asset: "XRP", volume: 7 }, { asset: "DOGE", volume: 6 }, { asset: "ADA", volume: 4 }] }
      : known ? { items: [{ asset: "BTC", volume: 60 }, { asset: "ETH", volume: 40 }] } : null,
    orders: lead.todayEvents ? [
      ...lead.todayEvents.map(item => ({ id: item.id, time: item.time, symbol: item.symbol,
        dir: `${item.action}_${item.side}`, qty: item.qty, avgPrice: item.price, pnl: item.realizedPnl, type: "MARKET" })),
      // 再补一笔今天的空单和一笔昨天的建仓：今日动态不全是同一个币种，且昨天的单不会混进“今日”。
      ...(full ? [
        { id: "sample-order-eth", time: todayTime - 1000, symbol: "ETHUSDT", dir: "OPEN_SHORT", qty: 4, avgPrice: 3698.1, pnl: null, type: "LIMIT" },
        { id: "sample-order-sol", time: now - 86400000, symbol: "SOLUSDT", dir: "OPEN_LONG", qty: 20, avgPrice: 167.8, pnl: null, type: "LIMIT" },
      ] : []),
    ] : null,
    closed: lead.id === 1 ? [
      { id: "sample-closed-1", symbol: "ADAUSDT", side: "LONG", opened: now - 86400000,
        closed: todayTime, avgCost: 0.452, avgClosePrice: 0.473, maxQty: 6990, closedVol: 6990, pnl: 146.8, leverage: 2 },
      { id: "sample-closed-2", symbol: "ETHUSDT", side: "SHORT", opened: now - 172800000,
        closed: now - 86400000, avgCost: 3700, avgClosePrice: 3732, maxQty: 3, closedVol: 3, pnl: -96, leverage: 2 },
    ] : lead.positions == null ? null : [],
    orderBatches: lead.id === 1 ? [
      { id: "sample-batch-1", symbol: "BTCUSDT", dir: "OPEN_LONG", startTime: todayTime, endTime: todayTime + 3000,
        totalQty: 0.4, avgPrice: 68420.5, notional: 27368.2, orderCount: 2,
        items: [{ id: "sample-batch-order-1", time: todayTime, qty: 0.2, avgPrice: 68420.5 }] },
    ] : [],
    tradeCycles: lead.id === 1 ? [
      { cycleId: "sample-cycle-1", symbol: "ADAUSDT", positionSide: "LONG", status: "CLOSED",
        avgEntryPrice: 0.452, avgExitPrice: 0.473, entryCount: 2, exitCount: 1,
        durationMs: 86400000, realizedPnl: 146.8, openedAt: now - 86400000 },
    ] : [],
  };
}
// 各状态均有多页数据，避免筛选只在当前页有效的假象。
const executionStates = ["EXECUTED", "EXECUTED", "FAILED", "SKIPPED_DISABLED", "BLOCKED_RISK", "ACCUMULATED_DEBT", "REJECTED", "SKIPPED_STALE"];
export const mockExecutions: ExecutionRecord[] = Array.from({ length: 47 }, (_, index) => ({
  id: 1000 + index, bindingId: index % 2 ? 12 : 11, bindingName: index % 2 ? "Atlas / Live" : "Atlas / Paper",
  leadId: 1, accountId: index % 2 ? 2 : 1, leadName: "Atlas Momentum", accountName: index % 2 ? "Main Futures" : "Paper Alpha",
  symbol: "BTCUSDT", instId: "BTC-USDT-SWAP", side: "buy", positionSide: "long", action: index % 2 ? "ADD" : "OPEN",
  status: executionStates[index % executionStates.length], leadQty: 0.2, leadPrice: 68420.5, leadNotional: 13684.1,
  myQty: index % executionStates.length < 2 ? 0.02 : null,
  myPrice: index % executionStates.length < 2 ? 68425 : null, myNotional: index % executionStates.length < 2 ? 1368.5 : null,
  slippagePct: index % executionStates.length < 2 ? 0.0066 : null,
  spreadPct: index % executionStates.length < 2 ? 0.0012 : null,
  orderId: index % executionStates.length < 2 ? `sample-order-${index}` : null,
  details: [null, null, "INSUFFICIENT_MARGIN", "BINDING_DISABLED", "CIRCUIT_BREAKER", "WAITING_EXECUTION", "INSUFFICIENT_MARGIN", "LEAD_STALE"][index % executionStates.length] ?? null,
  createdAt: now - index * 60000,
  environment: index % 2 ? "live" : "demo",
}));
export const mockIntents: OrderIntent[] = [
  { id: "sample-intent-1", bindingId: 12, bindingName: "Atlas / Live", accountId: 2, accountName: "Main Futures",
    instId: "ETHUSDT", posSide: "net", state: "UNKNOWN", requestedQty: 0.4, filledQty: null,
    exchangeOrderId: null, reduceOnly: false, createdAt: now - 120000, sourceEventId: "103" },
  { id: "sample-intent-2", bindingId: 11, bindingName: "Atlas / Paper", accountId: 1, accountName: "Paper Alpha",
    instId: "BTC-USDT-SWAP", posSide: "Long", state: "SUBMITTING", requestedQty: 0.02, filledQty: 0.005,
    exchangeOrderId: "sample-order-pending", reduceOnly: false, createdAt: now - 15000, sourceEventId: "104" },
];
const mockAuditLogs: AuditLog[] = [
  { id: 1, username: "preview", action: "login", resourceType: "auth", createdAt: now - 120000, details: null },
  { id: 2, username: "preview", action: "settings_update", resourceType: "settings", createdAt: now - 300000, details: "risk_defaults" },
];
const mockSecurityGuards: SecurityGuard[] = [
  { id: 1, keyType: "username", keyValue: "preview-locked", guardKey: "username:preview-locked",
    failCount: 5, isLocked: true, lockedUntil: now + 900000, lockRemainingSec: 900,
    lastUsername: "preview-locked", lastIp: "127.0.0.1", lastFailAt: now - 30000 },
];
const captcha: CaptchaChallenge = {
  captchaToken: "sample-challenge", expiresInSec: 120,
  svg: '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="48"><rect width="160" height="48" fill="white"/><text x="16" y="32" fill="black" font-size="24">3 + 5 = ?</text></svg>',
};
export const mockAuthPreview = { username: "preview", lockRemainingSec: 45, captcha, errorCode: "NETWORK_ERROR" };
// 预览模式下唯一可写的字段：带单员是否进公开看板（内存中生效，刷新即还原）；额外允许只读详情手动同步模拟。
export function mockWrite(endpoint: string, method: string, body: unknown): unknown {
  const pathname = new URL(endpoint, "http://mock.local").pathname;
  const detailSyncMatch = /^\/api\/accounts\/(\d+)\/detail\/sync$/.exec(pathname);
  if (method === "POST" && detailSyncMatch) {
    const accountId = Number(detailSyncMatch[1]);
    const res: AccountDetailSyncResponse = {
      ok: true,
      accountId,
      syncedAtMs: Date.now(),
      datasets: mockAccountDetail(accountId).dataQuality.datasets,
      results: {
        open_orders: { status: "ok", count: accountId === 2 ? 2 : 0 },
        equity: { status: "ok" },
        cashflow: { status: "ok" },
        fills: { status: "ok" },
        orders: { status: "ok" },
        force_orders: { status: "ok" },
        commission_rates: { status: "ok" },
        daily_snapshots: { status: accountId === 1 ? "unsupported" : "ok" },
      },
    };
    return structuredClone(res);
  }
  const match = /^\/api\/leads\/(\d+)$/.exec(pathname);
  const lead = match && mockLeads.find(item => item.id === Number(match[1]));
  if (method !== "PUT" || !lead || typeof body !== "string") return undefined;
  const patch = JSON.parse(body) as Record<string, unknown>;
  if (Object.keys(patch).length !== 1 || typeof patch.isPublicDashboard !== "boolean") return undefined;
  lead.isPublicDashboard = patch.isPublicDashboard;
  return structuredClone({ item: lead });
}

function mockAccountDetail(accountId: number): AccountDetailResponse {
  const base = mockAccounts.find(a => a.id === accountId) ?? mockAccounts[0];
  const isUninit = accountId === 4;
  const isStaleErr = accountId === 3;
  const isMixed = accountId === 2;
  const fetchedAt = isUninit ? null : isStaleErr ? now - 240_000 : now - 5_000;
  const covStart = now - 30 * 86_400_000;
  return {
    account: {
      id: accountId,
      ownerUserId: base.ownerUserId ?? 1,
      name: base.name ?? `Account #${accountId}`,
      exchange: base.exchange ?? "binance",
      environment: (base.environment as Account["environment"]) ?? "live",
      apiKeyMasked: "****4k9x",
      isEnabled: base.isEnabled ?? true,
      marginMode: "cross",
      positionMode: "hedge",
      clockOffsetMs: isStaleErr ? 1850 : -24,
      status: base.status ?? "connected",
      lastSyncAt: fetchedAt ? new Date(fetchedAt).toISOString() : null,
      lastSyncAtMs: fetchedAt,
      lastError: base.lastError ?? null,
      readOnly: true,
    },
    risk: {
      currency: "USDT",
      riskLevel: isUninit ? "unknown" : isMixed ? "warning" : isStaleErr ? "critical" : "normal",
      equity: isUninit ? null : (base.totalEquity ?? 24782.6),
      walletBalance: isUninit ? null : (isMixed ? 24865.3 : 10300),
      availableBalance: isUninit ? null : (base.availEquity ?? 20190.3),
      unrealizedPnl: isUninit ? null : (base.unrealizedPnl ?? -82.7),
      hwmEquity: isUninit ? null : 25400,
      drawdownPct: isUninit ? null : (isMixed ? 2.43 : 0),
      dayStartEquity: isUninit ? null : (isMixed ? 25000 : 10200),
      todayNetFlow: isUninit ? null : (isMixed ? 200 : 0),
      todayPnl: isUninit || isStaleErr ? null : (isMixed ? -417.4 : 226.5),
      totalNotional: isUninit ? null : (isMixed ? 18420 : 1390),
      longNotional: isUninit ? null : (isMixed ? 14220 : 1390),
      shortNotional: isUninit ? null : (isMixed ? 4200 : 0),
      netNotional: isUninit ? null : (isMixed ? 10020 : 1390),
      allocatedNotional: isUninit ? null : (isMixed ? 14220 : 1390),
      unallocatedNotional: isUninit ? null : (isMixed ? 4200 : 0),
      maxSinglePositionSharePct: isUninit ? null : (isMixed ? 77.2 : 100),
      initialMargin: isUninit ? null : (isMixed ? 3892.3 : 463.3),
      initialMarginRatio: isUninit ? null : 0.157,
      initialMarginRatioPct: isUninit ? null : 15.7,
      maintenanceMargin: isUninit ? null : (isMixed ? 194.6 : 12.4),
      marginRatio: isUninit ? null : (isMixed ? 0.52 : 0.12),
      marginRatioPct: isUninit ? null : (isMixed ? 52.0 : 12.0),
      marginRatioSource: isUninit ? null : "derived",
      effectiveLeverage: isUninit ? null : (isMixed ? 0.74 : 0.13),
      closestLiquidationDistancePct: isUninit ? null : (isMixed ? 8.4 : 42.1),
      closestLiquidationSymbol: isUninit ? null : "SOLUSDT",
      positionCount: isUninit ? null : (isMixed ? 2 : 1),
      openOrderCount: isUninit ? null : (isMixed ? 2 : 1),
      canTrade: isUninit ? null : !isStaleErr,
      multiAssetsMargin: false,
      dualSidePosition: true,
      otherAssets: isMixed ? [{ currency: "USDC", equity: 320.5, available: 320.5, walletBalance: 320.5, unrealizedPnl: 0 }] : [],
      snapshotAsOfMs: fetchedAt,
      snapshotQuality: isUninit ? null : (isStaleErr ? "partial" : "ok"),
      dataQuality: { status: isUninit ? "unavailable" : isStaleErr ? "stale" : "ok", fetchedAt, coverageStart: covStart, coverageEnd: fetchedAt },
    },
    pnl: {
      currency: "USDT",
      range: "30d",
      startMs: covStart,
      endMs: fetchedAt,
      cashflowStatus: isUninit ? "unavailable" : "ok",
      cashflowCoverageComplete: !isUninit && !isStaleErr,
      cashflowCoverageStartMs: isUninit ? null : covStart,
      fillsStatus: isUninit ? "unavailable" : "ok",
      fillsCoverageComplete: !isUninit,
      startEquity: isUninit ? null : 21500,
      endEquity: isUninit ? null : (base.totalEquity ?? 24782.6),
      totalEquityChange: isUninit ? null : 3282.6,
      unrealizedPnlChange: isUninit ? null : -82.7,
      deposits: isUninit ? null : 2000,
      withdrawals: isUninit ? null : 500,
      netTransfers: isUninit ? null : 1500,
      nonTradingExternal: isUninit ? null : 25,
      tradingNetPnl: isUninit ? null : 1757.6,
      tradingPnl: isUninit ? null : 1757.6,
      ledgerNetPnl: isUninit ? null : 1757.4,
      ledger: {
        realizedPnl: isUninit ? null : 1912.5,
        fundingFee: isUninit ? null : -34.1,
        commission: isUninit ? null : -42.3,
        commissionRebate: isUninit ? null : 4.0,
        liquidationFee: isUninit ? null : 0,
        insuranceClear: isUninit ? null : 0,
        bonus: isUninit ? null : 25,
        referralRebate: isUninit ? null : 0,
        otherIncome: isUninit ? null : 0,
        otherExternalFlow: isUninit ? null : 0,
        unknownFlow: isUninit ? null : 0,
      },
      reconciled: isUninit ? null : true,
      reconciliationDelta: isUninit ? null : 0.2,
      attribution: {
        system: { fillCount: isUninit ? 0 : 12, grossRealizedPnlUsdt: isUninit ? null : 1680, commissionUsdt: isUninit ? null : 31.2, netPnlUsdt: isUninit ? null : 1648.8 },
        manual: { fillCount: isUninit ? 0 : 3, grossRealizedPnlUsdt: isUninit ? null : 232.5, commissionUsdt: isUninit ? null : 11.1, netPnlUsdt: isUninit ? null : 221.4 },
        unknown: { fillCount: 0, grossRealizedPnlUsdt: isUninit ? null : 0, commissionUsdt: isUninit ? null : 0, netPnlUsdt: isUninit ? null : 0 },
      },
      bySymbol: isUninit ? [] : [
        { symbol: "BTCUSDT", fillCount: 10, realizedPnlUsdt: 1680, commissionUsdt: 31.2, fundingFeeUsdt: -21.0, netPnlUsdt: 1627.8 },
        { symbol: "SOLUSDT", fillCount: 5, realizedPnlUsdt: 232.5, commissionUsdt: 11.1, fundingFeeUsdt: -13.1, netPnlUsdt: 208.3 },
      ],
      byBinding: isUninit ? [] : [{ bindingId: 12, bindingName: "Atlas / Live", fillCount: 12, realizedPnlUsdt: 1680, commissionUsdt: 31.2, netPnlUsdt: 1648.8 }],
      nonUsdtFillCommissions: isMixed ? { BNB: 0.012 } : {},
      otherAssetCashflows: [],
      dataQuality: { status: isUninit ? "unavailable" : "ok", fetchedAt, coverageStart: covStart, coverageEnd: fetchedAt },
    },
    positions: {
      positionsKnown: !isUninit,
      allocatedNotionalUsdt: isUninit ? null : (isMixed ? 14220 : 1390),
      unallocatedNotionalUsdt: isUninit ? null : (isMixed ? 4200 : 0),
      orphanAllocations: [],
      dataQuality: { status: isUninit ? "unavailable" : isStaleErr ? "stale" : "ok", fetchedAt },
      items: isUninit ? [] : [
        {
          instId: "BTCUSDT", symbol: "BTCUSDT", currency: "USDT", posSide: "long", rawPosSide: "LONG", mgnMode: "cross",
          qty: isMixed ? 0.2 : 0.02, availQty: isMixed ? 0.2 : 0.02, avgPx: 68420.5, curPx: 71100,
          breakEvenPx: 68490, liquidationPx: 54120, liqDistancePct: 23.88, upl: 535.9, uplRatioPct: 12.1,
          notionalUsd: isMixed ? 14220 : 1390, initialMargin: 2844, maintenanceMargin: 71.1, marginRatio: 0.08,
          lever: 5, adlQuantile: 2, ownership: "system", unallocatedClass: "none",
          allocatedQty: isMixed ? 0.2 : 0.02, unallocatedQty: 0, overAllocatedQty: 0, requiresProtection: true,
          allocations: [{ allocationId: 1, bindingId: isMixed ? 12 : 11, bindingName: isMixed ? "Atlas / Live" : "Atlas / Paper", leadId: 1, leadName: "Atlas Momentum", ownedQty: isMixed ? 0.2 : 0.02, expectedQty: isMixed ? 0.2 : 0.02, entryCost: 13684, cycleRatio: 1, generation: 2, openedAtMs: now - 3 * 3_600_000 }],
          protection: { status: "protected", hasTakeProfit: true, hasStopLoss: true, hasTrailingStop: false, hasSoftwareGuard: true, slCoveredQty: isMixed ? 0.2 : 0.02, slCoveragePct: 100, nearestSlPrice: 67200, tpCoveredQty: isMixed ? 0.2 : 0.02, tpCoveragePct: 100, nearestTpPrice: 73500, overCovered: false, protectiveOrderCount: 2, orders: [] },
        },
        ...(isMixed ? [{
          instId: "SOLUSDT", symbol: "SOLUSDT", currency: "USDT", posSide: "short", rawPosSide: "SHORT", mgnMode: "isolated",
          qty: 24.5, availQty: 24.5, avgPx: 171.4, curPx: 171.43, breakEvenPx: 171.3, liquidationPx: 185.83, liqDistancePct: 8.4,
          upl: -18.4, uplRatioPct: -2.1, notionalUsd: 4200, initialMargin: 840, maintenanceMargin: 42, marginRatio: 0.52,
          lever: 5, adlQuantile: 4, ownership: "manual" as const, unallocatedClass: "manual" as const,
          allocatedQty: 0, unallocatedQty: 24.5, overAllocatedQty: 0, requiresProtection: false,
          allocations: [],
          protection: { status: "unprotected" as const, hasTakeProfit: false, hasStopLoss: false, hasTrailingStop: false, hasSoftwareGuard: false, slCoveredQty: 0, slCoveragePct: 0, nearestSlPrice: null, tpCoveredQty: 0, tpCoveragePct: 0, nearestTpPrice: null, overCovered: false, protectiveOrderCount: 0, orders: [] },
        }] : []),
      ],
    },
    openOrdersSummary: {
      openOrdersKnown: !isUninit && !isStaleErr,
      openOrdersStale: isStaleErr,
      fetchedAtMs: fetchedAt,
      openOrdersStatus: isUninit ? "unavailable" : isStaleErr ? "stale" : "ok",
      unprotectedCount: isUninit || isStaleErr ? null : (isMixed ? 1 : 0),
      partialProtectedCount: isUninit || isStaleErr ? null : 0,
      totalOpenOrders: isUninit ? null : (isMixed ? 2 : 1),
      byCategory: isUninit ? {} : { stop_loss: 1, take_profit: isMixed ? 1 : 0 },
      dataQuality: { status: isUninit ? "unavailable" : isStaleErr ? "stale" : "ok", fetchedAt },
    },
    bindings: {
      items: mockBindings.filter(b => b.accountId === accountId).map(b => ({
        id: b.id, name: b.name ?? "", leadId: b.leadId ?? 0, leadName: b.leadName ?? "", isEnabled: Boolean(b.isEnabled),
        riskScale: b.riskScale ?? 1, orderMode: b.orderMode ?? "ioc_then_market", tpSlMode: b.tpSlMode ?? "follow_lead",
        circuitBreakerTripped: Boolean(b.circuitBreakerTripped), circuitBreakerReason: b.circuitBreakerReason ?? null,
        consecutiveFailures: b.consecutiveFailures ?? 0, allocatedPositionsCount: 1, allocatedNotionalUsd: isMixed ? 14220 : 1390,
        periodNetPnlUsdt: isUninit ? null : 1648.8, periodFillCount: isUninit ? 0 : 12,
      })),
      dataQuality: { status: "ok", fetchedAt: now },
    },
    alerts: {
      active: isMixed ? [
        { code: "LIQUIDATION_PROXIMITY", scopeKey: "SOLUSDT:short", severity: "warning", messageKey: "accountDetail.alert.liqProximityWarning", params: { symbol: "SOLUSDT", pct: 8.4 }, firstSeenAtMs: now - 300_000, lastSeenAtMs: now - 5_000 },
        { code: "POSITION_UNPROTECTED", scopeKey: "SOLUSDT:short", severity: "warning", messageKey: "accountDetail.alert.positionUnprotected", params: { symbol: "SOLUSDT", side: "short" }, firstSeenAtMs: now - 300_000, lastSeenAtMs: now - 5_000 },
      ] : isUninit ? [
        { code: "ACCOUNT_SYNC_STALE", scopeKey: "", severity: "warning", messageKey: "accountDetail.alert.neverSynced", params: {} },
      ] : [],
      recentResolved: [],
      criticalCount: 0,
      warningCount: isMixed ? 2 : isUninit ? 1 : 0,
      infoCount: 0,
      dataQuality: { status: "ok", fetchedAt: now },
    },
    dataQuality: {
      overallStatus: isUninit ? "degraded" : isMixed ? "partial" : "ok",
      accountSyncedAtMs: fetchedAt,
      accountSyncStale: isUninit || isStaleErr,
      openOrdersFetchedAtMs: fetchedAt,
      openOrdersComplete: !isUninit && !isStaleErr,
      datasets: {
        equity: { dataset: "equity", status: isUninit ? "unavailable" : isStaleErr ? "stale" : "ok", coverageStartMs: covStart, coverageEndMs: fetchedAt, lastSuccessAtMs: fetchedAt, recordCount: isUninit ? 0 : 120 },
        open_orders: { dataset: "open_orders", status: isUninit ? "unavailable" : isStaleErr ? "stale" : "ok", lastSuccessAtMs: fetchedAt, recordCount: isUninit ? 0 : 2 },
        cashflow: { dataset: "cashflow", status: isUninit ? "unavailable" : "ok", coverageStartMs: covStart, coverageEndMs: fetchedAt, lastSuccessAtMs: fetchedAt, recordCount: isUninit ? 0 : 18 },
        fills: { dataset: "fills", status: isUninit ? "unavailable" : "ok", coverageStartMs: covStart, coverageEndMs: fetchedAt, lastSuccessAtMs: fetchedAt, recordCount: isUninit ? 0 : 15 },
        orders: { dataset: "orders", status: isUninit ? "unavailable" : "ok", coverageStartMs: covStart, coverageEndMs: fetchedAt, lastSuccessAtMs: fetchedAt, recordCount: isUninit ? 0 : 18 },
        force_orders: { dataset: "force_orders", status: isUninit ? "unavailable" : "ok", coverageStartMs: covStart, coverageEndMs: fetchedAt, lastSuccessAtMs: fetchedAt, recordCount: 0 },
        commission_rates: { dataset: "commission_rates", status: isUninit ? "unavailable" : "ok", lastSuccessAtMs: fetchedAt, recordCount: 2 },
        daily_snapshots: { dataset: "daily_snapshots", status: accountId === 1 ? "unsupported" : "ok", errorMessage: accountId === 1 ? "Demo mode" : null, recordCount: accountId === 1 ? 0 : 30 },
      },
    },
  };
}

function mockAccountEquity(accountId: number, range: string): AccountEquityCurveResponse {
  if (accountId === 4) {
    return {
      currency: "USDT", range, resolution: "raw", cashflowCovered: false, cashflowCoverageStartMs: null,
      points: [], transfers: [], currentDrawdownPct: null, maxDrawdownPct: null,
      summary: { pointCount: 0, startEquity: null, endEquity: null, peakEquity: null, twrPct: null, currentDrawdownPct: null, maxDrawdownPct: null },
      dataQuality: { status: "unavailable" }, cashflowQuality: { status: "unavailable" },
    };
  }
  const vals = [21500, 21950, 21200, 23200, 23680, 24100, 24782.6];
  const stepMs = range === "24h" ? 3_600_000 : 86_400_000;
  const points = vals.map((eq, idx) => ({
    tsMs: now - (vals.length - 1 - idx) * stepMs,
    bucketStartMs: now - (vals.length - 1 - idx) * stepMs,
    resolution: range === "24h" ? "raw" : idx === 0 ? "day" : "hour",
    source: idx === 0 ? "exchange_daily" : "account_sync",
    dataQuality: idx === 0 ? "partial" : "ok",
    gapBefore: idx === 4,
    equity: eq,
    equityMin: eq - 80,
    equityMax: eq + 95,
    cumulativeTradingPnl: eq - 21500 - (idx >= 3 ? 1500 : 0),
    twrIndex: [1, 1.021, 0.986, 0.986, 1.006, 1.024, 1.053][idx],
    twrPct: [0, 2.1, -1.4, -1.4, 0.6, 2.4, 5.3][idx],
    drawdownPct: [0, 0, 3.43, 3.43, 1.47, 0, 0][idx],
    walletBalance: eq - 20, availableBalance: eq * 0.8, unrealizedPnl: 20,
    totalNotional: 18420, marginRatio: 0.14, effectiveLeverage: 0.74,
  }));
  return {
    currency: "USDT", range, resolution: range === "24h" ? "raw" : "hour",
    cashflowCovered: true, cashflowCoverageStartMs: now - 30 * 86_400_000,
    points,
    transfers: [{ id: 1, tsMs: points[3].tsMs - 10_000, category: "TRANSFER_IN", signedAmount: 2000, asset: "USDT" }],
    currentDrawdownPct: 0, maxDrawdownPct: 3.43,
    summary: { pointCount: points.length, startEquity: 21500, endEquity: 24782.6, peakEquity: 24782.6, twrPct: 5.3, currentDrawdownPct: 0, maxDrawdownPct: 3.43 },
    dataQuality: { status: "ok", fetchedAt: now - 5000 },
    cashflowQuality: { status: "ok", fetchedAt: now - 5000 },
  };
}
export function mockResponse(endpoint: string): unknown {
  const url = new URL(endpoint, "http://mock.local");
  let value: unknown;
  if (url.pathname === "/api/auth/status") value = { initialized: true, authenticated: true, user: { id: 1, username: "preview", role: "admin", totpEnabled: false } };
  else if (url.pathname === "/api/auth/captcha") value = captcha;
  else if (url.pathname === "/api/dashboard/board") value = mockBoard;
  else if (url.pathname === "/api/dashboard/intents") value = { items: mockIntents };
  else if (url.pathname === "/api/dashboard/executions") {
    const byBinding = mockExecutions.filter(item => !url.searchParams.get("binding_id") || String(item.bindingId) === url.searchParams.get("binding_id"));
    const isSkipped = (st: string) => st.startsWith("SKIPPED");
    const isSuccess = (st: string) => ["EXECUTED", "FILLED", "SUCCESS", "ADOPTED_POSITION"].includes(st);
    const isFailed = (st: string) => ["FAILED", "REJECTED", "UNFILLED"].includes(st);
    const isBlocked = (st: string) => st.startsWith("BLOCKED");
    const counts = {
      all: byBinding.length,
      actionable: byBinding.filter(item => !isSkipped((item.status ?? "").toUpperCase())).length,
      success: byBinding.filter(item => isSuccess((item.status ?? "").toUpperCase())).length,
      failed: byBinding.filter(item => isFailed((item.status ?? "").toUpperCase())).length,
      skipped: byBinding.filter(item => isSkipped((item.status ?? "").toUpperCase())).length,
      blocked: byBinding.filter(item => isBlocked((item.status ?? "").toUpperCase())).length,
    };
    const statusParam = url.searchParams.get("status");
    const groupParam = url.searchParams.get("status_group");
    const filtered = byBinding.filter(item => {
      const st = (item.status ?? "").toUpperCase();
      if (statusParam) return statusParam.split(",").includes(item.status ?? "");
      if (groupParam === "actionable") return !isSkipped(st);
      if (groupParam === "success") return isSuccess(st);
      if (groupParam === "failed") return isFailed(st);
      if (groupParam === "skipped") return isSkipped(st);
      if (groupParam === "blocked") return isBlocked(st);
      return true;
    });
    const offset = Number(url.searchParams.get("offset") || 0);
    value = {
      items: filtered.slice(offset, offset + Number(url.searchParams.get("limit") || 20)),
      total: filtered.length,
      counts,
    };
  } else if (url.pathname === "/api/accounts") value = { items: mockAccounts };
  else if (/^\/api\/accounts\/(\d+)\/detail$/.test(url.pathname)) {
    const id = Number(url.pathname.split("/")[3]);
    value = mockAccountDetail(id);
  } else if (/^\/api\/accounts\/(\d+)\/equity$/.test(url.pathname)) {
    const id = Number(url.pathname.split("/")[3]);
    value = mockAccountEquity(id, url.searchParams.get("range") || "30d");
  } else if (/^\/api\/accounts\/(\d+)\/cashflows$/.test(url.pathname)) {
    const id = Number(url.pathname.split("/")[3]);
    const cat = url.searchParams.get("category") || "";
    const sym = (url.searchParams.get("symbol") || "").toUpperCase();
    const rows: AccountCashflowsResponse["items"] = id === 4 ? [] : [
      { id: 3, exchange: "binance", externalId: "tx-103", asset: "USDT", rawType: "REALIZED_PNL", category: "REALIZED_PNL", signedAmount: 420.5, symbol: "BTCUSDT", occurredAtMs: now - 3600_000, source: "exchange_income" },
      { id: 2, exchange: "binance", externalId: "tx-102", asset: "USDT", rawType: "FUNDING_FEE", category: "FUNDING_FEE", signedAmount: -8.4, symbol: "SOLUSDT", occurredAtMs: now - 7200_000, source: "exchange_income" },
      { id: 1, exchange: "binance", externalId: "tx-101", asset: "USDT", rawType: "TRANSFER", category: "TRANSFER_IN", signedAmount: 2000, symbol: null, occurredAtMs: now - 86_400_000, source: "exchange_income" },
    ];
    const filtered = rows.filter(r => (!cat || r.category === cat) && (!sym || r.symbol === sym));
    value = { items: filtered, nextCursor: null, hasMore: false, dataQuality: { status: id === 4 ? "unavailable" : "ok", fetchedAt: now - 5000 } } satisfies AccountCashflowsResponse;
  } else if (/^\/api\/accounts\/(\d+)\/trades$/.test(url.pathname)) {
    const id = Number(url.pathname.split("/")[3]);
    const rt = url.searchParams.get("recordType") || "";
    const src = url.searchParams.get("source") || "";
    const rows: AccountTradesResponse["items"] = id === 4 ? [] : [
      { recordType: "fill", id: 20, occurredAtMs: now - 1800_000, symbol: "BTCUSDT", instId: "BTCUSDT", side: "sell", positionSide: "long", qty: 0.1, price: 70500, quoteQty: 7050, realizedPnl: 420.5, fee: 3.1, feeAsset: "USDT", maker: false, status: "FILLED", sourceClass: "system", attributionConfidence: "exact", bindingId: 12, bindingName: "Atlas / Live", executionId: 91, exchangeOrderId: "ord-20", exchangeFillId: "fill-20" },
      { recordType: "execution", id: 21, occurredAtMs: now - 2400_000, symbol: "ETHUSDT", instId: "ETHUSDT", side: "buy", positionSide: "short", action: "CLOSE", qty: 0, price: 3720, status: "BLOCKED_SLIPPAGE", details: "Slippage 0.92% > 0.50%", sourceClass: "system", attributionConfidence: "exact", bindingId: 12, bindingName: "Atlas / Live", slippagePct: 0.92, spreadPct: 0.04 },
      { recordType: "order", id: 22, occurredAtMs: now - 3600_000, symbol: "SOLUSDT", instId: "SOLUSDT", side: "sell", positionSide: "short", category: "ordinary", orderType: "LIMIT", qty: 24.5, filledQty: 24.5, price: 171.4, status: "FILLED", sourceClass: "manual", exchangeOrderId: "ord-22" },
    ];
    const filtered = rows.filter(r => (!rt || r.recordType === rt) && (!src || r.sourceClass === src));
    value = { items: filtered, nextCursor: null, hasMore: false, dataQuality: { status: id === 4 ? "unavailable" : "ok" }, ordersQuality: { status: id === 4 ? "unavailable" : "ok" } } satisfies AccountTradesResponse;
  } else if (/^\/api\/accounts\/(\d+)\/orders$/.test(url.pathname)) {
    const id = Number(url.pathname.split("/")[3]);
    const st = url.searchParams.get("state") || "open";
    if (st === "open") {
      value = {
        state: "open", openOrdersKnown: id !== 4 && id !== 3, openOrdersStale: id === 3, fetchedAtMs: id === 4 ? null : now - 5000,
        items: id === 4 ? [] : [
          { orderId: "oo-1", algoId: "alg-1", category: "stop_loss", symbol: "BTCUSDT", instId: "BTCUSDT", side: "sell", positionSide: "long", orderType: "STOP_MARKET", status: "NEW", qty: 0.2, triggerPrice: 67200, reduceOnly: true, closePosition: false, sourceClass: "system", bindingId: 12, bindingName: "Atlas / Live" },
          { orderId: "oo-2", algoId: "alg-2", category: "take_profit", symbol: "BTCUSDT", instId: "BTCUSDT", side: "sell", positionSide: "long", orderType: "TAKE_PROFIT_MARKET", status: "NEW", qty: 0.2, triggerPrice: 73500, reduceOnly: true, closePosition: false, sourceClass: "system", bindingId: 12, bindingName: "Atlas / Live" },
        ],
        nextCursor: null, hasMore: false, dataQuality: { status: id === 4 ? "unavailable" : id === 3 ? "stale" : "ok", fetchedAt: id === 4 ? null : now - 5000 },
      } satisfies AccountOrdersResponse;
    } else {
      value = { state: "history", items: [], nextCursor: null, hasMore: false, dataQuality: { status: id === 4 ? "unavailable" : "ok" } } satisfies AccountOrdersResponse;
    }
  } else if (/^\/api\/accounts\/(\d+)\/performance$/.test(url.pathname)) {
    const id = Number(url.pathname.split("/")[3]);
    value = {
      range: url.searchParams.get("range") || "90d",
      fillsStatus: id === 4 ? "unavailable" : "ok", fillsCoverageStartMs: id === 4 ? null : now - 30 * 86_400_000, fillsCoverageEndMs: id === 4 ? null : now, ordersStatus: id === 4 ? "unavailable" : "ok",
      tradeSummary: { fillCount: id === 4 ? 0 : 15, volumeUsd: id === 4 ? null : 142500, grossRealizedPnlUsdt: id === 4 ? null : 1912.5, commissionUsdt: id === 4 ? null : 42.3, nonUsdtCommissions: {}, netRealizedPnlUsdt: id === 4 ? null : 1870.2, makerCount: id === 4 ? 0 : 6, takerCount: id === 4 ? 0 : 9, makerRatioPct: id === 4 ? null : 40, closedCycleCount: id === 4 ? 0 : 8, incompleteCycleCount: id === 4 ? 0 : 1, winCount: id === 4 ? 0 : 6, lossCount: id === 4 ? 0 : 2, breakevenCount: 0, winRatePct: id === 4 ? null : 75, payoffRatio: id === 4 ? null : 2.1, profitFactor: id === 4 ? null : 4.2, expectancyUsdt: id === 4 ? null : 233.78, avgWinUsdt: id === 4 ? null : 362.5, avgLossUsdt: id === 4 ? null : 172.6, largestWinUsdt: id === 4 ? null : 740, largestLossUsdt: id === 4 ? null : -210, avgHoldingDurationMs: id === 4 ? null : 14_400_000 },
      executionQuality: { totalExecutions: id === 4 ? 0 : 14, executedCount: id === 4 ? 0 : 11, failedCount: id === 4 ? 0 : 1, blockedCount: id === 4 ? 0 : 2, skippedCount: 0, successRatePct: id === 4 ? null : 78.57, avgSlippagePct: id === 4 ? null : 0.042, medianSlippagePct: id === 4 ? null : 0.031, p95SlippagePct: id === 4 ? null : 0.11, maxSlippagePct: id === 4 ? null : 0.14, avgSpreadPct: id === 4 ? null : 0.018, spreadSampleCount: id === 4 ? 0 : 11, avgLatencyMs: id === 4 ? null : 480, p95LatencyMs: id === 4 ? null : 920, exactLatencyCount: id === 4 ? 0 : 9, estimatedLatencyCount: id === 4 ? 0 : 2, statusCounts: id === 4 ? {} : { EXECUTED: 11, BLOCKED_SLIPPAGE: 2, FAILED: 1 } },
      commissionRates: id === 4 ? [] : [{ symbol: "BTCUSDT", instId: "BTCUSDT", makerRate: 0.0002, takerRate: 0.0005 }],
      bySymbol: [], dataQuality: { status: id === 4 ? "unavailable" : "ok" },
    } satisfies AccountPerformanceResponse;
  }
  else if (url.pathname === "/api/leads") value = { items: mockLeads };
  else if (/\/api\/leads\/\d+\/snapshot$/.test(url.pathname)) {
    const lead = mockBoard.leads?.find(item => item.id === Number(url.pathname.split("/")[3]));
    value = { snapshot: lead ? snapshot(lead) : null };
  } else if (url.pathname === "/api/bindings") value = { items: mockBindings, globalKillSwitch: false };
  else if (url.pathname === "/api/settings") value = mockSettings;
  else if (url.pathname === "/api/dashboard/public") value = { enabled: true, leads: mockLeads.map((lead, index) => ({ ...lead, snapshot: snapshot(mockBoard.leads![index]) })).filter(lead => lead.isPublicDashboard === true) };
  else if (url.pathname === "/api/proxies") value = { items: mockProxies };
  else if (url.pathname === "/api/settings/audit-logs") value = { items: mockAuditLogs };
  else if (url.pathname === "/api/settings/security-guards") value = { items: mockSecurityGuards };
  else if (url.pathname === "/api/notifications/subscriptions") value = {
    items: mockLeads.map((lead, idx) => ({
      leadId: lead.id,
      leadName: lead.name,
      leadPlatform: lead.platform,
      hasBinding: idx < 2,
      isEnabled: idx === 0,
    })),
  };
  else if (url.pathname === "/api/admin/users") value = {
    items: [
      { id: 1, username: "preview", role: "admin", isActive: true, createdAt: now - 864000000, lastLoginAt: now - 120000, accountCount: 4, bindingCount: 2, enabledBindingCount: 1, openPositionCount: 3, totalEquity: 40229.1 },
      { id: 2, username: "trader_demo", role: "user", isActive: true, createdAt: now - 432000000, lastLoginAt: now - 3600000, accountCount: 1, bindingCount: 1, enabledBindingCount: 0, openPositionCount: 0, totalEquity: 1200 },
    ],
  };
  else if (/^\/api\/admin\/users\/\d+$/.test(url.pathname)) value = {
    user: { id: 1, username: "preview", role: "admin", isActive: true, createdAt: now - 864000000, lastLoginAt: now - 120000, accountCount: 4, bindingCount: 2, enabledBindingCount: 1, openPositionCount: 3, totalEquity: 40229.1 },
    accounts: mockAccounts.map(a => ({ id: a.id, name: a.name, exchange: a.exchange, environment: a.environment, status: a.status, isEnabled: a.isEnabled, equity: a.totalEquity })),
    bindings: mockBindings.map(b => ({ id: b.id, name: b.name, leadId: b.leadId, leadName: b.leadName, accountId: b.accountId, accountName: b.accountName, isEnabled: b.isEnabled, openPositions: [] })),
    subscriptions: [{ leadId: 1, leadName: "Atlas Momentum", isEnabled: true }],
  };
  else if (url.pathname === "/api/admin/invite-codes") value = {
    items: [
      { id: 1, code: "MQ-ACTIVE-98K2Z1", createdByUserId: 1, createdByUsername: "preview", maxUses: 5, usedCount: 1, expiresAt: null, isRevoked: false, createdAt: now - 86400000, status: "active" },
      { id: 2, code: "MQ-USED-77P4L9M2", createdByUserId: 1, createdByUsername: "preview", maxUses: 1, usedCount: 1, expiresAt: null, isRevoked: false, createdAt: now - 172800000, status: "exhausted" },
    ],
  };
  else throw new Error("Unsupported mock endpoint");
  return structuredClone(value);
}
