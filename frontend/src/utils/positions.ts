import type { BoardLead, LeadSnapshot } from "@/types/api";

// 与看板同一条规则：持仓未公开（或公开状态未知）时，空数组不代表已确认空仓。
export function positionsConfirmed(snapshot?: LeadSnapshot | null): boolean {
  const meta = snapshot?.openPositionsMeta;
  if (meta?.emptyVerified === true) return true;
  if (meta?.authoritative === true && meta?.complete === true) return true;
  if (meta?.reconstructed === true && snapshot?.meta?.orderHistoryComplete === true) return true;
  const list = snapshot?.openPositions;
  if (Array.isArray(list)) return list.length > 0 || snapshot?.profile?.positionShow === true;
  return false;
}

export function boardPositionsConfirmed(
  lead?: Pick<BoardLead, "positions" | "positionShow" | "positionsConfirmed"> | null,
): boolean {
  if (!lead) return false;
  if (lead.positionsConfirmed === true) return true;
  if (lead.positionsConfirmed === false) return false;
  return Array.isArray(lead.positions) && (lead.positions.length > 0 || lead.positionShow === true);
}

// 北京时间当天零点的毫秒时间戳，用来判断“今日新开”。
export function beijingDayStart(now: number): number {
  return Math.floor((now + 8 * 3600000) / 86400000) * 86400000 - 8 * 3600000;
}
