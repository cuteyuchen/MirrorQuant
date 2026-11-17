import { api, ApiError } from "@/api/client";
import type { ExecutionRecord, ExecutionsResponse } from "@/types/api";
import { timestampMs } from "@/utils/format";

// 后端只接受单个精确状态；分类枚举同时供列表与统计使用。
export const EXECUTION_GROUPS = {
  all: [],
  success: ["EXECUTED", "FILLED", "SUCCESS", "ADOPTED_POSITION"],
  failed: ["FAILED", "REJECTED", "UNFILLED"],
  skipped: ["SKIPPED", "SKIPPED_DISABLED", "SKIPPED_STALE", "SKIPPED_UNSUPPORTED", "SKIPPED_CLOSED_CYCLE", "SKIPPED_NO_LOCAL_POS", "SKIPPED_BELOW_MIN"],
  blocked: ["BLOCKED_RISK", "BLOCKED_KILL_SWITCH", "BLOCKED_SOURCE_ORDERING", "BLOCKED_COLLATERAL_BALANCE", "BLOCKED_FILTER",
    "BLOCKED_ALLOCATION_MISMATCH", "BLOCKED_DRAWDOWN", "BLOCKED_SPREAD", "BLOCKED_SLIPPAGE", "BLOCKED_CATCHUP_DRIFT",
    "BLOCKED_RESTING_LIMIT", "BLOCKED_LEVERAGE",
    "BLOCKED_INSUFFICIENT_MARGIN", "BLOCKED_ALLOCATION_DEFICIT", "BLOCKED_ORPHAN_CLOSE_EVIDENCE", "BLOCKED_ORPHAN_DETECTED", "BLOCKED_PROTECTION_UNAVAILABLE"],
} as const;
export type ExecutionFilter = keyof typeof EXECUTION_GROUPS;
export type ExecutionCounts = Record<ExecutionFilter, number | null>;
export const EXECUTION_FILTERS = Object.keys(EXECUTION_GROUPS) as ExecutionFilter[];
export function executionGroup(status: string | null | undefined): Exclude<ExecutionFilter, "all"> | "other" {
  const normalized = status?.trim().toUpperCase() ?? "";
  for (const group of EXECUTION_FILTERS) {
    if (group !== "all" && (EXECUTION_GROUPS[group] as readonly string[]).includes(normalized)) return group;
  }
  return "other";
}
export function executionOrder(a: ExecutionRecord, b: ExecutionRecord) {
  return (timestampMs(b.createdAt) ?? 0) - (timestampMs(a.createdAt) ?? 0) || (b.id ?? 0) - (a.id ?? 0);
}
type ExecutionRequest = (endpoint: string) => Promise<ExecutionsResponse>;
const defaultRequest: ExecutionRequest = endpoint => api.get<ExecutionsResponse>(endpoint);
interface Query { bindingId?: number | null; status?: string; limit: number; offset: number }
async function queryExecutions(options: Query, request: ExecutionRequest): Promise<{ items: ExecutionRecord[]; total: number | null }> {
  const query = new URLSearchParams({ limit: String(options.limit), offset: String(options.offset) });
  if (options.bindingId != null) query.set("binding_id", String(options.bindingId));
  if (options.status) query.set("status", options.status);
  const response = await request(`/api/dashboard/executions?${query}`);
  if (!Array.isArray(response?.items) || (response.total != null && (!Number.isSafeInteger(response.total) || response.total < 0))) {
    throw new ApiError(502, "INVALID_RESPONSE");
  }
  return { items: response.items, total: response.total };
}
export async function readExecutionCounts(bindingId?: number | null, request: ExecutionRequest = defaultRequest): Promise<ExecutionCounts> {
  const results = await Promise.all(EXECUTION_FILTERS.map(async group => {
    const statuses: readonly (string | undefined)[] = group === "all" ? [undefined] : EXECUTION_GROUPS[group];
    const counts = await Promise.all(statuses.map(async status => (await queryExecutions({ bindingId, status, limit: 1, offset: 0 }, request)).total));
    return [group, counts.some(count => count == null) ? null : counts.reduce<number>((sum, count) => sum + (count ?? 0), 0)] as const;
  }));
  return Object.fromEntries(results) as ExecutionCounts;
}
export async function readExecutionPage(options: {
  bindingId?: number | null; filter: ExecutionFilter; page: number; pageSize: number;
}, request: ExecutionRequest = defaultRequest): Promise<{ items: ExecutionRecord[]; total: number | null }> {
  const start = options.page * options.pageSize;
  const end = start + options.pageSize;
  if (options.filter === "all") return queryExecutions({ bindingId: options.bindingId, limit: options.pageSize, offset: start }, request);
  const batches = await Promise.all(EXECUTION_GROUPS[options.filter].map(async status => {
    const items: ExecutionRecord[] = [];
    let total: number | null = null;
    let offset = 0;
    // 各分类取前 end 条足以得到全局前 end 条，超过500时遵循接口上限继续分页。
    while (offset < end) {
      const limit = Math.min(500, end - offset);
      const response = await queryExecutions({ bindingId: options.bindingId, status, limit, offset }, request);
      if (offset === 0) total = response.total;
      items.push(...response.items);
      if (response.items.length < limit || (total != null && items.length >= total)) break;
      offset += response.items.length;
    }
    return { items, total };
  }));
  return {
    items: batches.flatMap(batch => batch.items).sort(executionOrder).slice(start, end),
    total: batches.some(batch => batch.total == null) ? null : batches.reduce((sum, batch) => sum + (batch.total ?? 0), 0),
  };
}
