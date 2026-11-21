"""Public leaders publish leverage; open/add follows the leader's leverage, hidden leaders fall back to dynamic leverage.

User rule (2026-11-22): a public leader's open uses the leader's leverage; a hidden
leader's leverage is not visible, so the system computes dynamic leverage.
"""
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.domain import ExecutionRecord
from app.services.adapters.base import ExchangePosition
from app.services.engine.router import (
    _effective_open_leverage,
    _lead_position_leverage,
)
from app.services.lead_collector.binance_bapi import normalize_holdings
from test_router_remediations import NOW_MS, OfflineExchange, trading  # noqa: F401  (fixture reuse)


# ---------------------------------------------------------------------------
# 1. normalize_holdings keeps leverage (public holdings only).
# ---------------------------------------------------------------------------

def test_normalize_holdings_keeps_public_leverage():
    holdings = normalize_holdings([{
        "symbol": "ZECUSDT", "positionAmount": "-50", "positionSide": "SHORT",
        "entryPrice": "783.66", "leverage": 49,
    }])
    assert holdings[0]["leverage"] == 49
    assert holdings[0]["positionSide"] == "SHORT"
    assert holdings[0]["qty"] == 50


@pytest.mark.parametrize("raw", [None, "49", 49.0, "49.9"])
def test_normalize_holdings_leverage_parses_to_int(raw):
    holdings = normalize_holdings([{
        "symbol": "ZECUSDT", "positionAmount": "-50", "positionSide": "SHORT",
        "entryPrice": "783.66", "leverage": raw,
    }])
    assert holdings[0]["leverage"] == (49 if raw is not None else None)


@pytest.mark.parametrize("raw", [0, -5, "abc", [], {}, "nan", "inf"])
def test_normalize_holdings_bad_leverage_becomes_none_without_failing_snapshot(raw):
    # Leverage is informational only: a bad value must never invalidate the snapshot.
    holdings = normalize_holdings([{
        "symbol": "ZECUSDT", "positionAmount": "-50", "positionSide": "SHORT",
        "entryPrice": "783.66", "leverage": raw,
    }])
    assert holdings[0]["leverage"] is None
    assert holdings[0]["qty"] == 50


def test_normalize_holdings_missing_leverage_is_none():
    holdings = normalize_holdings([{
        "symbol": "ZECUSDT", "positionAmount": "-50", "positionSide": "SHORT",
        "entryPrice": "783.66",
    }])
    assert holdings[0]["leverage"] is None


def test_normalize_holdings_still_rejects_malformed_rows():
    # The leverage change must not weaken the existing fail-closed behaviour.
    with pytest.raises(ValueError):
        normalize_holdings([{"symbol": "BTCUSDT", "entryPrice": "100"}])
    with pytest.raises(ValueError):
        normalize_holdings([{"symbol": "BTCUSDT", "positionAmount": "1", "positionSide": "SIDEWAYS", "entryPrice": "100"}])


# ---------------------------------------------------------------------------
# 2. _lead_position_leverage reads leverage from a fresh snapshot.
# ---------------------------------------------------------------------------

def _snapshot(positions):
    return {"openPositions": positions}


def test_lead_position_leverage_hit():
    snap = _snapshot([{"symbol": "ZECUSDT", "positionSide": "SHORT", "qty": 50, "leverage": 49}])
    assert _lead_position_leverage(snap, "ZECUSDT", "SHORT") == 49


@pytest.mark.parametrize("symbol,side", [
    ("ETHUSDT", "SHORT"),   # symbol absent
    ("ZECUSDT", "LONG"),    # side mismatch
])
def test_lead_position_leverage_miss_returns_none(symbol, side):
    snap = _snapshot([{"symbol": "ZECUSDT", "positionSide": "SHORT", "qty": 50, "leverage": 49}])
    assert _lead_position_leverage(snap, symbol, side) is None


@pytest.mark.parametrize("positions", [[], None, "not-a-list"])
def test_lead_position_leverage_empty_snapshot_returns_none(positions):
    assert _lead_position_leverage({"openPositions": positions}, "ZECUSDT", "SHORT") is None


def test_lead_position_leverage_malformed_snapshot_returns_none():
    assert _lead_position_leverage(None, "ZECUSDT", "SHORT") is None
    assert _lead_position_leverage({}, "ZECUSDT", "SHORT") is None
    assert _lead_position_leverage("junk", "ZECUSDT", "SHORT") is None


