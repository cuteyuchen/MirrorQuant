import type { Account, Binding, Lead, LegacyPosition } from "@/types/api";

export type AlignmentStatus = "aligned" | "waiting" | "mismatch" | "unbound" | "paused" | "stale";
type AlignmentLead = Pick<Lead, "id" | "isEnabled" | "stale" | "positionsKnown" | "snapshotIncomplete">;
type AlignmentAccount = Pick<Account, "id" | "isEnabled" | "status"> & { stale?: boolean | null };
type AlignmentBinding = Pick<Binding, "leadId" | "accountId" | "isEnabled"> & Partial<Pick<Binding, "circuitBreakerTripped" | "symbolMap">>;
interface AlignmentOverview {
  kpis?: { globalKillSwitch?: boolean | null };
  leadsStatus?: AlignmentLead[]; accountsStatus?: AlignmentAccount[];
  bindingsStatus?: AlignmentBinding[]; leadPositions?: LegacyPosition[];
  followerPositions?: LegacyPosition[];
}

function positionKey(position: LegacyPosition, symbolMap: Record<string, string> | null = {}): string {
  const raw = position.symbol || position.instId || "";
  const symbol = (symbolMap?.[raw] || raw).toUpperCase().replace(/-SWAP$/, "").replace(/[-_/]/g, "");
  let side = String(position.positionSide || position.posSide || "").toLowerCase();
  // 旧接口兼容时也不能把未知净仓方向推断为空头或多头。
  if (side === "net" || side === "both") {
    const qty = position.pos ?? position.qty;
    side = qty == null || !Number.isFinite(qty) || qty === 0 ? "unknown" : qty < 0 ? "short" : "long";
  }
  if (!["long", "short"].includes(side)) side = "unknown";
  return `${symbol}:${side}`;
}

// 只兼容旧接口的方向对齐；时效由后端 stale 决定，新看板直接使用 board.mine.state。
export function getLeadAlignment(lead: AlignmentLead, overview: AlignmentOverview, _now?: number): AlignmentStatus {
  if (!lead.isEnabled) return "paused";
  if (lead.stale || !lead.positionsKnown || lead.snapshotIncomplete) return "stale";
  const bindings = (overview.bindingsStatus || []).filter(b => b.leadId === lead.id && b.isEnabled);
  if (!bindings.length) return "unbound";
  if (overview.kpis?.globalKillSwitch || bindings.some(b => b.circuitBreakerTripped)) return "paused";
  let anyPositions = false;
  let missingPositions = false;
  for (const binding of bindings) {
    const account = (overview.accountsStatus || []).find(a => a.id === binding.accountId);
    if (!account?.isEnabled) return "paused";
    if (account.status !== "connected" || account.stale) return "stale";

    // 一个账户可能跟随多个带单员，不能把另一来源的持仓误报为残留仓。
    const accountBindings = (overview.bindingsStatus || []).filter(b => b.accountId === account.id && b.isEnabled);
    const expected = new Set<string>();
    for (const item of accountBindings) {
      const source = (overview.leadsStatus || []).find(l => l.id === item.leadId);
      if (!source?.isEnabled || source.stale || !source.positionsKnown || source.snapshotIncomplete) return "stale";
      for (const position of overview.leadPositions || []) {
        if (position.leadId === item.leadId) expected.add(positionKey(position, item.symbolMap));
      }
    }
    const actual = new Set<string>((overview.followerPositions || [])
      .filter(p => p.accountId === account.id && Math.abs(p.pos ?? p.qty ?? 0) > 0)
      .map(p => positionKey(p)));
    const leadKeys = (overview.leadPositions || [])
      .filter(p => p.leadId === lead.id).map(p => positionKey(p, binding.symbolMap));
    if ([...actual, ...leadKeys].some(key => key.endsWith(":unknown"))) return "stale";
    if ([...actual].some(key => !expected.has(key))) return "mismatch";
    if (leadKeys.some(key => !actual.has(key))) missingPositions = true;
    anyPositions ||= leadKeys.length > 0;
  }
  return missingPositions ? "waiting" : anyPositions ? "aligned" : "waiting";
}
