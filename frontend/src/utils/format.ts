// 所有格式化入口保留未知值，不将空字符串或 null 当成零。
export type NumericValue = number | string | null | undefined;

export interface DateTimeOptions {
  withSeconds?: boolean;
  short?: boolean;
  timeZone?: string;
}

// 旧 SQLite 字符串没有时区时按 UTC 解析，秒和毫秒时间戳都接受。
export function timestampMs(value: Date | string | number | null | undefined): number | null {
  if (value == null || value === "" || (typeof value === "string" && !value.trim())) return null;
  const stamp = value instanceof Date ? value.getTime() : typeof value === "number"
    ? (Math.abs(value) < 1e11 ? value * 1000 : value)
    : Date.parse(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(value.trim()) ? value.trim() : `${value.trim()}Z`);
  return Number.isFinite(stamp) ? stamp : null;
}

// 日期输出统一为北京时间；显式传入时区可供导出和回归测试使用。
export function formatDateTime(value: Date | string | number | null | undefined, options: DateTimeOptions = {}): string {
  const stamp = timestampMs(value);
  if (stamp == null) return "—";
  const date = new Date(stamp);
  try {
    const parts = new Intl.DateTimeFormat("en-GB", {
      timeZone: options.timeZone || "Asia/Shanghai",
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
    }).formatToParts(date);
    const part = (type: Intl.DateTimeFormatPartTypes) => parts.find(p => p.type === type)?.value;
    const day = `${part("month")}-${part("day")}`;
    const time = `${part("hour")}:${part("minute")}${options.withSeconds === false ? "" : `:${part("second")}`}`;
    return `${options.short ? day : `${part("year")}-${day}`} ${time}`;
  } catch {
    return "—";
  }
}

// 只取北京时间的时分秒：同一天内的动态与状态条时钟不需要重复日期。
export function formatClock(value: Date | string | number | null | undefined, withSeconds = true): string {
  const text = formatDateTime(value, { withSeconds });
  return text === "—" ? text : text.slice(text.indexOf(" ") + 1);
}

export function finiteNumber(value: NumericValue): number | null {
  if (value == null || (typeof value === "string" && !value.trim())) return null;
  const number = Number(value);
  return Number.isFinite(number) ? (Object.is(number, -0) ? 0 : number) : null;
}

function decimals(value: number, fallback: number, maximum = 20): number {
  return Number.isFinite(value) ? Math.max(0, Math.min(maximum, Math.trunc(value))) : fallback;
}

// 价格限制小数位，避免小币种价格撑破数字列。
export function formatPrice(value: NumericValue, prefix = "$", maxDecimals?: number): string {
  const number = finiteNumber(value);
  if (number === null) return "—";
  const absolute = Math.abs(number);
  const limit = absolute === 0 || absolute >= 1 ? 2 : absolute < 0.0001 ? 6 : 4;
  const maximum = maxDecimals === undefined ? limit : Math.max(2, decimals(maxDecimals, limit, limit));
  return (number < 0 ? "-" : "") + prefix + absolute.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: maximum,
  });
}

export function formatQty(value: NumericValue): string {
  const number = finiteNumber(value);
  if (number === null) return "—";
  return number.toLocaleString("en-US", Math.abs(number) > 0 && Math.abs(number) < 0.001
    ? { maximumSignificantDigits: 4 }
    : { maximumFractionDigits: 4 });
}

export function signedUsd(value: NumericValue): string {
  const number = finiteNumber(value);
  return number === null ? "—" : `${number > 0 ? "+" : ""}${formatUsd(number)}`;
}

export function baseAsset(symbol?: string | null): string {
  return symbol ? symbol.toUpperCase().replace(/[-_/]/g, "").replace(/SWAP$/, "").replace(/(?:USDT|USDC)$/, "") : "—";
}

export function formatUsd(value: NumericValue, digits = 2): string {
  const number = finiteNumber(value);
  if (number === null) return "—";
  const precision = decimals(digits, 2);
  return number.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: precision,
    maximumFractionDigits: precision,
  });
}

// 输入已经是百分点，例如 0.8 表示 0.80%。
export function formatPct(value: NumericValue, digits = 2, signed = true): string {
  const number = finiteNumber(value);
  if (number === null) return "—";
  const precision = decimals(digits, 2);
  return (signed && number > 0 ? "+" : "") + number.toLocaleString("en-US", {
    minimumFractionDigits: precision,
    maximumFractionDigits: precision,
  }) + "%";
}

// 杠杆倍数统一保留两位小数并带 x；未知保留破折号，不补零。
export function formatLeverage(value: NumericValue, digits = 2): string {
  const number = finiteNumber(value);
  return number === null ? "—" : `${number.toFixed(decimals(digits, 2))}x`;
}