@pytest.mark.parametrize("raw", [None, 0, -3, "abc", "nan"])
def test_lead_position_leverage_bad_value_returns_none(raw):
    snap = _snapshot([{"symbol": "ZECUSDT", "positionSide": "SHORT", "qty": 50, "leverage": raw}])
    assert _lead_position_leverage(snap, "ZECUSDT", "SHORT") is None


def test_lead_position_leverage_ignores_non_dict_rows():
    snap = _snapshot(["junk", {"symbol": "ZECUSDT", "positionSide": "SHORT", "qty": 50, "leverage": 49}])
    assert _lead_position_leverage(snap, "ZECUSDT", "SHORT") == 49


# ---------------------------------------------------------------------------
# 3. _effective_open_leverage priority: existing > lead > dynamic.
# ---------------------------------------------------------------------------

def test_effective_leverage_existing_position_wins_over_lead():
    # Existing local leverage wins even when a lead leverage is supplied.
    assert _effective_open_leverage(10, target_notional=100, equity=1000,
                                    max_symbol_leverage=100, lead_leverage=49) == 10
    assert _effective_open_leverage(50, target_notional=100, equity=1000,
                                    max_symbol_leverage=20, lead_leverage=49) == 20


def test_effective_leverage_uses_lead_when_no_existing_position():
    # No local position + valid lead leverage -> use the leader's leverage.
    assert _effective_open_leverage(None, target_notional=100, equity=1000,
                                    max_symbol_leverage=100, lead_leverage=49) == 49


def test_effective_leverage_caps_lead_to_tier_max():
    # Lead leverage above the tier max is capped, same as existing leverage.
    assert _effective_open_leverage(None, target_notional=100, equity=1000,
                                    max_symbol_leverage=20, lead_leverage=49) == 20


@pytest.mark.parametrize("lead", [None, 0, -5, "abc", float("nan")])
def test_effective_leverage_falls_back_to_dynamic_without_valid_lead(lead):
    # No local position and no usable lead leverage -> dynamic calculation,
    # identical to the old behaviour (100/(1000*0.08) rounds up to 2, floor 10).
    assert _effective_open_leverage(None, target_notional=100, equity=1000,
                                    max_symbol_leverage=100, lead_leverage=lead) == 10


# ---------------------------------------------------------------------------
# 4. _execute_batch_for_binding: a new open actually uses the lead leverage.
# ---------------------------------------------------------------------------

async def test_open_new_position_uses_lead_leverage(trading):
    # Public leader (e.g. 金木 49x): a fresh open must ensure_leverage with 49,
    # not the dynamic value (10x for this small notional/equity).
    t = trading
    exchange = OfflineExchange()
    exchange.get_max_leverage = AsyncMock(return_value=100)
    batch = {"symbol": "BTCUSDT", "side": "BUY", "positionSide": "LONG", "qty": "1",
             "avgPrice": "100", "notional": "100", "_event_id": "lead-lev-open",
             "startTime": NOW_MS - 150_000, "endTime": NOW_MS - 150_000}
    await t.router._execute_batch_for_binding(
        t.db, t.binding, t.lead, t.account, exchange, batch,
        lead_margin_balance=1000, active_lead_positions={"BTCUSDT_LONG": 1}, my_equity=1000,
        positions=[], instruments={"BTCUSDT": t.spec}, global_kill=False,
        notify_cfg={}, resilience_cfg=None, lead_leverage=49,
    )
    await t.db.commit()
    exchange.ensure_leverage.assert_awaited_once()
    assert exchange.ensure_leverage.await_args.args[1] == 49
    assert (await t.db.scalar(select(ExecutionRecord))).status == "EXECUTED"


