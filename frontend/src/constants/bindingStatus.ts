import { api, ApiError, CancelledRequestError } from "@/api/client";
import type { Binding, BoardLead, BoardMine, DashboardBoard, ExecutionsResponse, OrderIntent } from "@/types/api";
import { timestampMs } from "@/utils/format";
import { executionGroup } from "@/utils/executions";

export interface BindingPresentation {
  state: string;
  reasons: string[];
  tone: "danger" | "warning" | "neutral";
  pendingCount: number | null;
  positionCount: number | null;
}
export interface ExecutionCounts { success: number; failed: number }

function leadFor(binding: Binding, board: DashboardBoard | null): BoardLead | undefined {
  return board?.leads?.find(lead => lead.id === binding.leadId);
}
function mineFor(binding: Binding, lead?: BoardLead): BoardMine[] {
  return (lead?.positions ?? []).flatMap(position => (position.mine ?? []).filter(mine => mine.bindingId === binding.id));
}

// 只计算确知的实持仓；未知数量或缺失列表不能被解释为已经空仓。
export function bindingPositionCount(binding: Binding, board: DashboardBoard | null): number | null {
  const lead = leadFor(binding, board);
  if (!lead || lead.positions == null || lead.orphans == null || (lead.positionShow !== true && !lead.positions.length)) return null;
  const positions = new Set<string>();
  for (const position of lead.positions) {
    const mine = position.mine?.find(row => row.bindingId === binding.id);
    if (!mine) return null;
    if (mine.qty == null || !Number.isFinite(mine.qty)) return null;
    if (Math.abs(mine.qty) > 0) positions.add(position.key ?? `${position.symbol}:${position.side}`);
  }
  for (const orphan of lead.orphans.filter(row => row.bindingId === binding.id)) {
    if (orphan.qty == null || !Number.isFinite(orphan.qty)) return null;
    if (Math.abs(orphan.qty) > 0) positions.add(`orphan:${orphan.symbol}:${orphan.side}`);
  }
  return positions.size;
}

export function bindingPresentation(binding: Binding, board: DashboardBoard | null, intents: OrderIntent[] | null, killSwitch: boolean | null): BindingPresentation {
  const lead = leadFor(binding, board);
  const mine = mineFor(binding, lead);
  const pendingCount = intents == null ? null : intents.filter(intent => intent.bindingId === binding.id).length;
  const positionCount = bindingPositionCount(binding, board);
  const orphans = lead?.orphans?.filter(orphan => orphan.bindingId === binding.id) ?? [];
  const result = (state: string, reasons: (string | null | undefined)[], tone: BindingPresentation["tone"]): BindingPresentation =>
    ({ state, reasons: [...new Set(reasons.filter((reason): reason is string => !!reason))], tone, pendingCount, positionCount });
  // 状态优先级保留资金风险的原因，即使全局暂停也不掩盖熔断与遗留仓位。
  if (orphans.length) return result("BLOCKED", orphans.map(orphan => orphan.reason ?? "ORPHAN_POSITION"), "danger");
  if (binding.circuitBreakerTripped === true || mine.some(row => row.breakerTripped === true)) {
    return result("CIRCUIT_BREAKER", [binding.circuitBreakerReason ?? "CIRCUIT_BREAKER"], "danger");
  }
  if (killSwitch === true) return result("KILL_SWITCH", ["GLOBAL_KILL_SWITCH"], "danger");
  if (binding.isEnabled === false) return result("DISABLED", ["BINDING_DISABLED"], "neutral");
  if (pendingCount != null && pendingCount > 0) return result("PENDING_ORDERS", ["WAITING_EXECUTION"], "warning");
  if (binding.isEnabled == null || binding.circuitBreakerTripped == null || killSwitch == null || pendingCount == null || !lead) {
    return result("UNKNOWN", [], "warning");
  }
  if (lead.enabled === false) return result("BLOCKED", ["LEAD_DISABLED"], "warning");
  if (lead.stale === true) return result("STALE", ["LEAD_STALE"], "warning");
  const blocked = mine.filter(row => row.state === "blocked");
  if (blocked.length) return result("BLOCKED", blocked.map(row => row.reason), "danger");
  const incomplete = mine.filter(row => row.state === "partial" || row.state === "missing");
  if (incomplete.length) return result(incomplete.some(row => row.state === "partial") ? "PARTIAL" : "MISSING", incomplete.map(row => row.reason), "warning");
  if (binding.baselineSeeded === false) return result("BASELINE_PENDING", ["BASELINE_PENDING"], "warning");
  // 已对齐枚举不能替代真实数量，缺失数量仍必须显示待确认。
  // 隐藏仓位带单员的重构证据由看板确认（positions/orphans 非空即已确认），
  // 不能仅凭 positionShow 再判未知，否则隐藏仓位绑定永远显示未知。
  const evidenceMissing = binding.baselineSeeded == null || positionCount == null || lead.stale == null ||
    lead.enabled == null || lead.positions == null || lead.orphans == null ||
    (lead.positions.length > 0 && (mine.length < lead.positions.length || mine.some(row => row.state !== "aligned")));
  if (evidenceMissing) {
    const hiddenWithoutEvidence = lead.positionShow === false &&
      (lead.positions == null || lead.orphans == null || lead.positions.length === 0);
    return result("UNKNOWN", hiddenWithoutEvidence ? ["POSITION_DATA_HIDDEN"] : mine.map(row => row.reason), "warning");
  }
  return result("HEALTHY", [], "neutral");
}

