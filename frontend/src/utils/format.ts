type NumericValue = number | string | null | undefined;

export interface DateTimeOptions {
  withSeconds?: boolean;
  short?: boolean;
  timeZone?: string;
}

/** API timestamps without an offset are UTC, including legacy SQLite values. */
export function formatDateTime(value: Date | string | number | null | undefined, options: DateTimeOptions = {}): string {
  if (value == null || value === "") return "—";
  let date: Date;
  if (value instanceof Date) {
    date = value;
  } else if (typeof value === "number") {
    date = new Date(Math.abs(value) < 1e11 ? value * 1000 : value);
  } else {
    const iso = value.trim();
    if (!iso) return "—";
    date = new Date(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(iso) ? iso : `${iso}Z`);
  }
  if (!Number.isFinite(date.getTime())) return "—";
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

function finiteNumber(value: NumericValue): number | null {
  if (value == null || (typeof value === "string" && !value.trim())) return null;
  const number = Number(value);
  return Number.isFinite(number) ? (Object.is(number, -0) ? 0 : number) : null;
}

function decimals(value: number, fallback: number, maximum = 20): number {
  return Number.isFinite(value) ? Math.max(0, Math.min(maximum, Math.trunc(value))) : fallback;
}

/** Prices always use bounded decimal notation, including very small tokens. */
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
  return (finiteNumber(value) ?? 0).toLocaleString("en-US", { maximumFractionDigits: 4 });
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

/** The input is already in percentage points (0.8 means 0.80%). */
export function formatPct(value: NumericValue, digits = 2, signed = true): string {
  const number = finiteNumber(value);
  if (number === null) return "—";
  const precision = decimals(digits, 2);
  return (signed && number > 0 ? "+" : "") + number.toLocaleString("en-US", {
    minimumFractionDigits: precision,
    maximumFractionDigits: precision,
  }) + "%";
}
