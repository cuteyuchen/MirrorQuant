import { describe, expect, it } from "vitest";
import type { LegacyPosition } from "@/types/api";
import { calculateSizing } from "@/utils/sizing";

// 测算只消费已知行情，缺失或非法输入不得产生可复制的下单计划。
const position: LegacyPosition = { symbol: "BTCUSDT", positionSide: "LONG", qty: 0.2, avgEntry: 68000, markPrice: 69000 };

describe("仓位计算器失败关闭", () => {
  it("已知价格时保留原比例公式", () => {
    const result = calculateSizing([position], 10000, 1000, 1);
    expect(result.complete).toBe(true);
    expect(result.rows[0].targetNotional).toBeCloseTo(1360);
    expect(result.rows[0].targetQty).toBeCloseTo(0.02);
  });

  it("缺少开仓价或现价时不代入 1，也不生成完整计划", () => {
    for (const input of [{ ...position, avgEntry: null }, { ...position, markPrice: null }, { ...position, avgEntry: 0 }]) {
      const result = calculateSizing([input], 10000, 1000, 1);
      expect(result.complete).toBe(false);
      expect(result.rows[0].targetQty).toBeNull();
      expect(result.totalNotional).toBeNull();
    }
  });

  it("非法本金、倍率与溢出不能被当作完整计划", () => {
    for (const multiplier of [0, -1, NaN, Infinity]) {
      const result = calculateSizing([position], 10000, 1000, multiplier);
      expect(result.complete).toBe(false);
      expect(result.rows[0].targetQty).toBeNull();
    }
    expect(calculateSizing([position], 10000, null, 1).complete).toBe(false);
    expect(calculateSizing([position], 1, Number.MAX_VALUE, 2).complete).toBe(false);
    expect(calculateSizing([{ ...position, avgEntry: Number.MIN_VALUE, markPrice: Number.MIN_VALUE, entryValue: 10000 }], 10000, 1000, 1).complete).toBe(false);
    expect(calculateSizing([{ ...position, entryValue: 1 }], 10000, Number.MIN_VALUE, 1).complete).toBe(false);
  });

  it("单行有限但合计金额、总杠杆或保证金溢出时禁止完整计划", () => {
    const large = ["A", "B"].map(symbol => ({ ...position, symbol, avgEntry: 100, markPrice: 100, notional: 1e308 }));
    for (const result of [calculateSizing(large, 1, 1, 1), calculateSizing(large, 1, 1e-308, 1),
      calculateSizing([{ ...position, mmr: { maxLev: 1e-320 } }], 10000, 1000, 1)]) {
      expect(result.complete).toBe(false);
      expect(result.totalNotional).toBeNull();
      expect(result.totalMargin).toBeNull();
      expect(result.portfolioLeverage).toBeNull();
    }
  });

  it("未知方向不猜为空头，有符号净仓按数量归一化", () => {
    expect(calculateSizing([{ ...position, positionSide: "both", qty: null }], 10000, 1000, 1).complete).toBe(false);
    expect(calculateSizing([{ ...position, positionSide: "net", qty: -0.2 }], 10000, 1000, 1).rows[0].side).toBe("SHORT");
    expect(calculateSizing([{ ...position, positionSide: "lOnG" }], 10000, 1000, 1).rows[0].side).toBe("LONG");
  });
});
