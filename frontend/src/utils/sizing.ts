import type { LegacyPosition, PositionSummary } from "@/types/api";
import { finiteNumber } from "./format";
import { normalizeSide } from "./status";

// 沿用原测算公式；缺失行情、比例或本金时失败关闭，不用虚构价格填补。
export function calculateSizing(positions: LegacyPosition[], marginBalance: number | null, capital: number | null, multiplier: number, summary?: PositionSummary | null) {
  const positive = (value: number | null | undefined) => {
    const parsed = finiteNumber(value);
    return parsed != null && parsed > 0 ? parsed : null;
  };
  const rows = positions.map(position => {
    const entry = positive(position.avgEntry);
    const mark = positive(position.currentPrice ?? position.markPrice);
    const quantity = finiteNumber(position.qty ?? position.pos);
    const notional = positive(position.entryValue ?? position.notional)
      ?? (quantity != null && entry != null ? Math.abs(quantity) * entry : null);
    const margin = positive(marginBalance);
    const ratio = margin != null && notional != null ? notional / margin
      : positive(position.entryLeverage) ?? positive(position.notionalLeverage)
        ?? (position.sharePct != null && summary?.entryLeverage != null ? positive(position.sharePct / 100 * summary.entryLeverage) : null);
    const side = normalizeSide(position.positionSide ?? position.posSide, quantity);
    // 非法倍率或溢出也属于不可测算，避免 NaN 被当作完整计划。
    const valid = entry != null && mark != null && ratio != null && positive(capital) != null
      && positive(multiplier) != null && side !== "UNKNOWN" && Number.isFinite(capital! * ratio! * multiplier);
    const candidateNotional = valid ? capital! * ratio! * multiplier : null;
    const candidateQty = candidateNotional != null ? candidateNotional / entry! : null;
    // 极小价格或倍率可能导致除法溢出、乘法下溢，不能生成含 Infinity 或伪零的计划。
    const targetNotional = candidateNotional != null && candidateNotional > 0 && candidateQty != null && candidateQty > 0 && Number.isFinite(candidateQty) ? candidateNotional : null;
    const targetQty = targetNotional != null ? candidateQty : null;
    const leverage = targetNotional != null ? Math.min(positive(position.mmr?.maxLev) ?? 50, 50, Math.max(2, Math.ceil(ratio! * multiplier / 0.05))) : null;
    const initialMargin = targetNotional != null && leverage != null ? targetNotional / leverage : null;
    const mmr = finiteNumber(position.mmr?.mmrPct ?? position.maintMarginPct);
    const maintenance = targetNotional != null && mmr != null && mmr >= 0 ? targetNotional * mmr / 100 : null;
    return { key: `${position.symbol ?? position.instId}:${side}:${position.openTime}`, symbol: position.symbol ?? position.instId,
      side, entry, mark, ratio: ratio == null ? null : ratio * multiplier, targetNotional, targetQty, leverage, initialMargin, maintenance };
  });
  // 单行有限不代表合计有限，聚合金额和总杠杆溢出同样禁止复制计划。
  const rowsComplete = rows.length > 0 && rows.every(row =>
    positive(row.targetQty) != null && positive(row.initialMargin) != null && positive(row.ratio) != null);
  const notionalSum = rowsComplete ? positive(rows.reduce((sum, row) => sum + row.targetNotional!, 0)) : null;
  const marginSum = rowsComplete ? positive(rows.reduce((sum, row) => sum + row.initialMargin!, 0)) : null;
  const leverageSum = notionalSum != null && positive(capital) != null ? positive(notionalSum / capital!) : null;
  const complete = rowsComplete && notionalSum != null && marginSum != null && leverageSum != null;
  const totalNotional = complete ? notionalSum : null;
  const totalMargin = complete ? marginSum : null;
  const totalMaintenance = complete && rows.every(row => row.maintenance != null)
    ? finiteNumber(rows.reduce((sum, row) => sum + row.maintenance!, 0)) : null;
  const buffer = totalNotional != null && totalNotional > 0 && capital != null && totalMaintenance != null
    ? finiteNumber(Math.max(0, (capital - totalMaintenance) / totalNotional * 100)) : null;
  return {
    complete, totalNotional, totalMargin, buffer,
    portfolioLeverage: complete ? leverageSum : null,
    freeCash: totalMargin != null && capital != null ? finiteNumber(capital - totalMargin) : null,
    rows: rows.map(row => ({ ...row, liquidation: buffer != null && row.entry != null
      ? finiteNumber(row.side === "SHORT" ? row.entry * (1 + buffer / 100) : Math.max(0, row.entry * (1 - buffer / 100))) : null })),
  };
}
