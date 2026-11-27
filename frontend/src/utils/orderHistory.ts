import type { LeadSnapshot, OrderBatch, TelemetryOrder, TradeCycle } from "@/types/api";
import { finiteNumber } from "@/utils/format";
import { positionsConfirmed } from "@/utils/positions";

const EPS = 1e-7;
const RESOLVED_ACTIONS = new Set([
  "OPEN_LONG",
  "ADD_LONG",
  "REDUCE_LONG",
  "CLOSE_LONG",
  "OPEN_SHORT",
  "ADD_SHORT",
  "REDUCE_SHORT",
  "CLOSE_SHORT",
]);

interface NormalizedOrder {
  index: number;
  sym: string;
  side: "BUY" | "SELL";
  ps: "LONG" | "SHORT" | "BOTH";
  qty: number;
  price: number;
  ot: number;
  ut: number;
  pnl: number | null;
  presetAction: string | null;
}

interface ClosedInterval {
  open: number;
  close: number;
  ps: "LONG" | "SHORT";
}

export function isUnresolvedDirection(dir?: string | null): boolean {
  const upper = (dir ?? "").trim().toUpperCase();
  return !RESOLVED_ACTIONS.has(upper);
}

function inferTradeSide(order: TelemetryOrder): "BUY" | "SELL" | null {
  const side = (order.side ?? "").trim().toUpperCase();
  if (side === "BUY" || side === "SELL") return side;
  const dir = (order.dir ?? "").trim().toUpperCase();
  if (dir === "BUY" || dir === "OPEN_LONG" || dir === "ADD_LONG" || dir === "REDUCE_SHORT" || dir === "CLOSE_SHORT") {
    return "BUY";
  }
  if (dir === "SELL" || dir === "OPEN_SHORT" || dir === "ADD_SHORT" || dir === "REDUCE_LONG" || dir === "CLOSE_LONG") {
    return "SELL";
  }
  return null;
}

function inferPosSide(order: TelemetryOrder): "LONG" | "SHORT" | "BOTH" {
  const ps = (order.positionSide ?? order.ps ?? "").trim().toUpperCase();
  if (ps === "LONG" || ps === "SHORT") return ps;
  const dir = (order.dir ?? "").trim().toUpperCase();
  if (dir.endsWith("_LONG") || dir === "LONG") return "LONG";
  if (dir.endsWith("_SHORT") || dir === "SHORT") return "SHORT";
  return "BOTH";
}

function oneWayActionFromState(before: number, after: number, side: "BUY" | "SELL"): string {
  if (before > EPS || after > EPS) {
    if (side === "BUY") return before <= EPS ? "OPEN_LONG" : "ADD_LONG";
    return after <= EPS ? "CLOSE_LONG" : "REDUCE_LONG";
  }
  if (side === "SELL") return before >= -EPS ? "OPEN_SHORT" : "ADD_SHORT";
  return after >= -EPS ? "CLOSE_SHORT" : "REDUCE_SHORT";
}

function isHedgeOpen(side: "BUY" | "SELL", ps: "LONG" | "SHORT"): boolean {
  return (ps === "LONG" && side === "BUY") || (ps === "SHORT" && side === "SELL");
}

function normalizeClosedRows(snapshot: LeadSnapshot): Map<string, ClosedInterval[]> {
  const map = new Map<string, ClosedInterval[]>();
  for (const row of snapshot.closed ?? []) {
    if (!row?.symbol) continue;
    const ps = (row.side ?? "").trim().toUpperCase();
    if (ps !== "LONG" && ps !== "SHORT") continue;
    const close = finiteNumber(row.closed);
    const open = finiteNumber(row.opened) ?? (close != null && row.heldMs != null ? close - row.heldMs : null);
    if (open == null || close == null || close < open) continue;
    const list = map.get(row.symbol) ?? [];
    list.push({ open, close, ps });
    map.set(row.symbol, list);
  }
  return map;
}

