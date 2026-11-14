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
    assert.equal(formatQty(value), "0");
  }
});

test("quantities remove trailing zeros and amounts and percentages keep two decimals", () => {
  assert.equal(formatQty(2.123456789), "2.1235");
  assert.equal(formatQty("2.1000"), "2.1");
  assert.equal(formatQty(1234567.123456789), "1,234,567.1235");
  assert.equal(formatUsd(11.60123456), "$11.60");
  assert.equal(formatUsd(-11.60123456), "-$11.60");
  assert.equal(formatUsd(11.6, 0), "$12");
  assert.equal(formatPct(.801234), "+0.80%");
  assert.equal(formatPct(-.801234), "-0.80%");
  assert.equal(formatPct(.801234, 2, false), "0.80%");
  assert.equal(formatPct(0), "0.00%");
});
