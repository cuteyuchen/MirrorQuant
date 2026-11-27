// 响应中的未知值保留为 null，不把缺失的资金或数量补成零。
export type NullableFields<T> = { [K in keyof T]: T[K] | null };
export type Environment = "live" | "demo";
export type PositionSide = "LONG" | "SHORT";
export type FollowState = "aligned" | "partial" | "missing" | "blocked" | "unknown";
export type TradeAction = "OPEN" | "ADD" | "REDUCE" | "CLOSE";
export type Timestamp = number | string;
export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };
export type ListResponse<T> = { items: T[] | null };

export type BoardAlert = NullableFields<{
  level: "critical" | "warning" | "info"; code: string; message: string;
  bindingId: number; leadId: number; accountId: number;
}>;
export type BoardAccount = NullableFields<{
  id: number; name: string; exchange: string; environment: Environment; status: string;
  equity: number; available: number; unrealizedPnl: number; todayPnl: number;
  todayPnlPct: number; lastSyncAt: number; riskLegacyUnverified: boolean;
}> & { ownerUserId?: number | null };
export type BoardMine = NullableFields<{
  bindingId: number; bindingName: string; accountId: number; accountName: string;
  environment: Environment; bindingEnabled: boolean; breakerTripped: boolean;
  state: FollowState; qty: number; entryPrice: number; pnl: number; pnlPct: number;
  expectedQty: number; reason: string;
}>;
export type BoardOrphan = BoardMine & NullableFields<{ symbol: string; side: PositionSide }>;
export type BoardPosition = NullableFields<{
  key: string; symbol: string; side: PositionSide; qty: number; entryPrice: number;
  markPrice: number; pnl: number; pnlPct: number; leverage: number; notional: number;
  openedAt: number; openedToday: boolean; takeProfitPrice: number; stopLossPrice: number;
  mine: BoardMine[];
}>;
export type BoardExecution = NullableFields<{
  bindingId: number; accountName: string; status: string; qty: number; price: number;
  slippagePct: number; spreadPct: number; details: string;
}>;
export type BoardEvent = NullableFields<{
  id: number | string; time: number; symbol: string; side: PositionSide; action: TradeAction;
  qty: number; price: number; realizedPnl: number; mine: BoardExecution[];
}>;
export type BoardLead = NullableFields<{
  id: number; name: string; avatarUrl: string; status: string; enabled: boolean;
  stale: boolean; snapshotAgeSec: number; positionShow: boolean;
  boundCount: number; activeBoundCount: number;
  todayOpenCount: number;
  todayCloseCount: number; todayRealizedPnl: number; positions: BoardPosition[];
  orphans: BoardOrphan[]; todayEvents: BoardEvent[];
}> & { positionsConfirmed?: boolean | null };
export type DashboardBoard = NullableFields<{
  serverTime: number; dayStart: number; globalKillSwitch: boolean;
  alerts: BoardAlert[]; accounts: BoardAccount[]; leads: BoardLead[];
}>;
export type OrderIntent = NullableFields<{
  id: number | string; bindingId: number; bindingName: string; accountId: number;
  accountName: string; instId: string; posSide: string; state: string;
  requestedQty: number; filledQty: number; exchangeOrderId: string; reduceOnly: boolean;
  createdAt: Timestamp; sourceEventId: number | string;
}>;
export type IntentsResponse = ListResponse<OrderIntent>;
export type ExecutionRecord = NullableFields<{
  id: number; bindingId: number; bindingName: string; leadId: number; accountId: number;
  leadName: string; accountName: string; symbol: string; instId: string; side: string;
  positionSide: string; action: string; status: string; leadQty: number; leadPrice: number;
  leadNotional: number; myQty: number; myPrice: number; myNotional: number;
  slippagePct: number; spreadPct: number; orderId: string; details: string; createdAt: Timestamp;
}> & { environment?: Environment | null; bindingDeleted?: boolean | null; accountDeleted?: boolean | null };
export type ExecutionGroupCounts = Record<"all" | "actionable" | "success" | "failed" | "skipped" | "blocked", number | null>;
export type ExecutionsResponse = ListResponse<ExecutionRecord> & { total: number | null; counts?: ExecutionGroupCounts | null };
export type SocketMessage = { type?: string; data?: ExecutionRecord | null };

