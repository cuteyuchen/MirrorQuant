"""Analysis refreshes retain durable metrics and usable position valuations."""
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.lead_collector.binance_bapi import BinanceBapiCollector, is_authoritative_snapshot


PORTFOLIO = "analysis-lead"
NOW_MS = 2_000_000
RANGES = ("7D", "30D", "90D", "180D", "365D")
PERFORMANCE = {tr: {"roi": 10, "pnl": 20, "winRate": 60, "mdd": 5} for tr in RANGES}
CHART = [{"t": 1000, "v": 10}, {"t": 2000, "v": 12}]
ALLOCATION = {"timeRange": "90D", "items": [{"symbol": "BTCUSDT", "ratio": 100}]}
QUOTE = {"markPrice": 110, "lastPrice": 111, "currentPrice": 111,
         "priceTime": NOW_MS - 1000, "source": "ticker+premiumIndex"}
BRACKETS = {"BTCUSDT": {"tiers": [
    {"floor": 0, "cap": 200, "mmr": 0.01, "cum": 0, "maxLev": 100},
    {"floor": 200, "cap": 1000, "mmr": 0.02, "cum": 2, "maxLev": 50},
]}}
ORDER = {"orderId": "open", "orderTime": NOW_MS - 10_000, "symbol": "BTCUSDT",
         "side": "BUY", "positionSide": "LONG", "type": "MARKET",
         "executedQty": "2", "avgPrice": "100"}


@pytest.fixture
def collector(monkeypatch):
    monkeypatch.setattr("app.services.lead_collector.binance_bapi.time.time", lambda: NOW_MS / 1000)
    value = BinanceBapiCollector(pa_analyzer=SimpleNamespace(
        analyze_position=AsyncMock(return_value={"ok": True}),
    ))
    value.fetch_detail = AsyncMock(return_value={"positionShow": True, "marginBalance": 1000})
    value.fetch_order_history = AsyncMock(return_value=[copy.deepcopy(ORDER)])
    value.fetch_position_history = AsyncMock(return_value={"list": [], "total": 0})
    value.fetch_holdings = AsyncMock(return_value=[{
        "symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": "2", "entryPrice": "100",
    }])
    value.fetch_performance = AsyncMock(side_effect=lambda portfolio_id, time_range, proxy_url: copy.deepcopy(PERFORMANCE[time_range]))
    value.fetch_roi_chart = AsyncMock(return_value=copy.deepcopy(CHART))
    value.fetch_allocation = AsyncMock(return_value=copy.deepcopy(ALLOCATION))
    value.fetch_marks = AsyncMock(return_value={"BTCUSDT": copy.deepcopy(QUOTE)})
    value.fetch_brackets = AsyncMock(return_value=copy.deepcopy(BRACKETS))
    return value


def previous_snapshot():
    return {"performance": copy.deepcopy(PERFORMANCE), "roiChart365D": copy.deepcopy(CHART),
            "allocation": copy.deepcopy(ALLOCATION), "openPositions": [{"symbol": "BTCUSDT", **QUOTE}]}


