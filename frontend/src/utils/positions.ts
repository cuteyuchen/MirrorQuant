import type { LeadSnapshot } from "@/types/api";

// 与看板同一条规则：持仓未公开（或公开状态未知）时，空数组不代表已确认空仓。
export function positionsConfirmed(snapshot?: LeadSnapshot | null): boolean {
  const list = snapshot?.openPositions;
  if (Array.isArray(list)) return list.length > 0 || snapshot?.profile?.positionShow === true;
  // 直连接口已确认空仓：即使列表缺失也不再报“数据尚不可用”，而是按空仓处理。
  const meta = snapshot?.openPositionsMeta;
  return meta?.authoritative === true && meta?.emptyVerified === true;
}

// 北京时间当天零点的毫秒时间戳，用来判断“今日新开”。
export function beijingDayStart(now: number): number {
  return Math.floor((now + 8 * 3600000) / 86400000) * 86400000 - 8 * 3600000;
}