// 管理接口仍沿用现有序列化字段，与看板的新契约分开维护。
export interface Account {
  id: number; name: string | null; exchange: string | null; environment: Environment | null;
  proxyId: number | null; isEnabled: boolean | null; status: string | null;
  ownerUserId?: number | null;
  apiKeyMasked?: string | null; marginMode?: string | null; positionMode?: string | null;
  totalEquity?: number | null; usdtEquity?: number | null; usdcEquity?: number | null;
  availEquity?: number | null; unrealizedPnl?: number | null; lastSyncAt?: Timestamp | null;
  lastError?: string | null; clockOffsetMs?: number | null; positions?: LegacyPosition[] | null;
  riskEquityLegacyUnverified?: boolean | null;
  // 回撤只使用后端高水位，不在浏览器臆造权益基准。
  hwmEquity?: number | null; dayStartEquity?: number | null; totalNotional?: number | null;
}
export interface Lead {
  id: number; name: string | null; portfolioId: string | null; platform?: string | null;
  avatarUrl?: string | null; description?: string | null; pollIntervalSec?: number | null;
  proxyId: number | null; isEnabled: boolean | null; isPublicDashboard?: boolean | null;
  slotWatchEnabled?: boolean | null;
  positionShow?: boolean | null; marginBalance?: number | null; aumAmount?: number | null;
  lastTradeTime?: number | null; lastSyncAt?: Timestamp | null; lastError?: string | null;
  status?: string | null; openPositionsCount?: number | null; openUnrealizedPnl?: number | null;
  stale?: boolean | null; snapshotAgeSec?: number | null; snapshot?: LeadSnapshot | null;
  positionsKnown?: boolean | null; snapshotIncomplete?: boolean | null;
  latestAction?: { time: number | null; symbol: string | null; side: string | null; qty: number | null; direction: string | null } | null;
  notificationConfig?: NotificationSettings | null;
}
export interface PublicDashboardResponse {
  enabled: boolean | null;
  leads: Lead[] | null;
}
export interface ProxyNode {
  id: number; name: string | null; url: string | null; region: string | null;
  priority: number | null; isEnabled: boolean | null; isHealthy: boolean | null;
  latencyMs: number | null; lastCheckedAt: Timestamp | null; lastError: string | null;
  url_configured?: boolean | null;
}
// 检测请求成功不等于代理健康，界面必须检查 test.healthy。
export interface ProxyTestResponse {
  item: ProxyNode | null;
  test: { healthy: boolean | null; latencyMs: number | null; error: string | null } | null;
}
export interface RiskSettings {
  enable_circuit_breaker: boolean | null; enable_leverage_cap: boolean | null;
  max_total_leverage: number | null; max_single_leverage: number | null;
  max_instrument_leverage: number | null; max_drawdown_pct: number | null;
  daily_max_loss_pct: number | null; max_slippage_pct: number | null;
  max_spread_pct: number | null; ioc_offset_pct: number | null;
  order_mode: string | null; tp_sl_mode: string | null;
  // 滑点超限处理：block 拦截 / limit 改挂限价单；限价单超过有效期未成交自动撤单。
  slippage_action: string | null; limit_order_ttl_sec: number | null;
  adopt_existing_positions: boolean | null; adopt_tolerance_pct: number | null;
  enable_stop_loss: boolean | null; stop_loss_pct: number | null;
  enable_take_profit: boolean | null; take_profit_pct: number | null;
  enable_trailing_stop: boolean | null; trailing_callback_pct: number | null;
  hard_stop_loss_pct: number | null;
}
export interface Binding {
  id: number; name: string | null; leadId: number | null; leadName: string | null;
  accountId: number | null; accountName: string | null; accountExchange: string | null;
  accountEnvironment: Environment | null; isEnabled: boolean | null;
  riskScale: number | null; enableCircuitBreaker: boolean | null; enableLeverageCap: boolean | null;
  maxTotalLeverage: number | null; maxSingleLeverage: number | null; maxInstrumentLeverage: number | null;
  maxDrawdownPct: number | null; dailyMaxLossPct: number | null; maxSlippagePct: number | null;
  maxSpreadPct: number | null; iocOffsetPct: number | null; orderMode: string | null;
  slippageAction: string | null; limitOrderTtlSec: number | null;
  adoptExistingPositions: boolean | null; adoptTolerancePct: number | null;
  adoptionCompleted: boolean | null;
  tpSlMode: string | null; enableStopLoss: boolean | null; stopLossPct: number | null;
  enableTakeProfit: boolean | null; takeProfitPct: number | null;
  enableTrailingStop: boolean | null; trailingCallbackPct: number | null; hardStopLossPct: number | null;
  symbolWhitelist: string[] | null; symbolBlacklist: string[] | null; symbolMap: Record<string, string> | null;
  circuitBreakerTripped: boolean | null; circuitBreakerReason: string | null;
  consecutiveFailures: number | null; baselineSeeded: boolean | null; pendingDebtUsd: number | null;
  todaySuccessCount?: number | null; todayFailedCount?: number | null;
}
export type BindingsResponse = ListResponse<Binding> & { globalKillSwitch: boolean | null };
export interface ResilienceSettings {
  missed_order_max_age_sec: number | null; catchup_max_price_dev_pct: number | null;
  reconcile_interval_sec: number | null; auto_close_orphan_positions: string | null;
  insufficient_margin_policy: string | null; flip_close_first: boolean | null;
  clock_sync_interval_sec: number | null; max_consecutive_failures: number | null;
}
export type DisplayUnit = "notional" | "quantity" | "both";
export interface NotificationSettings {
  display_unit: DisplayUnit | null;
  webhook_enabled: boolean | null; webhook_url: string | null;
  telegram_enabled: boolean | null; telegram_bot_token: string | null; telegram_chat_id: string | null;
  wecom_enabled: boolean | null; wecom_webhook_url: string | null;
  dingtalk_enabled: boolean | null; dingtalk_webhook_url: string | null;
  dingtalk_secret?: string | null; dingtalk_secret_configured?: boolean | null;
  outbox_enabled: boolean | null; outbox_path: string | null;
  notify_on_execution: boolean | null; notify_on_lead_order: boolean | null;
  notify_on_risk_block: boolean | null; notify_on_security_alert: boolean | null;
  telegram_bot_token_configured?: boolean | null; webhook_url_configured?: boolean | null;
  wecom_webhook_url_configured?: boolean | null; dingtalk_webhook_url_configured?: boolean | null;
}
export interface SettingsResponse {
  global_kill_switch: boolean | null; public_monitoring_enabled: boolean | null;
  risk_defaults: RiskSettings | null; resilience: ResilienceSettings | null;
  notifications: NotificationSettings | null;
  security?: {
    encryptionAlgorithm?: string | null; passwordHashing?: string | null; authMode?: string | null;
    masterKeyConfigured?: boolean | null; masterKeySource?: string | null; totpEnabled?: boolean | null;
  } | null;
}
export interface AuditLog {
  id: number; action?: string | null; username?: string | null; details?: string | null;
  createdAt?: Timestamp | null; resourceType?: string | null; resourceId?: string | null; ipAddress?: string | null;
}
export interface SecurityGuard {
  id: number; username?: string | null; ipAddress?: string | null; lockedUntil?: Timestamp | null;
  lockRemainingSec?: number | null; failureCount?: number | null; isLocked?: boolean | null;
  guardKey?: string | null; keyType?: string | null; keyValue?: string | null;
  failCount?: number | null; lastIp?: string | null; lastUsername?: string | null; lastFailAt?: Timestamp | null;
}
export interface AccountTestResult {
  ok: boolean | null; error?: string | null; posMode?: string | null;
  totalEquity?: number | null; usdtEquity?: number | null; usdcEquity?: number | null;
}
export interface User {
  id: number; username: string; role: string; isActive?: boolean;
  totpEnabled?: boolean; createdAt?: string | null; lastLoginAt?: string | null;
}
export interface AuthStatus { initialized: boolean; authenticated: boolean; user: User | null }
export interface CaptchaChallenge { captchaToken: string; svg: string; expiresInSec: number }
// 1.3.3 多用户：邀请码注册与管理员用户管理（后端契约，camelCase）。
export type InviteCodeStatus = "active" | "revoked" | "expired" | "exhausted";
export interface InviteCode {
  id: number; code: string; createdByUserId: number | null; createdByUsername: string | null;
  maxUses: number | null; usedCount: number | null; expiresAt: string | null;
  isRevoked: boolean | null; createdAt: string | null; status: InviteCodeStatus | null;
}
export interface AdminUserSummary {
  id: number; username: string; role: string; isActive: boolean | null;
  createdAt: string | null; lastLoginAt: string | null;
  accountCount: number | null; bindingCount: number | null; enabledBindingCount: number | null;
  openPositionCount: number | null; totalEquity: number | null;
}
export interface AdminUserAccount {
  id: number; name: string | null; exchange: string | null; environment: Environment | null;
  status: string | null; isEnabled: boolean | null; equity: number | null;
}
export interface AdminUserBindingPosition { instId: string | null; posSide: string | null; qty: number | null }
export interface AdminUserBinding {
  id: number; name: string | null; leadId: number | null; leadName: string | null;
  accountId: number | null; accountName: string | null; isEnabled: boolean | null;
  openPositions: AdminUserBindingPosition[] | null;
}
export interface AdminUserSubscription { leadId: number; leadName: string | null; isEnabled: boolean | null }
export interface AdminUserDetail {
  user: AdminUserSummary;
  accounts: AdminUserAccount[] | null; bindings: AdminUserBinding[] | null;
  subscriptions: AdminUserSubscription[] | null;
}
export interface TotpSetup { secret: string; otpauthUri: string; qrSvgDataUri: string }
// 通知测试接口逐渠道返回实际投递结果。
export interface NotificationTestResponse {
  ok: boolean | null;
  results: Record<string, { ok: boolean | null; error?: string | null; status?: number | null; errcode?: number | null }> | null;
}
export interface LeadSubscription {
  id?: number | null;
  leadId: number;
  leadName: string;
  leadPlatform?: string | null;
  isEnabled: boolean;
  channels?: string[] | null;
  hasBinding: boolean;
  bindingCount: number;
}
export interface UserPreferences {
  notifications: NotificationSettings | null;
  riskDefaults?: RiskSettings | null;
  hasCustomNotifications: boolean;
  displayUnit: DisplayUnit;
}
export interface ValidationIssue {
  loc: (string | number)[]; msg: string; type: string; ctx?: Record<string, string | number>;
}
export interface ErrorBody {
  detail?: string | ValidationIssue[] | {
    code?: string; message?: string; captchaRequired?: boolean; totpRequired?: boolean; lockRemainingSec?: number;
  };
  error?: string; code?: string;
  // 认证接口可在顶层返回挑战和锁定时间，不只检查 detail 对象。
  captchaRequired?: boolean | null; totpRequired?: boolean | null; lockRemainingSec?: number | null;
}