def assert_valuation(snapshot, *, short=False, exact=True):
    position, = snapshot["openPositions"]
    assert position["markPrice"] == 110
    assert position["lastPrice"] == position["currentPrice"] == 111
    assert position["markValue"] == 220
    assert position["notionalLeverage"] == pytest.approx(0.22)
    assert position["uPnL"] == (-20 if short else 20)
    assert position["uPnLPct"] == (-10 if short else 10)
    assert position["uPnLRoePct"] == (-2 if short else 2)
    assert position["priceEdgePct"] == (-11 if short else 11)
    assert position["mmr"]["exact"] is exact
    assert position["maintMargin"] == (2.4 if exact else 11)
    assert position["maintMarginPct"] == (2 if exact else 5)
    assert position["maintMarginRoePct"] == (0.24 if exact else 1.1)
    summary = snapshot["openPositionsSummary"]
    assert summary["totalMarkNotional"] == 220
    assert summary["totalUnrealizedPnl"] == (-20 if short else 20)
    assert summary["estMaintenanceMargin"] == position["maintMargin"]


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["LONG", "SHORT"])
async def test_analysis_refresh_populates_metrics_quotes_and_mmr(collector, side):
    collector.fetch_holdings.return_value[0].update(positionSide=side, positionAmt="-2" if side == "SHORT" else "2")
    snapshot = await collector.sync_snapshot(PORTFOLIO, {}, [], with_analysis=True)
    assert snapshot["performance"] == PERFORMANCE
    assert snapshot["roiChart365D"] == CHART
    assert snapshot["allocation"] == ALLOCATION
    assert is_authoritative_snapshot(snapshot)
    assert_valuation(snapshot, short=side == "SHORT")
    assert snapshot["openPositionsSummary"]["mmrSource"] == "binance-official-brackets"
    assert {call.kwargs["time_range"] for call in collector.fetch_performance.await_args_list} == set(RANGES)
    collector.fetch_roi_chart.assert_awaited_once_with(PORTFOLIO, time_range="365D", proxy_url=None)
    collector.fetch_allocation.assert_awaited_once_with(PORTFOLIO, time_range="90D", proxy_url=None)
    collector.fetch_marks.assert_awaited_once_with(["BTCUSDT"], proxy_url=None)
    assert snapshot["openPositions"][0]["analysis"] == {"ok": True}
    assert collector.pa_analyzer.analyze_position.await_args.kwargs["current_price"] == 111


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("failure", ["exception", "empty", "empty-items"])
async def test_failed_analysis_refresh_keeps_previous_or_cached_metrics(collector, cached, failure):
    previous = previous_snapshot()
    original = copy.deepcopy(previous)
    if cached:
        collector._perf_cache[PORTFOLIO] = (0.0, copy.deepcopy(PERFORMANCE))
        collector._chart_cache[PORTFOLIO] = (0.0, copy.deepcopy(CHART))
        collector._alloc_cache[PORTFOLIO] = (0.0, copy.deepcopy(ALLOCATION))
        # An existing nonempty cache takes precedence over the durable snapshot.
        previous.update(performance={"7D": {"roi": -99}}, roiChart365D=[{"t": 0, "v": -99}],
                        allocation={"items": [{"symbol": "ETHUSDT"}]})
        original = copy.deepcopy(previous)
    for method, empty in ((collector.fetch_performance, {}), (collector.fetch_roi_chart, []),
                          (collector.fetch_allocation, {"timeRange": "90D", "items": []} if failure == "empty-items" else {})):
        method.side_effect = httpx.ReadTimeout("analysis unavailable") if failure == "exception" else None
        method.return_value = empty
    collector.fetch_marks.side_effect = httpx.ConnectError("marks unavailable")
    collector.fetch_brackets.side_effect = httpx.ConnectError("brackets unavailable")
    for _ in range(2):
        snapshot = await collector.sync_snapshot(PORTFOLIO, {}, [], with_analysis=True, previous_snapshot=previous)
        assert snapshot["performance"] == PERFORMANCE
        assert snapshot["roiChart365D"] == CHART
        assert snapshot["allocation"] == ALLOCATION
        assert collector._perf_cache[PORTFOLIO] == (0.0, PERFORMANCE)
        assert collector._chart_cache[PORTFOLIO] == (0.0, CHART)
        assert collector._alloc_cache[PORTFOLIO] == (0.0, ALLOCATION)
        assert_valuation(snapshot, exact=False)
        assert snapshot["openPositions"][0]["priceSource"] == "previous-snapshot"
        assert snapshot["meta"]["priceUpdatedAt"] == QUOTE["priceTime"]
        assert previous == original
    assert collector.fetch_performance.await_count == 10  # Failed refreshes remain eligible for retry.


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_cache", [False, True])
async def test_seeded_analysis_refresh_merges_successful_ranges(collector, empty_cache):
    previous = previous_snapshot()
    original = copy.deepcopy(previous)
    if empty_cache:
        collector._perf_cache[PORTFOLIO] = (NOW_MS / 1000, {})
        collector._chart_cache[PORTFOLIO] = (NOW_MS / 1000, [])
        collector._alloc_cache[PORTFOLIO] = (NOW_MS / 1000, {"items": []})

    async def fetch_performance(portfolio_id, time_range, proxy_url):
        if time_range == "7D":
            return {"roi": 42, "pnl": 84}
        if time_range == "30D":
            return {}
        raise httpx.ReadTimeout("range unavailable")

    collector.fetch_performance.side_effect = fetch_performance
    collector.fetch_roi_chart.return_value = [{"t": 3000, "v": 42}]
    collector.fetch_allocation.return_value = {"timeRange": "90D", "items": [{"symbol": "ETHUSDT", "ratio": 100}]}
    snapshot = await collector.sync_snapshot(PORTFOLIO, {}, [], with_analysis=True, previous_snapshot=previous)
    expected = {**PERFORMANCE, "7D": {"roi": 42, "pnl": 84}}
    assert snapshot["performance"] == expected
    assert collector._perf_cache[PORTFOLIO] == (NOW_MS / 1000, expected)
    assert snapshot["roiChart365D"] == collector.fetch_roi_chart.return_value
    assert collector._chart_cache[PORTFOLIO] == (NOW_MS / 1000, snapshot["roiChart365D"])
    assert snapshot["allocation"] == collector.fetch_allocation.return_value
    assert collector._alloc_cache[PORTFOLIO] == (NOW_MS / 1000, snapshot["allocation"])
    assert previous == original


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["markPrice", "lastPrice", "currentPrice"])
@pytest.mark.parametrize("missing", [None, "", "NaN", 0])
async def test_missing_quotes_inherit_any_previous_valid_price(collector, field, missing):
    previous = {"openPositions": [{"symbol": "ETHUSDT", "markPrice": 999},
                                  {"symbol": "BTCUSDT", field: "110", "priceTime": NOW_MS - 5000}]}
    collector.fetch_marks.return_value = {"BTCUSDT": {"markPrice": missing, "currentPrice": missing}}
    snapshot = await collector.sync_snapshot(PORTFOLIO, {}, [], with_analysis=True, previous_snapshot=previous)
    position, = snapshot["openPositions"]
    assert position["markPrice"] == position["lastPrice"] == position["currentPrice"] == 110
    assert position["markValue"] == 220 and position["uPnL"] == 20
    assert position["maintMargin"] == 2.4
    assert position["priceTime"] == NOW_MS - 5000
    assert snapshot["openPositionsSummary"]["priceAsOf"] == NOW_MS - 5000


