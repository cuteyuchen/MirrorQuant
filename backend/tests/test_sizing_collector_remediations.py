from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.services.adapters.base import InstrumentSpec
from app.services.engine.risk import RiskEngine
from app.services.engine.sizer import PositionSizer, exact, quantize_down
from app.services.lead_collector.binance_bapi import (
    BinanceBapiCollector,
    expand_slim_order,
    group_orders_into_batches,
    is_authoritative_snapshot,
    normalize_holdings,
    order_dedup_key,
    reconstruct_open_positions,
    slim_order,
)


def spec(**kwargs):
    values = dict(inst_id="BTCUSDT", ct_val=Decimal("1"), min_sz=Decimal("0.01"),
                  lot_sz=Decimal("0.01"), tick_sz=Decimal("0.01"), min_notional=Decimal("0"))
    values.update(kwargs)
    return InstrumentSpec(**values)


def source_order(**kwargs):
    order = dict(orderId="stable-1", orderTime=1000, orderUpdateTime=1100,
                 symbol="BTCUSDT", side="BUY", positionSide="LONG", type="MARKET",
                 executedQty="0.1", avgPrice="100")
    order.update(kwargs)
    return order


def test_exact_proportional_target_is_independent_of_leverage_and_floors_once():
    target = PositionSizer.calculate_target_usd("10", "100", "1", "3", "0")
    assert target == Decimal("0.3")
    sized = PositionSizer.size_open_order(target, "0.1", spec())
    assert sized.contracts == Decimal("3.00")
    assert sized.contracts * Decimal("0.1") <= target
    assert PositionSizer.calculate_target_usd("10", "100", "1", "3", "0.2") == Decimal("0.5")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_financial_inputs_fail_closed(value):
    with pytest.raises(ValueError):
        exact(value)
    with pytest.raises(ValueError):
        PositionSizer.size_open_order(value, "0.1", spec())


def test_partial_minimum_position_is_never_promoted_to_full_close():
    instrument = spec(min_sz=Decimal("1"), lot_sz=Decimal("1"))
    assert PositionSizer.size_close_order("1", False, "1", "99", instrument) == 0
    assert PositionSizer.size_close_order("10", False, "1", "99", instrument) == 0
    assert PositionSizer.size_close_order("10.9", True, "1", "0", instrument) == Decimal("10")
    with pytest.raises(ValueError):
        PositionSizer.size_close_order("10", False, "1", "-1", instrument)
    assert quantize_down("-1", "0.01") == 0
    assert quantize_down("1", "0") == 0


def test_dynamic_leverage_tracks_exposure_and_symbol_ceiling():
    assert PositionSizer.compute_dynamic_leverage("80", "100", 100) == 10
    assert PositionSizer.compute_dynamic_leverage("81", "100", 100) == 11
    assert PositionSizer.compute_dynamic_leverage("800", "100", 20) == 20
    assert PositionSizer.compute_dynamic_leverage("1", "100", 1) == 1
    assert PositionSizer.compute_dynamic_leverage("0", "100", 5) == 5
    with pytest.raises(ValueError):
        PositionSizer.compute_dynamic_leverage("1", "100", 0)
    with pytest.raises(ValueError):
        PositionSizer.compute_dynamic_leverage("1", "100", 10, 0)


def test_risk_guards_reject_missing_crossed_books_and_invalid_event_time():
    assert not RiskEngine.check_spread(0, 100, 0.5).allowed
    assert not RiskEngine.check_spread(101, 100, 0.5).allowed
    assert not RiskEngine.check_slippage(float("nan"), 100, 1).allowed
    assert not RiskEngine.check_slippage(100, 0, 1).allowed
    assert not RiskEngine.check_missed_order_catchup(0, 1000, 120, 100, 100, 1).allowed
    assert not RiskEngine.check_missed_order_catchup(1001, 1000, 120, 100, 100, 1).allowed


def test_caps_revalidate_minimum_notional():
    instrument = spec(min_notional=Decimal("5"))
    qty, _ = RiskEngine.cap_by_leverage(Decimal("10"), 1, instrument, 1, [], "BTCUSDT", 3, 3)
    assert qty == 0
    qty, error, _ = RiskEngine.adjust_for_available_margin(Decimal("3"), 1, instrument, 100, 10)
    assert qty == 0 and error


def test_mutable_fill_fields_preserve_identity_and_ids_distinguish_orders():
    before = source_order()
    after = source_order(orderUpdateTime=9999, executedQty="0.8", avgPrice="105")
    assert order_dedup_key(before) == order_dedup_key(after)
    assert order_dedup_key(before) != order_dedup_key(source_order(orderId="stable-2"))
    assert order_dedup_key(expand_slim_order(slim_order(before))) == order_dedup_key(before)


def test_batches_preserve_open_close_open_causality_and_decimal_totals():
    orders = [source_order(orderId="three", orderTime=3000),
              source_order(orderId="two", side="SELL", orderTime=2000),
              source_order(orderId="one", orderTime=1000)]
    batches = group_orders_into_batches(orders)
    assert [b["startTime"] for b in batches] == [1000, 2000, 3000]
    assert [b["side"] for b in batches] == ["BUY", "SELL", "BUY"]
    together = group_orders_into_batches([source_order(), source_order(orderId="two", executedQty="0.2")])[0]
    assert Decimal(together["qtyExact"]) == Decimal("0.3")
    assert Decimal(together["notionalExact"]) == Decimal("30")