// 深度分析保留服务端指标，不在前端重算收益或猜测缺失行情。
export interface Performance {
  roi?: number | null; pnl?: number | null; copierPnl?: number | null; mdd?: number | null;
  winRate?: number | null; winOrders?: number | null; totalOrder?: number | null; sharpRatio?: number | null;
}
export interface LeadProfile {
  nickname?: string | null; avatarUrl?: string | null; description?: string | null;
  badgeName?: string | null; positionShow?: boolean | null; signTradFi?: boolean | null;
  tags?: string[] | null; lastTradeTime?: number | null; aumAmount?: number | null;
  copyAum?: number | null; marginBalance?: number | null; profitSharingRate?: number | null;
  copierPnl?: number | null; currentCopyCount?: number | null; maxCopyCount?: number | null;
  sharpRatio?: number | null;
}
export interface PositionSummary {
  positionCount?: number | null; shortCount?: number | null; longCount?: number | null;
  totalNotionalLeverage?: number | null; entryLeverage?: number | null; marginBalance?: number | null;
  availableMargin?: number | null; totalMarkNotional?: number | null; totalEntryNotional?: number | null;
  maxPositionLeverage?: number | null; estMaintenanceMarginRatioPct?: number | null;
  marginCoveragePct?: number | null; adverseMoveBufferPct?: number | null;
  totalUnrealizedPnl?: number | null; totalMarginRoePct?: number | null; mmrSource?: string | null; priceAsOf?: number | null;
}
export interface PriceAnalysis {
  ok?: boolean | null; verdict?: string | null; verdictZh?: string | null; verdictLevel?: string | null;
  trend1h?: string | null; trend1hZh?: string | null; trend4h?: string | null; trend4hZh?: string | null;
  entryZone?: string | null; entryZoneZh?: string | null; scalingStyle?: string | null; scalingStyleZh?: string | null;
  summary?: string | null; summaryZh?: string | null; entryRangePct?: number | null; curRangePct?: number | null;
  low72h?: number | null; high72h?: number | null; eq72h?: number | null; keyResistance?: number | null;
  keySupport?: number | null; invalidationLevel?: number | null; target1?: number | null; target2?: number | null;
  rrFromEntry?: number | null; rsi1h?: number | null; rsi4h?: number | null; atr1hPct?: number | null;
  entryVsEma21Atr?: number | null; nearestBearFvg?: number[] | null; nearestBullFvg?: number[] | null;
}
export interface TelemetryOrder {
  id?: string | number | null; orderId?: string | number | null; symbol?: string | null;
  dir?: string | null; side?: string | null; positionSide?: string | null; ps?: string | null;
  time?: number | null; t?: number | null; orderTime?: number | null; ot?: number | null;
  ty?: string | null; qty?: number | null; q?: number | null; avgPrice?: number | null; price?: number | null;
  p?: number | null; pnl?: number | null; pnlKnown?: boolean | null; isClose?: boolean | null; type?: string | null;
}
export interface LegacyPosition {
  symbol?: string | null; instId?: string | null; positionSide?: string | null; posSide?: string | null;
  pos?: number | null; qty?: number | null; accountId?: number | null; leadId?: number | null;
  avgEntry?: number | null; currentPrice?: number | null; markPrice?: number | null;
  entryValue?: number | null; notional?: number | null; markValue?: number | null;
  entryLeverage?: number | null; notionalLeverage?: number | null; sharePct?: number | null;
  maintMarginPct?: number | null; maintMargin?: number | null; maintMarginRoePct?: number | null;
  uPnL?: number | null; uPnLRoePct?: number | null; heldMs?: number | null; openTime?: number | null;
  lastAddTime?: number | null; fundingRate?: number | null; priceEdgePct?: number | null; priceDiffPct?: number | null;
  entryOrdersCount?: number | null; entryOrders?: TelemetryOrder[] | null; analysis?: PriceAnalysis | null;
  mmr?: { maxLev?: number | null; mmrPct?: number | null; tier?: number | null; exact?: boolean | null } | null;
  // 账户仓位接口的原始字段；uplRatio 是比例而非百分数。
  avgPx?: number | null; curPx?: number | null; markPx?: number | null;
  upl?: number | null; uplRatio?: number | null; notionalUsd?: number | null; marginUsd?: number | null; lever?: number | null;
  cTime?: Timestamp | null; uTime?: Timestamp | null; mgnMode?: string | null;
  availPos?: number | null; priceSource?: string | null;
}
export interface OrderBatch extends TelemetryOrder {
  startTime?: number | null; endTime?: number | null; totalQty?: number | null; notional?: number | null;
  totalValue?: number | null; orderCount?: number | null; count?: number | null; totalPnl?: number | null;
  items?: TelemetryOrder[] | null; orders?: TelemetryOrder[] | number | null;
}
export interface TradeCycle {
  cycleId?: string | number | null; symbol?: string | null; positionSide?: string | null; status?: string | null;
  avgEntryPrice?: number | null; avgEntry?: number | null; avgExitPrice?: number | null; avgClose?: number | null;
  entryCount?: number | null; entryOrdersCount?: number | null; scaleInCount?: number | null;
  exitCount?: number | null; exitOrdersCount?: number | null; durationMs?: number | null; durationSec?: number | null;
  totalEntryQty?: number | null; totalExitQty?: number | null;
  realizedPnl?: number | null; openedAt?: number | null; startTime?: number | null; endTime?: number | null;
}
export interface ClosedPosition {
  id?: number | string | null; symbol?: string | null; side?: string | null; closed?: number | null;
  opened?: number | null; leverage?: number | null; isolated?: boolean | string | null; avgCost?: number | null;
  avgClosePrice?: number | null; avgClose?: number | null; maxQty?: number | null; closedVol?: number | null;
  maxOpen?: number | null; pnl?: number | null; heldMs?: number | null;
}
export interface LeadSnapshot {
  profile?: LeadProfile | null; performance?: Record<string, Performance | null> | null;
  meta?: {
    generatedAt?: number | null; priceUpdatedAt?: number | null; portfolioId?: string | null;
    fetchMs?: number | null; orderStoreSize?: number | null; stale?: boolean | null; snapshotAgeSec?: number | null;
    orderHistoryComplete?: boolean | null; closedHistoryComplete?: boolean | null; detailOk?: boolean | null;
  } | null;
  openPositionsSummary?: PositionSummary | null;
  openPositionsMeta?: {
    source?: string | null; authoritative?: boolean | null; complete?: boolean | null;
    emptyVerified?: boolean | null; partial?: boolean | null;
    reconstructed?: boolean | null; derived?: boolean | null; positionShow?: boolean | null; asOf?: number | null;
  } | null;
  openPositions?: LegacyPosition[] | null; roiChart365D?: { t: number | null; v: number | null }[] | null;
  allocation?: { updateTime?: number | null; items?: { asset: string | null; volume: number | null }[] | null } | null;
  orderBatches?: OrderBatch[] | null; tradeCycles?: TradeCycle[] | null;
  closed?: ClosedPosition[] | null; orders?: TelemetryOrder[] | null;
}