function orderRealizedPnl(order: TelemetryOrder | OrderBatch): number | null {
  const raw = (order as OrderBatch).totalPnl ?? order.pnl;
  const val = finiteNumber(raw);
  if (val == null) return null;
  if (order.pnlKnown === false) return null;
  if (order.pnlKnown === true) return val;
  // Treat zero as unknown unless pnlKnown is explicitly true? Wait: in BAPI raw orders/batches,
  // entries often arrive with pnl=0; treat non-zero as strong close signal and zero as zero-PnL cue.
  return val;
}

function classifyNormalized(
  items: NormalizedOrder[],
  snapshot: LeadSnapshot,
): Map<number, string> {
  const result = new Map<number, string>();
  if (!items.length) return result;

  const confirmed = positionsConfirmed(snapshot);
  const closedBySym = normalizeClosedRows(snapshot);

  const openHedge = new Map<string, number>();
  const openOneWay = new Map<string, number>();
  if (confirmed) {
    for (const pos of snapshot.openPositions ?? []) {
      const sym = pos.symbol ?? pos.instId;
      const qty = finiteNumber(pos.qty ?? pos.pos);
      const ps = (pos.positionSide ?? pos.posSide ?? "").trim().toUpperCase();
      if (!sym || qty == null || qty <= 0) continue;
      if (ps === "LONG" || ps === "SHORT") {
        const key = `${sym}|${ps}`;
        openHedge.set(key, (openHedge.get(key) ?? 0) + qty);
        openOneWay.set(sym, (openOneWay.get(sym) ?? 0) + (ps === "LONG" ? qty : -qty));
      } else if (ps === "BOTH" || ps === "NET") {
        const side = pos.qty != null ? (pos.qty >= 0 ? "LONG" : "SHORT") : null;
        if (side) {
          openOneWay.set(sym, (openOneWay.get(sym) ?? 0) + qty);
        }
      }
    }
  }

  const hedgeGroups = new Map<string, NormalizedOrder[]>();
  const oneWayGroups = new Map<string, NormalizedOrder[]>();

  for (const item of items) {
    if (item.presetAction && !isUnresolvedDirection(item.presetAction)) {
      result.set(item.index, item.presetAction);
    }
    if (item.ps === "LONG" || item.ps === "SHORT") {
      const key = `${item.sym}|${item.ps}`;
      const list = hedgeGroups.get(key) ?? [];
      list.push(item);
      hedgeGroups.set(key, list);
    } else {
      const list = oneWayGroups.get(item.sym) ?? [];
      list.push(item);
      oneWayGroups.set(item.sym, list);
    }
  }

  for (const [key, list] of hedgeGroups.entries()) {
    list.sort((a, b) => a.ut - b.ut || a.ot - b.ot || a.index - b.index);
    const [sym, ps] = key.split("|") as [string, "LONG" | "SHORT"];
    if (confirmed) {
      let after = openHedge.get(key) ?? 0;
      for (let i = list.length - 1; i >= 0; i--) {
        const item = list[i];
        const opening = isHedgeOpen(item.side, ps);
        if (opening) {
          const before = after - item.qty;
          if (before < -EPS) break;
          if (!result.has(item.index)) {
            result.set(item.index, before <= EPS ? `OPEN_${ps}` : `ADD_${ps}`);
          }
          after = Math.max(0, before);
        } else {
          if (!result.has(item.index)) {
            result.set(item.index, after <= EPS ? `CLOSE_${ps}` : `REDUCE_${ps}`);
          }
          after = after + item.qty;
        }
      }
    }

    let net = 0;
    const symCycles = (closedBySym.get(sym) ?? []).filter(c => c.ps === ps);
    for (let idx = 0; idx < list.length; idx++) {
      const item = list[idx];
      const opening = isHedgeOpen(item.side, ps);
      const ts = item.ut || item.ot;
      if (!result.has(item.index)) {
        for (const c of symCycles) {
          if (c.open - 2000 <= ts && ts <= c.close + 2000) {
            if (opening) {
              result.set(item.index, Math.abs(item.ot - c.open) <= 2000 ? `OPEN_${ps}` : `ADD_${ps}`);
            } else {
              result.set(item.index, Math.abs(ts - c.close) <= 2000 ? `CLOSE_${ps}` : `REDUCE_${ps}`);
            }
            break;
          }
        }
      }
      if (!result.has(item.index)) {
        if (opening) {
          result.set(item.index, net <= EPS ? `OPEN_${ps}` : `ADD_${ps}`);
        } else {
          const nextNet = net - item.qty;
          if (net > EPS) {
            result.set(item.index, nextNet <= EPS ? `CLOSE_${ps}` : `REDUCE_${ps}`);
          } else {
            const laterExitOnly = list.slice(idx + 1).some(l => !isHedgeOpen(l.side, ps))
              && !list.slice(idx + 1).some(l => isHedgeOpen(l.side, ps));
            result.set(item.index, laterExitOnly ? `REDUCE_${ps}` : `CLOSE_${ps}`);
          }
        }
      }
      if (opening) net += item.qty;
      else net = Math.max(0, net - item.qty);
    }
  }

  for (const [sym, list] of oneWayGroups.entries()) {
    list.sort((a, b) => a.ut - b.ut || a.ot - b.ot || a.index - b.index);
    const symCycles = closedBySym.get(sym) ?? [];

    const matchClosed = (item: NormalizedOrder): { act: string; atClose: boolean } | null => {
      const ts = item.ut || item.ot;
      const ot = item.ot || ts;
      for (const c of symCycles) {
        if ((c.open - 2000 <= ot && ot <= c.close + 2000) || (c.open - 2000 <= ts && ts <= c.close + 2000)) {
          const opening = (c.ps === "LONG" && item.side === "BUY") || (c.ps === "SHORT" && item.side === "SELL");
          if (opening) {
            return { act: Math.abs(ot - c.open) <= 2000 ? `OPEN_${c.ps}` : `ADD_${c.ps}`, atClose: false };
          }
          const atClose = Math.abs(ts - c.close) <= 2000;
          return { act: atClose ? `CLOSE_${c.ps}` : `REDUCE_${c.ps}`, atClose };
        }
      }
      return null;
    };

    let after: number | null = confirmed ? (openOneWay.get(sym) ?? 0) : null;
    for (let i = list.length - 1; i >= 0; i--) {
      const item = list[i];
      if (after == null) {
        const matched = matchClosed(item);
        if (matched) {
          if (!result.has(item.index)) result.set(item.index, matched.act);
          if (matched.atClose) {
            const delta = item.side === "BUY" ? item.qty : -item.qty;
            after = -delta;
          }
        }
        continue;
      }

      const delta = item.side === "BUY" ? item.qty : -item.qty;
      const before = after - delta;
      if (((before > EPS && after < -EPS) || (before < -EPS && after > EPS)) && item.pnl === 0) {
        if (!result.has(item.index)) {
          result.set(item.index, item.side === "SELL" ? "OPEN_SHORT" : "OPEN_LONG");
        }
        after = null;
        continue;
      }

      if (Math.abs(before) <= EPS && Math.abs(after) <= EPS) {
        after = before;
        continue;
      }

      if (!result.has(item.index)) {
        result.set(item.index, oneWayActionFromState(before, after, item.side));
      }
      after = before;
    }

    let net = 0;
    for (let idx = 0; idx < list.length; idx++) {
      const item = list[idx];
      const existing = result.get(item.index);
      if (existing) {
        if (existing === "CLOSE_SHORT" || existing === "CLOSE_LONG") net = 0;
        else if (existing === "REDUCE_SHORT") net = net < -EPS ? net + item.qty : net;
        else if (existing === "REDUCE_LONG") net = net > EPS ? net - item.qty : net;
        else if (existing === "OPEN_LONG") net = item.qty;
        else if (existing === "ADD_LONG") net += item.qty;
        else if (existing === "OPEN_SHORT") net = -item.qty;
        else if (existing === "ADD_SHORT") net -= item.qty;
        continue;
      }

      const matched = matchClosed(item);
      if (matched) {
        result.set(item.index, matched.act);
        if (matched.atClose) net = 0;
        else net += item.side === "BUY" ? item.qty : -item.qty;
        continue;
      }

      if (item.side === "BUY") {
        if (net < -EPS && (item.pnl == null || item.pnl !== 0)) {
          const nextNet = net + item.qty;
          if (nextNet < -EPS) {
            result.set(item.index, "REDUCE_SHORT");
            net = nextNet;
          } else if (Math.abs(nextNet) <= EPS) {
            result.set(item.index, "CLOSE_SHORT");
            net = 0;
          } else {
            const moreShort = list.slice(idx + 1, idx + 4).some(l => l.side === "BUY" && l.pnl != null && l.pnl !== 0)
              && list.slice(idx + 1, idx + 3).some(l => l.side === "SELL" && (l.pnl == null || l.pnl === 0));
            result.set(item.index, moreShort ? "REDUCE_SHORT" : "CLOSE_SHORT");
            net = 0;
          }
        } else if (item.pnl != null && item.pnl !== 0) {
          const nextSameClose = idx + 1 < list.length && list[idx + 1].side === "BUY"
            && list[idx + 1].pnl != null && list[idx + 1].pnl !== 0;
          result.set(item.index, nextSameClose ? "REDUCE_SHORT" : "CLOSE_SHORT");
          net = 0;
        } else {
          result.set(item.index, net <= EPS ? "OPEN_LONG" : "ADD_LONG");
          net = Math.max(0, net) + item.qty;
        }
      } else {
        if (net > EPS && (item.pnl == null || item.pnl !== 0)) {
          const nextNet = net - item.qty;
          if (nextNet > EPS) {
            result.set(item.index, "REDUCE_LONG");
            net = nextNet;
          } else if (Math.abs(nextNet) <= EPS) {
            result.set(item.index, "CLOSE_LONG");
            net = 0;
          } else {
            const moreLong = list.slice(idx + 1, idx + 4).some(l => l.side === "SELL" && l.pnl != null && l.pnl !== 0)
              && list.slice(idx + 1, idx + 3).some(l => l.side === "BUY" && (l.pnl == null || l.pnl === 0));
            result.set(item.index, moreLong ? "REDUCE_LONG" : "CLOSE_LONG");
            net = 0;
          }
        } else if (item.pnl != null && item.pnl !== 0) {
          const nextSameClose = idx + 1 < list.length && list[idx + 1].side === "SELL"
            && list[idx + 1].pnl != null && list[idx + 1].pnl !== 0;
          result.set(item.index, nextSameClose ? "REDUCE_LONG" : "CLOSE_LONG");
          net = 0;
        } else {
          result.set(item.index, net >= -EPS ? "OPEN_SHORT" : "ADD_SHORT");
          net = Math.min(0, net) - item.qty;
        }
      }
    }
  }

  return result;
}