export function bindingSummary(bindings: Binding[], board: DashboardBoard | null, intents: OrderIntent[] | null, killSwitch: boolean | null) {
  const states = bindings.map(binding => bindingPresentation(binding, board, intents, killSwitch));
  return {
    total: bindings.length,
    tripped: bindings.filter(binding => binding.circuitBreakerTripped === true ||
      mineFor(binding, leadFor(binding, board)).some(mine => mine.breakerTripped === true)).length,
    disabled: bindings.filter(binding => binding.isEnabled === false).length,
    pending: intents == null ? null : new Set(intents.map(intent => intent.bindingId).filter(id => bindings.some(binding => binding.id === id))).size,
    unknown: states.filter(state => state.state === "UNKNOWN").length,
  };
}

// 执行接口没有按日聚合：固定快照截止时间，分页到当天起点，并去重分页期间新写入造成的重复行。
export async function fetchTodayExecutionCounts(dayStart: number | null, cutoff: number | null, current: () => boolean = () => true): Promise<Record<number, ExecutionCounts>> {
  if (dayStart == null || cutoff == null || !Number.isFinite(dayStart) || !Number.isFinite(cutoff) || dayStart > cutoff) {
    throw new ApiError(502, "INVALID_RESPONSE");
  }
  const counts: Record<number, ExecutionCounts> = {};
  const seen = new Set<number>();
  const pages = new Set<string>();
  const limit = 500;
  let offset = 0;
  while (current()) {
    const response = await api.get<ExecutionsResponse>(`/api/dashboard/executions?limit=${limit}&offset=${offset}`);
    if (!current()) throw new CancelledRequestError();
    if (response.items == null) throw new ApiError(502, "INVALID_RESPONSE");
    const rows = response.items;
    const signature = `${rows[0]?.id}:${rows.at(-1)?.id}:${rows.length}`;
    if (rows.length && pages.has(signature)) throw new ApiError(502, "INVALID_RESPONSE");
    pages.add(signature);
    let reachedPreviousDay = false;
    for (const row of rows) {
      const time = timestampMs(row.createdAt);
      if (time == null || row.id == null || row.status == null) throw new ApiError(502, "INVALID_RESPONSE");
      if (row.bindingId == null) continue; // 无归属的历史记录不计入任何绑定，不能因此让整列统计失败。
      if (time < dayStart) { reachedPreviousDay = true; continue; }
      if (time > cutoff || seen.has(row.id)) continue;
      seen.add(row.id);
      const tally = counts[row.bindingId] ??= { success: 0, failed: 0 };
      const group = executionGroup(row.status);
      if (group === "success") tally.success++;
      else if (group === "failed") tally.failed++;
    }
    offset += rows.length;
    if (reachedPreviousDay || rows.length < limit || (response.total != null && offset >= response.total)) return counts;
  }
  throw new CancelledRequestError();
}
