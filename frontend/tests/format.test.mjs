import assert from "node:assert/strict";
import test from "node:test";
import { loadTs } from "./load-ts.mjs";
const { formatPrice, formatQty, formatUsd, formatPct } = await loadTs("format");

test("price decimals are bounded at each threshold", () => {
  for (const [input, expected] of [
    [167.86281897742975, "$167.86"], [1, "$1.00"], [12345.67890123, "$12,345.68"],
    [0.123456789, "$0.1235"], [0.0001, "$0.0001"], [0.0000123456789, "$0.000012"],
    [1e-9, "$0.00"], [0, "$0.00"], [-0, "$0.00"], [-167.8628189, "-$167.86"],
    ["167.86281897742975", "$167.86"], [-0.123456789, "-$0.1235"],
  ]) assert.equal(formatPrice(input), expected);
  assert.equal(formatPrice(167.862818977, "", 12), "167.86");
  assert.equal(formatPrice(0.123456, "€", 3), "€0.123");
  assert.equal(formatPrice(0.123456, "$", 1), "$0.12");
});

test("invalid and empty values never display long floats or NaN", () => {
  for (const value of [null, undefined, NaN, Infinity, -Infinity, "", " ", "garbage", "Infinity"]) {
    assert.equal(formatPrice(value), "—");
    assert.equal(formatUsd(value), "—");
    assert.equal(formatPct(value), "—");
    // 未知数量不能冒充零仓位。
    assert.equal(formatQty(value), "—");
  }
});

test("quantities remove trailing zeros and amounts and percentages keep two decimals", () => {
  assert.equal(formatQty(2.123456789), "2.1235");
  assert.equal(formatQty("2.1000"), "2.1");
  assert.equal(formatQty(1234567.123456789), "1,234,567.1235");
  assert.equal(formatQty(0.00000123456), "0.000001235");
  assert.equal(formatUsd(11.60123456), "$11.60");
  assert.equal(formatUsd(-11.60123456), "-$11.60");
  assert.equal(formatUsd(11.6, 0), "$12");
  assert.equal(formatPct(.801234), "+0.80%");
  assert.equal(formatPct(-.801234), "-0.80%");
  assert.equal(formatPct(.801234, 2, false), "0.80%");
  assert.equal(formatPct(0), "0.00%");
});

const { formatDateTime } = await loadTs("format");

test("all supported timestamp inputs render Beijing time regardless of browser timezone", () => {
  const iso = "2026-11-14T14:38:19.655Z";
  const stamp = Date.parse(iso);
  const original = process.env.TZ;
  try {
    for (const timeZone of ["UTC", "America/Los_Angeles", "Asia/Shanghai"]) {
      process.env.TZ = timeZone;
      for (const input of [iso, iso.slice(0, -1), stamp, stamp / 1000, new Date(stamp), "2026-11-14T22:38:19.655+08:00", "2026-11-14T22:38:19.655+0800"]) {
        assert.equal(formatDateTime(input), "2026-11-14 22:38:19");
      }
    }
  } finally {
    if (original === undefined) delete process.env.TZ;
    else process.env.TZ = original;
  }
});

test("date formatting handles midnight, short output, seconds and explicit timezone", () => {
  assert.equal(formatDateTime("2026-11-14T16:00:00"), "2026-11-15 00:00:00");
  assert.equal(formatDateTime("2026-11-14T14:38:19Z", { short: true }), "11-14 22:38:19");
  assert.equal(formatDateTime("2026-11-14T14:38:19Z", { withSeconds: false }), "2026-11-14 22:38");
  assert.equal(formatDateTime("2026-11-14T14:38:19Z", { timeZone: "UTC" }), "2026-11-14 14:38:19");
  for (const input of [null, undefined, "", " ", "invalid", NaN, Infinity, new Date(NaN)]) assert.equal(formatDateTime(input), "—");
});