function reconstructClientCycles(orders: TelemetryOrder[]): TradeCycle[] {
  const chronological = [...orders].sort(
    (a, b) => ((a.time ?? a.orderTime ?? a.t ?? 0) - (b.time ?? b.orderTime ?? b.t ?? 0)),
  );
  const groups = new Map<string, TelemetryOrder[]>();
  for (const order of chronological) {
    const sym = order.symbol;
    if (!sym) continue;
    const ps = inferPosSide(order);
    if (ps !== "LONG" && ps !== "SHORT") continue;
    const key = `${sym}|${ps}`;
    const list = groups.get(key) ?? [];
    list.push(order);
    groups.set(key, list);
  }

  const cycles: TradeCycle[] = [];
  for (const [key, list] of groups.entries()) {
    const [sym, ps] = key.split("|") as [string, "LONG" | "SHORT"];
    let cycleIdx = 1;
    let current: TradeCycle | null = null;
    let netQty = 0;
    let totalEntryQty = 0;
    let totalExitQty = 0;
    let entryNotional = 0;
    let exitNotional = 0;

    for (const order of list) {
      const qty = finiteNumber(order.qty ?? order.q) ?? 0;
      const px = finiteNumber(order.avgPrice ?? order.price ?? order.p) ?? 0;
      const pnl = finiteNumber(order.pnl) ?? 0;
      const t = finiteNumber(order.time ?? order.t ?? order.orderTime) ?? 0;
      if (qty <= 0 || px <= 0) continue;

      const dir = (order.dir ?? "").trim().toUpperCase();
      const isOpen = dir.startsWith("OPEN_") || dir.startsWith("ADD_");
      const explicitClose = dir.startsWith("CLOSE_");

      if (!current) {
        if (!isOpen) continue;
        totalEntryQty = qty;
        totalExitQty = 0;
        entryNotional = qty * px;
        exitNotional = 0;
        netQty = qty;
        current = {
          cycleId: `${sym}-${ps} #${cycleIdx}`,
          symbol: sym,
          positionSide: ps,
          status: "OPEN",
          openedAt: t,
          startTime: t,
          endTime: t,
          durationMs: 0,
          durationSec: 0,
          entryCount: 1,
          scaleInCount: 0,
          exitCount: 0,
          totalEntryQty: qty,
          totalExitQty: 0,
          avgEntryPrice: px,
          avgExitPrice: 0,
          realizedPnl: 0,
        };
      } else {
        current.endTime = t;
        const start = current.startTime ?? current.openedAt ?? t;
        const durMs = Math.max(0, t - start);
        current.durationMs = durMs;
        current.durationSec = Math.floor(durMs / 1000);
        if (isOpen) {
          current.entryCount = (current.entryCount ?? 0) + 1;
          current.scaleInCount = (current.scaleInCount ?? 0) + 1;
          totalEntryQty += qty;
          entryNotional += qty * px;
          current.totalEntryQty = totalEntryQty;
          current.avgEntryPrice = totalEntryQty > 0 ? entryNotional / totalEntryQty : px;
          netQty += qty;
        } else {
          current.exitCount = (current.exitCount ?? 0) + 1;
          totalExitQty += qty;
          exitNotional += qty * px;
          current.totalExitQty = totalExitQty;
          current.avgExitPrice = totalExitQty > 0 ? exitNotional / totalExitQty : px;
          current.realizedPnl = Math.round(((current.realizedPnl ?? 0) + pnl) * 100) / 100;
          netQty -= qty;
        }

        if ((netQty <= 1e-6 || explicitClose) && totalExitQty > 0) {
          current.status = "CLOSED";
          cycles.push(current);
          current = null;
          netQty = 0;
          cycleIdx += 1;
        }
      }
    }

    if (current) {
      cycles.push(current);
    }
  }

  cycles.sort(
    (a, b) => ((b.endTime ?? b.openedAt ?? b.startTime ?? 0) - (a.endTime ?? a.openedAt ?? a.startTime ?? 0)),
  );
  return cycles;
}

