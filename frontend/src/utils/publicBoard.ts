import type { BoardEvent, BoardLead, BoardPosition, Lead, TradeAction } from "@/types/api";
import { finiteNumber, timestampMs } from "./format";
import { normalizeSide } from "./status";

// 公开接口逐字段投影，不能把私有 mine/orphans 或账户字段透传到组件。
export function publicBoardLead(lead: Lead, now: number): BoardLead {
  const start = Math.floor((now + 8 * 3600000) / 86400000) * 86400000 - 8 * 3600000;
  const positions: BoardPosition[] | null = lead.snapshot?.openPositions?.map(position => {
    const opened = timestampMs(position.openTime);
    const qty = finiteNumber(position.qty ?? position.pos);
    const side = normalizeSide(position.positionSide ?? position.posSide, qty);
    return {
      key: `${position.symbol ?? position.instId}:${side}:${opened}`, symbol: position.symbol ?? position.instId ?? null,
      side: side === "UNKNOWN" ? null : side, qty,
      entryPrice: finiteNumber(position.avgEntry), markPrice: finiteNumber(position.currentPrice ?? position.markPrice),
      pnl: finiteNumber(position.uPnL), pnlPct: finiteNumber(position.uPnLRoePct),
      leverage: finiteNumber(position.notionalLeverage), notional: finiteNumber(position.notional ?? position.markValue),
      openedAt: opened, openedToday: opened == null ? null : opened >= start && opened <= now,
      takeProfitPrice: null, stopLossPrice: null, mine: null,
    };
  }) ?? null;
  const events: BoardEvent[] | null = lead.snapshot?.orders?.flatMap(order => {
    const time = timestampMs(order.time ?? order.t);
    const parts = order.dir?.toUpperCase().split("_") ?? [];
    const action = parts[0];
    if (time == null || time < start || time > now || !["OPEN", "ADD", "REDUCE", "CLOSE"].includes(action ?? "")) return [];
    const side = normalizeSide(parts[1]);
    return [{
      id: order.id ?? order.orderId ?? null, time, symbol: order.symbol ?? null,
      side: side === "UNKNOWN" ? null : side, action: action as TradeAction,
      qty: finiteNumber(order.qty ?? order.q), price: finiteNumber(order.avgPrice ?? order.price ?? order.p),
      realizedPnl: finiteNumber(order.pnl), mine: null,
    }];
  }) ?? null;
  return {
    id: lead.id, name: lead.name, avatarUrl: lead.avatarUrl ?? lead.snapshot?.profile?.avatarUrl ?? null,
    status: lead.status ?? null, enabled: lead.isEnabled ?? null,
    stale: lead.stale ?? lead.snapshot?.meta?.stale ?? null,
    snapshotAgeSec: lead.snapshotAgeSec ?? lead.snapshot?.meta?.snapshotAgeSec ?? null,
    positionShow: lead.positionShow ?? lead.snapshot?.profile?.positionShow ?? null,
    // 公开快照订单可能截断，不能据此宣称全天笔数或已实现总额；绑定数始终为 null。
    boundCount: null, activeBoundCount: null,
    todayOpenCount: null, todayCloseCount: null, todayRealizedPnl: null,
    positions, todayEvents: events, orphans: null,
  };
}