def test_reconstructed_partial_close_reduces_cost_and_old_cycle_cannot_hide_reopen():
    records = [source_order(executedQty="10"),
               source_order(orderId="close", side="SELL", executedQty="5", orderTime=2000)]
    positions = reconstruct_open_positions({str(i): slim_order(order) for i, order in enumerate(records)})
    assert positions[0]["qty"] == 5
    assert positions[0]["avgEntry"] == 100
    reopened = source_order(orderId="reopen", orderTime=3000)
    positions = reconstruct_open_positions({"reopen": slim_order(reopened)}, closed_rows=[
        {"symbol": "BTCUSDT", "side": "LONG", "opened": 1000, "closed": 2000}
    ])
    assert len(positions) == 1


def test_direct_holdings_unknown_rows_and_stale_snapshots_fail_closed():
    holdings = normalize_holdings([dict(symbol="BTCUSDT", positionAmount="-2", positionSide="BOTH", entryPrice="100")])
    assert holdings[0]["positionSide"] == "SHORT"
    assert holdings[0]["qtyExact"] == "2"
    with pytest.raises(ValueError):
        normalize_holdings([dict(symbol="BTCUSDT", entryPrice="100")])
    snapshot = {"openPositions": [], "openPositionsMeta": {
        "source": "api", "authoritative": True, "complete": True, "positionShow": True, "asOf": 1000,
    }}
    assert is_authoritative_snapshot(snapshot, now_ms=1100)
    assert not is_authoritative_snapshot(snapshot, now_ms=32000)
    assert not is_authoritative_snapshot(snapshot, now_ms=999)
    snapshot["openPositionsMeta"]["positionShow"] = None
    assert not is_authoritative_snapshot(snapshot, now_ms=1100)


@pytest.mark.asyncio
async def test_source_store_updates_cumulative_fill_without_duplicate_or_precision_loss():
    collector = BinanceBapiCollector()
    initial = source_order(executedQty="0.1")
    record = slim_order(initial)
    store = {record["h"]: record}
    collector.fetch_order_history = AsyncMock(return_value=[source_order(executedQty="0.300000000000000001")])
    assert await collector.sync_orders_into_store("lead", store) == 0
    assert len(store) == 1
    assert next(iter(store.values()))["q"] == "0.300000000000000001"
    collector.fetch_order_history = AsyncMock(return_value=[initial])
    with pytest.raises(RuntimeError, match="Regressing"):
        await collector.sync_orders_into_store("lead", store)
    assert next(iter(store.values()))["q"] == "0.300000000000000001"


@pytest.mark.asyncio
async def test_missing_history_page_never_partially_advances_store():
    collector = BinanceBapiCollector()
    collector.fetch_order_history = AsyncMock(side_effect=[
        [source_order(orderId=str(i), orderTime=1000 + i) for i in range(100)],
        RuntimeError("page unavailable"),
    ])
    store = {}
    with pytest.raises(RuntimeError, match="page unavailable"):
        await collector.sync_orders_into_store("lead", store)
    assert store == {}


@pytest.mark.asyncio
async def test_snapshot_uses_fresh_direct_holdings_and_no_execution_analytics():
    collector = BinanceBapiCollector()
    collector.fetch_detail = AsyncMock(return_value={"positionShow": True, "marginBalance": 100})
    collector.sync_orders_into_store = AsyncMock(return_value=0)
    collector.fetch_position_history = AsyncMock(return_value={"list": [], "total": 0})
    collector.fetch_holdings = AsyncMock(return_value=[])
    collector.fetch_performance = AsyncMock(side_effect=AssertionError("analytics in execution path"))
    collector.fetch_marks = AsyncMock(side_effect=AssertionError("marks in execution path"))
    collector._holdings_cache["lead"] = (float("inf"), [{"malformed": "stale"}])
    order = slim_order(source_order(executedQty="10"))
    snapshot = await collector.sync_snapshot("lead", {order["h"]: order}, [], with_analysis=False)
    assert snapshot["openPositions"] == []
    assert is_authoritative_snapshot(snapshot)
    collector.fetch_performance.assert_not_awaited()
    collector.fetch_marks.assert_not_awaited()

    collector.fetch_holdings = AsyncMock(side_effect=RuntimeError("holdings unavailable"))
    snapshot = await collector.sync_snapshot("lead", {order["h"]: order}, [], with_analysis=False)
    assert len(snapshot["openPositions"]) == 1  # display-only history remains useful
    assert not is_authoritative_snapshot(snapshot)
    assert snapshot["openPositionsMeta"]["asOf"] is None


@pytest.mark.asyncio
async def test_legacy_store_adopts_restored_order_id_without_duplicating_order():
    collector = BinanceBapiCollector()
    legacy = dict(h="legacy-raw-key", ot=1000, t=1100, sym="BTCUSDT", side="BUY", ps="LONG",
                  ty="MARKET", q=0.1, p=100.0, pnl=0)
    previous_stable = order_dedup_key(expand_slim_order(legacy))
    collector.fetch_order_history = AsyncMock(return_value=[source_order(executedQty="0.3")])
    store = {"legacy-raw-key": legacy}
    await collector.sync_orders_into_store("lead", store)
    assert len(store) == 1
    adopted = next(iter(store.values()))
    assert adopted["oid"] == "stable-1" and adopted["q"] == "0.3"
    assert previous_stable in adopted["legacySourceKeys"]
    assert "legacy-raw-key" in adopted["legacyKeys"]
    expanded = expand_slim_order(adopted)
    assert previous_stable in expanded["_legacy_source_keys"]
    assert "legacy-raw-key" in expanded["_legacy_keys"]