export function enrichSnapshotHistory(snapshot: LeadSnapshot): {
  orders: TelemetryOrder[];
  orderBatches: OrderBatch[];
  tradeCycles: TradeCycle[];
} {
  const rawOrders = snapshot.orders ?? [];
  const rawBatches = snapshot.orderBatches ?? [];

  // 1. Classify raw orders
  const normalizedOrders: NormalizedOrder[] = [];
  rawOrders.forEach((order, index) => {
    const sym = order.symbol ?? "";
    const side = inferTradeSide(order);
    const qty = finiteNumber(order.qty ?? order.q);
    if (!sym || !side || qty == null || qty <= 0) return;
    const price = finiteNumber(order.avgPrice ?? order.price ?? order.p) ?? 0;
    const ot = finiteNumber(order.orderTime ?? order.ot ?? order.time ?? order.t) ?? 0;
    const ut = finiteNumber(order.time ?? order.t ?? ot) ?? ot;
    normalizedOrders.push({
      index,
      sym,
      side,
      ps: inferPosSide(order),
      qty,
      price,
      ot,
      ut,
      pnl: orderRealizedPnl(order),
      presetAction: isUnresolvedDirection(order.dir) ? null : order.dir!.trim().toUpperCase(),
    });
  });

  const orderActions = classifyNormalized(normalizedOrders, snapshot);
  const enrichedOrders: TelemetryOrder[] = rawOrders.map((order, index) => {
    const inferred = orderActions.get(index);
    if (inferred && isUnresolvedDirection(order.dir)) {
      return { ...order, dir: inferred };
    }
    return order;
  });

  // 2. Classify batches directly (handles snapshots where batches hold unresolved BUY/SELL)
  const normalizedBatches: NormalizedOrder[] = [];
  rawBatches.forEach((batch, index) => {
    const sym = batch.symbol ?? "";
    const side = inferTradeSide(batch);
    const qty = finiteNumber(batch.qty ?? batch.totalQty);
    if (!sym || !side || qty == null || qty <= 0) return;
    const price = finiteNumber(batch.avgPrice ?? batch.price ?? batch.p) ?? 0;
    const ot = finiteNumber(batch.startTime ?? batch.orderTime ?? batch.time) ?? 0;
    const ut = finiteNumber(batch.endTime ?? batch.time ?? ot) ?? ot;
    normalizedBatches.push({
      index,
      sym,
      side,
      ps: inferPosSide(batch),
      qty,
      price,
      ot,
      ut,
      pnl: orderRealizedPnl(batch),
      presetAction: isUnresolvedDirection(batch.dir) ? null : batch.dir!.trim().toUpperCase(),
    });
  });

  const batchActions = classifyNormalized(normalizedBatches, snapshot);
  const enrichedBatches: OrderBatch[] = rawBatches.map((batch, index) => {
    // Also check if child orders matched classified orders in snapshot.orders
    let resolvedDir = isUnresolvedDirection(batch.dir) ? (batchActions.get(index) ?? batch.dir) : batch.dir;
    const rawItems = batch.items ?? (Array.isArray(batch.orders) ? batch.orders : null);
    let nextItems = rawItems;
    if (Array.isArray(rawItems) && rawItems.length) {
      nextItems = rawItems.map(child => {
        if (!isUnresolvedDirection(child.dir)) return child;
        const cTime = finiteNumber(child.time ?? child.orderTime ?? child.t);
        const cQty = finiteNumber(child.qty ?? child.q);
        const matchedOrder = enrichedOrders.find(
          o => o.symbol === (child.symbol ?? batch.symbol)
            && !isUnresolvedDirection(o.dir)
            && finiteNumber(o.time ?? o.orderTime ?? o.t) === cTime
            && (cQty == null || finiteNumber(o.qty ?? o.q) === cQty),
        );
        const dir = matchedOrder?.dir ?? resolvedDir ?? child.dir;
        return dir ? { ...child, dir } : child;
      });
      if (isUnresolvedDirection(resolvedDir)) {
        const itemDirs = nextItems.map(i => i.dir).filter((d): d is string => !!d && !isUnresolvedDirection(d));
        if (itemDirs.length) {
          resolvedDir = itemDirs.find(d => d.startsWith("CLOSE_"))
            ?? itemDirs.find(d => d.startsWith("OPEN_"))
            ?? itemDirs[itemDirs.length - 1];
        }
      }
    }
    return {
      ...batch,
      ...(resolvedDir !== batch.dir ? { dir: resolvedDir } : {}),
      ...(nextItems && batch.items ? { items: nextItems } : {}),
      ...(nextItems && Array.isArray(batch.orders) ? { orders: nextItems } : {}),
    };
  });

  // 3. Reconstruct trade cycles when backend returned none
  let enrichedCycles: TradeCycle[];
  if ((snapshot.tradeCycles?.length ?? 0) > 0) {
    enrichedCycles = snapshot.tradeCycles!.map(c => ({
      ...c,
      openedAt: c.openedAt ?? c.startTime ?? c.endTime ?? null,
    }));
  } else {
    const cycleSource: TelemetryOrder[] = enrichedOrders.length
      ? enrichedOrders
      : enrichedBatches.map(b => ({
          symbol: b.symbol,
          dir: b.dir,
          side: b.side,
          positionSide: b.positionSide,
          time: b.endTime ?? b.startTime ?? b.time,
          orderTime: b.startTime ?? b.time,
          qty: b.qty ?? b.totalQty,
          avgPrice: b.avgPrice ?? b.price,
          pnl: b.totalPnl ?? b.pnl,
        }));
    enrichedCycles = reconstructClientCycles(cycleSource);
  }

  return {
    orders: enrichedOrders,
    orderBatches: enrichedBatches,
    tradeCycles: enrichedCycles,
  };
}
