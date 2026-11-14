export type AlignmentStatus = "aligned" | "waiting" | "mismatch" | "unbound" | "paused" | "stale";

function positionKey(position: any, symbolMap: Record<string, string> = {}): string {
  const raw = position.symbol || position.instId || "";
  const symbol = (symbolMap[raw] || raw).toUpperCase().replace(/-SWAP$/, "").replace(/[-_/]/g, "");
  let side = String(position.positionSide || position.posSide || "").toLowerCase();
  if (side === "net" || side === "both") side = Number(position.pos) < 0 ? "short" : "long";
  return `${symbol}:${side}`;
}

/** Compare symbols and directions in each bound account; this does not compare size. */
export function getLeadAlignment(lead: any, overview: any, now = Date.now()): AlignmentStatus {
  if (!lead.isEnabled) return "paused";
  if (lead.stale || !lead.positionsKnown || lead.snapshotIncomplete) return "stale";
  const bindings = (overview.bindingsStatus || []).filter((b: any) => b.leadId === lead.id && b.isEnabled);
  if (!bindings.length) return "unbound";
  if (overview.kpis?.globalKillSwitch || bindings.some((b: any) => b.circuitBreakerTripped)) return "paused";
  let anyPositions = false;
  let missingPositions = false;
  for (const binding of bindings) {
    const account = (overview.accountsStatus || []).find((a: any) => a.id === binding.accountId);
    if (!account?.isEnabled) return "paused";
    const synced = Date.parse(account.lastSyncAt || "");
    if (account.status !== "connected" || !Number.isFinite(synced) || synced > now || now - synced > 90000) return "stale";

    // An account can follow several leads, so include all of its active sources
    // before treating an additional position as a mismatch.
    const accountBindings = (overview.bindingsStatus || []).filter((b: any) => b.accountId === account.id && b.isEnabled);
    const expected = new Set<string>();
    for (const item of accountBindings) {
      const source = (overview.leadsStatus || []).find((l: any) => l.id === item.leadId);
      if (!source?.isEnabled || source.stale || !source.positionsKnown || source.snapshotIncomplete) return "stale";
      for (const position of overview.leadPositions || []) {
        if (position.leadId === item.leadId) expected.add(positionKey(position, item.symbolMap));
      }
    }
    const actual = new Set<string>((overview.followerPositions || [])
      .filter((p: any) => p.accountId === account.id && Math.abs(Number(p.pos)) > 0)
      .map((p: any) => positionKey(p)));
    const leadKeys = (overview.leadPositions || [])
      .filter((p: any) => p.leadId === lead.id).map((p: any) => positionKey(p, binding.symbolMap));
    if ([...actual].some(key => !expected.has(key))) return "mismatch";
    if (leadKeys.some((key: string) => !actual.has(key))) missingPositions = true;
    anyPositions ||= leadKeys.length > 0;
  }
  return missingPositions ? "waiting" : anyPositions ? "aligned" : "waiting";
}