async def test_open_new_position_without_lead_leverage_uses_dynamic(trading):
    # Hidden leader (no leverage visible): same open falls back to dynamic leverage.
    t = trading
    exchange = OfflineExchange()
    exchange.get_max_leverage = AsyncMock(return_value=100)
    batch = {"symbol": "BTCUSDT", "side": "BUY", "positionSide": "LONG", "qty": "1",
             "avgPrice": "100", "notional": "100", "_event_id": "lead-lev-hidden",
             "startTime": NOW_MS - 150_000, "endTime": NOW_MS - 150_000}
    await t.router._execute_batch_for_binding(
        t.db, t.binding, t.lead, t.account, exchange, batch,
        lead_margin_balance=1000, active_lead_positions={"BTCUSDT_LONG": 1}, my_equity=1000,
        positions=[], instruments={"BTCUSDT": t.spec}, global_kill=False,
        notify_cfg={}, resilience_cfg=None, lead_leverage=None,
    )
    await t.db.commit()
    exchange.ensure_leverage.assert_awaited_once()
    assert exchange.ensure_leverage.await_args.args[1] == 10
    assert (await t.db.scalar(select(ExecutionRecord))).status == "EXECUTED"


async def test_add_to_existing_position_ignores_lead_leverage(trading):
    # Add to an existing local position: local leverage wins, lead leverage ignored.
    t = trading
    exchange = OfflineExchange()
    exchange.get_max_leverage = AsyncMock(return_value=100)
    positions = [ExchangePosition("BTCUSDT", "BTCUSDT", "long", "cross", 10, 10, 100, 0, 0, 1000, 10)]
    batch = {"symbol": "BTCUSDT", "side": "BUY", "positionSide": "LONG", "qty": "1",
             "avgPrice": "100", "notional": "100", "_event_id": "lead-lev-add",
             "startTime": NOW_MS - 150_000, "endTime": NOW_MS - 150_000}
    await t.router._execute_batch_for_binding(
        t.db, t.binding, t.lead, t.account, exchange, batch,
        lead_margin_balance=1000, active_lead_positions={"BTCUSDT_LONG": 1}, my_equity=1000,
        positions=positions, instruments={"BTCUSDT": t.spec}, global_kill=False,
        notify_cfg={}, resilience_cfg=None, lead_leverage=49,
    )
    await t.db.commit()
    exchange.ensure_leverage.assert_awaited_once()
    assert exchange.ensure_leverage.await_args.args[1] == 10
    assert (await t.db.scalar(select(ExecutionRecord))).status == "EXECUTED"


# ---------------------------------------------------------------------------
# 5. End-to-end: process_lead_events carries the snapshot leverage into the open.
# ---------------------------------------------------------------------------

async def test_process_lead_events_uses_snapshot_leverage_for_public_open(trading, monkeypatch):
    # Full wiring: a public snapshot carrying leverage 49 (e.g. 金木) must reach
    # ensure_leverage when the engine opens the matching position.
    from test_router_remediations import current_snapshot, prepare_event_router, source_order

    t = trading
    exchange = OfflineExchange()
    exchange.get_positions.return_value = []
    await prepare_event_router(t, exchange, monkeypatch)
    snapshot = current_snapshot("LONG")
    snapshot["openPositions"][0]["leverage"] = 49
    orders = [source_order("public-open", "LONG", "BUY", NOW_MS - 150_000)]
    await t.router.process_lead_events(t.db, t.lead, orders, [], snapshot, [t.proxy])
    assert len(exchange.orders) == 1
    exchange.ensure_leverage.assert_awaited_once()
    assert exchange.ensure_leverage.await_args.args[1] == 49
    assert (await t.db.scalar(select(ExecutionRecord))).status == "EXECUTED"


async def test_process_lead_events_without_snapshot_leverage_uses_dynamic(trading, monkeypatch):
    # Same flow, hidden leader (no leverage in the snapshot): dynamic leverage.
    from test_router_remediations import current_snapshot, prepare_event_router, source_order

    t = trading
    exchange = OfflineExchange()
    exchange.get_positions.return_value = []
    await prepare_event_router(t, exchange, monkeypatch)
    snapshot = current_snapshot("LONG")
    # No "leverage" key: a reconstructed (hidden) position carries none.
    assert "leverage" not in snapshot["openPositions"][0]
    orders = [source_order("hidden-open", "LONG", "BUY", NOW_MS - 150_000)]
    await t.router.process_lead_events(t.db, t.lead, orders, [], snapshot, [t.proxy])
    assert len(exchange.orders) == 1
    exchange.ensure_leverage.assert_awaited_once()
    assert exchange.ensure_leverage.await_args.args[1] == 10
    assert (await t.db.scalar(select(ExecutionRecord))).status == "EXECUTED"