@pytest.mark.asyncio
async def test_partial_quote_retains_fresh_current_price_and_inherits_mark(collector):
    collector.fetch_marks.return_value = {"BTCUSDT": {"lastPrice": "120", "priceTime": NOW_MS}}
    snapshot = await collector.sync_snapshot(PORTFOLIO, {}, [], with_analysis=True, previous_snapshot=previous_snapshot())
    position, = snapshot["openPositions"]
    assert position["markPrice"] == 110
    assert position["lastPrice"] == position["currentPrice"] == 120
    assert position["priceEdgePct"] == 20
    assert position["markValue"] == 220 and position["uPnL"] == 20


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "empty"])
async def test_marks_and_brackets_failures_use_memory_caches(collector, failure):
    collector._marks_cache["BTCUSDT"] = copy.deepcopy(QUOTE)
    collector._brackets_map = copy.deepcopy(BRACKETS)
    for method in (collector.fetch_marks, collector.fetch_brackets):
        method.side_effect = httpx.ReadTimeout("") if failure == "exception" else None
        method.return_value = {}
    snapshot = await collector.sync_snapshot(PORTFOLIO, {}, [], with_analysis=True)
    assert_valuation(snapshot)
    assert snapshot["openPositions"][0]["priceSource"] == "cache"
    assert snapshot["openPositions"][0]["priceTime"] == QUOTE["priceTime"]


@pytest.mark.asyncio
@pytest.mark.parametrize("quote_source", ["live", "previous"])
@pytest.mark.parametrize("side", ["LONG", "SHORT"])
async def test_hidden_positions_reconstructed_with_quotes_and_maintenance_margin(collector, quote_source, side):
    collector.fetch_detail.return_value["positionShow"] = False
    collector.fetch_order_history.return_value[0].update(positionSide=side, side="SELL" if side == "SHORT" else "BUY")
    previous = None
    if quote_source == "previous":
        previous = previous_snapshot()
        collector.fetch_marks.side_effect = httpx.ReadTimeout("marks unavailable")
    snapshot = await collector.sync_snapshot(PORTFOLIO, {}, [], with_analysis=True, previous_snapshot=previous)
    collector.fetch_holdings.assert_not_awaited()
    assert not is_authoritative_snapshot(snapshot)
    assert snapshot["openPositionsMeta"]["source"] == "derived-from-order-history"
    position, = snapshot["openPositions"]
    assert position["qty"] == 2 and position["avgEntry"] == 100
    assert position["entryOrders"] == [{"t": ORDER["orderTime"], "q": 2.0, "p": 100.0}]
    assert_valuation(snapshot, short=side == "SHORT")


@pytest.mark.asyncio
async def test_failed_quotes_without_fallback_leave_valuation_unknown(collector):
    collector.fetch_marks.side_effect = httpx.ReadTimeout("")
    collector.fetch_brackets.side_effect = httpx.ReadTimeout("")
    snapshot = await collector.sync_snapshot(PORTFOLIO, {}, [], with_analysis=True)
    position, = snapshot["openPositions"]
    for field in ("markPrice", "currentPrice", "markValue", "uPnL", "uPnLPct", "uPnLRoePct", "mmr", "maintMargin"):
        assert position[field] is None
    for field in ("totalMarkNotional", "totalUnrealizedPnl", "estMaintenanceMargin", "mmrSource"):
        assert snapshot["openPositionsSummary"][field] is None


@pytest.mark.asyncio
async def test_analysis_disabled_still_seeds_durable_metrics(collector):
    previous = previous_snapshot()
    snapshot = await collector.sync_snapshot(PORTFOLIO, {}, [], with_analysis=False, previous_snapshot=previous)
    assert snapshot["performance"] == PERFORMANCE
    assert snapshot["roiChart365D"] == CHART
    assert snapshot["allocation"] == ALLOCATION
    for method in (collector.fetch_performance, collector.fetch_roi_chart, collector.fetch_allocation,
                   collector.fetch_marks, collector.fetch_brackets):
        method.assert_not_awaited()
