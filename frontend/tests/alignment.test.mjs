import assert from "node:assert/strict";
import test from "node:test";
import { loadTs } from "./load-ts.mjs";
const { getLeadAlignment } = await loadTs("alignment");
const now = Date.now();

function fixture() {
  const lead = { id: 1, isEnabled: true, stale: false, positionsKnown: true };
  return { lead, overview: {
    kpis: {}, leadsStatus: [lead],
    accountsStatus: [{ id: 2, isEnabled: true, status: "connected", lastSyncAt: new Date(now).toISOString() }],
    bindingsStatus: [{ leadId: 1, accountId: 2, isEnabled: true }],
    leadPositions: [{ leadId: 1, symbol: "BTCUSDT", positionSide: "LONG" }],
    followerPositions: [{ accountId: 2, instId: "BTC-USDT-SWAP", posSide: "long", pos: 2 }],
  } };
}

test("alignment normalizes exchange symbols and detects missing or opposite positions", () => {
  const { lead, overview } = fixture();
  assert.equal(getLeadAlignment(lead, overview, now), "aligned");
  overview.followerPositions[0].posSide = "short";
  assert.equal(getLeadAlignment(lead, overview, now), "mismatch");
  overview.followerPositions = [];
  assert.equal(getLeadAlignment(lead, overview, now), "waiting");
  overview.leadPositions = [];
  assert.equal(getLeadAlignment(lead, overview, now), "waiting");
});

test("unbound, paused and stale accounts never appear aligned", () => {
  for (const [mutate, status] of [
    [(l, o) => { o.bindingsStatus = []; }, "unbound"],
    [(l, o) => { o.kpis.globalKillSwitch = true; }, "paused"],
    [(l, o) => { o.bindingsStatus[0].circuitBreakerTripped = true; }, "paused"],
    [(l, o) => { o.accountsStatus[0].isEnabled = false; }, "paused"],
    [(l, o) => { o.accountsStatus[0].status = "error"; }, "stale"],
    [(l, o) => { o.accountsStatus[0].lastSyncAt = new Date(now - 100000).toISOString(); }, "stale"],
    [(l, o) => { o.accountsStatus[0].lastSyncAt = null; }, "stale"],
    [(l, o) => { l.positionsKnown = false; }, "stale"],
    [(l, o) => { l.stale = true; }, "stale"],
    [(l, o) => { l.snapshotIncomplete = true; }, "stale"],
    [(l, o) => { l.isEnabled = false; }, "paused"],
  ]) {
    const { lead, overview } = fixture();
    mutate(lead, overview);
    assert.equal(getLeadAlignment(lead, overview, now), status);
  }
});

test("each bound account is compared separately and symbol maps are respected", () => {
  const { lead, overview } = fixture();
  overview.bindingsStatus[0].symbolMap = { BTCUSDT: "XBTUSDT" };
  overview.followerPositions[0].instId = "XBT-USDT-SWAP";
  assert.equal(getLeadAlignment(lead, overview, now), "aligned");
  overview.accountsStatus.push({ ...overview.accountsStatus[0], id: 3 });
  overview.bindingsStatus.push({ leadId: 1, accountId: 3, isEnabled: true });
  assert.equal(getLeadAlignment(lead, overview, now), "waiting");
});

test("other active leads in the same account do not create false mismatches", () => {
  const { lead, overview } = fixture();
  overview.leadsStatus.push({ ...lead, id: 4 });
  overview.bindingsStatus.push({ leadId: 4, accountId: 2, isEnabled: true });
  overview.leadPositions.push({ leadId: 4, symbol: "ETHUSDT", positionSide: "SHORT" });
  overview.followerPositions.push({ accountId: 2, symbol: "ETHUSDT", posSide: "short", pos: 3 });
  assert.equal(getLeadAlignment(lead, overview, now), "aligned");
  overview.leadsStatus[1].stale = true;
  assert.equal(getLeadAlignment(lead, overview, now), "stale");
});
