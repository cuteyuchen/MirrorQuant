"""Transient BAPI failures and empty holdings must never liquidate followers."""
import copy
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.engine.router import CopyRouter
from app.services.lead_collector.binance_bapi import (
    BUSY_CODE, MAX_STALE_SNAPSHOT_SEC, RATE_LIMIT_CODE,
    BapiBusyError, BapiRateLimitError, BinanceBapiCollector,
    is_authoritative_snapshot, is_retryable_exception, slim_order,
)
from app.services.proxy.manager import ProxyManager


NOW_MS = 2_000_000
POSITION = {
    "symbol": "BTCUSDT", "positionSide": "LONG", "qty": 2,
    "qtyExact": "2", "avgEntry": 100, "openTime": NOW_MS - 20_000,
}
HOLDING = {"symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": "2", "entryPrice": "100"}


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=NOW_MS / 1000)
    monkeypatch.setattr("app.services.lead_collector.binance_bapi.time.time", lambda: state.now)
    return state


@pytest.fixture
def collector(monkeypatch, clock):
    value = BinanceBapiCollector()
    value.fetch_detail = AsyncMock(return_value={"positionShow": True, "marginBalance": 1000})
    value.sync_orders_into_store = AsyncMock(return_value=0)
    value.fetch_position_history = AsyncMock(return_value={"list": [], "total": 0})
    value.fetch_holdings = AsyncMock(return_value=[])
    monkeypatch.setattr("app.services.lead_collector.binance_bapi.asyncio.sleep", AsyncMock())
    return value


def previous_snapshot(age_ms=1000, positions=None):
    return {
        "meta": {"generatedAt": NOW_MS - age_ms, "detailOk": True},
        "openPositions": copy.deepcopy([POSITION] if positions is None else positions),
        "openPositionsMeta": {
            "asOf": NOW_MS - age_ms, "authoritative": True, "complete": True,
            "positionShow": True, "source": "api", "partial": False,
        },
    }


def close_order(**overrides):
    row = {
        "orderId": "close", "symbol": "BTCUSDT", "positionSide": "LONG", "side": "SELL",
        "orderTime": NOW_MS - 500, "orderUpdateTime": NOW_MS - 500,
        "executedQty": "2", "avgPrice": "100", "type": "MARKET",
    }
    row.update(overrides)
    record = slim_order(row)
    return {record["h"]: record}


def mock_http(monkeypatch, outcomes):
    calls = []
    remaining = list(outcomes)

    def respond(request):
        calls.append(request)
        value = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(ProxyManager, "build_client", lambda **kwargs: httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
    ))
    sleep = AsyncMock()
    monkeypatch.setattr("app.services.lead_collector.binance_bapi.asyncio.sleep", sleep)
    monkeypatch.setattr("app.services.lead_collector.binance_bapi.random.uniform", lambda *_: 0.1)
    return calls, sleep


@pytest.mark.parametrize("exc", [
    BapiRateLimitError(f"bapi rate limit {RATE_LIMIT_CODE}"), BapiBusyError(f"bapi busy {BUSY_CODE}"),
    httpx.ReadTimeout(""), httpx.ConnectTimeout(""), httpx.TimeoutException(""),
    httpx.ConnectError(""), httpx.RemoteProtocolError(""),
    *[httpx.HTTPStatusError("upstream failure", request=None, response=httpx.Response(status))
      for status in (429, 500, 502, 503, 599)],
])
def test_is_retryable_exception_handles_bapi_rate_limit_and_timeouts(exc):
    assert is_retryable_exception(exc)
    wrapped = RuntimeError("request failed")
    wrapped.__cause__ = exc
    outer = RuntimeError("collection failed")
    outer.__cause__ = wrapped
    assert is_retryable_exception(outer)


def test_is_retryable_exception_rejects_permanent_errors_and_cyclic_causes():
    error = RuntimeError("permanent error")
    error.__cause__ = error
    assert not is_retryable_exception(error)
    for status in (400, 403, 404):
        assert not is_retryable_exception(httpx.HTTPStatusError("permanent", request=None, response=httpx.Response(status)))


@pytest.mark.asyncio
async def test_fetch_holdings_retries_at_least_three_times_on_transient_error(monkeypatch):
    calls, sleep = mock_http(monkeypatch, [httpx.ReadTimeout(""), httpx.ReadTimeout(""),
        httpx.Response(200, json={"code": "000000", "data": [HOLDING]})])
    assert await BinanceBapiCollector().fetch_holdings("lead") == [HOLDING]
    assert len(calls) == 3
    assert [call.args[0] for call in sleep.await_args_list] == pytest.approx([0.9, 1.7])


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["fetch_holdings", "fetch_detail", "fetch_order_history", "fetch_position_history"])
@pytest.mark.parametrize("code", [RATE_LIMIT_CODE, int(RATE_LIMIT_CODE)])
async def test_bapi_rate_limit_twice_then_success(monkeypatch, endpoint, code):
    data = [HOLDING] if endpoint == "fetch_holdings" else {"list": [], "positionShow": True}
    calls, _ = mock_http(monkeypatch, [
        httpx.Response(200, json={"code": code, "message": "请求次数过多，请使用 WebSocket 更新"}),
        httpx.Response(200, json={"code": code}),
        httpx.Response(200, json={"code": "000000", "data": data}),
    ])
    result = await getattr(BinanceBapiCollector(), endpoint)("lead")
    assert result == ([] if endpoint == "fetch_order_history" else data)
    assert len(calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    httpx.ReadTimeout(""), httpx.ConnectTimeout(""), httpx.Response(429), httpx.Response(502),
    httpx.Response(503), httpx.Response(200, json={"code": BUSY_CODE}),
    httpx.Response(200, json={"code": RATE_LIMIT_CODE}),
])
async def test_fetch_holdings_exhausts_three_attempts_and_preserves_retryable_cause(monkeypatch, failure):
    calls, sleep = mock_http(monkeypatch, [failure])
    with pytest.raises(RuntimeError) as caught:
        await BinanceBapiCollector().fetch_holdings("lead")
    assert len(calls) == 3 and sleep.await_count == 2
    assert is_retryable_exception(caught.value)


@pytest.mark.asyncio
async def test_http_json_default_has_three_attempts(monkeypatch):
    calls, _ = mock_http(monkeypatch, [httpx.ReadTimeout("")])
    with pytest.raises(RuntimeError):
        await BinanceBapiCollector()._http_json("https://example.invalid")
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_sync_snapshot_holdings_error_falls_back_to_previous_snapshot(collector):
    collector.fetch_holdings.side_effect = httpx.ConnectTimeout("holdings unavailable")
    previous = previous_snapshot()
    original = copy.deepcopy(previous)
    snapshot = await collector.sync_snapshot("lead", {}, [], with_analysis=False, previous_snapshot=previous)
    assert snapshot["openPositions"][0]["qtyExact"] == "2"
    assert snapshot["openPositionsMeta"]["authoritative"] is False
    assert snapshot["openPositionsMeta"]["partial"] is True
    assert snapshot["openPositionsMeta"]["source"] == "stale-fallback"
    assert snapshot["openPositionsMeta"]["asOf"] == original["openPositionsMeta"]["asOf"]
    assert "ConnectTimeout" in snapshot["openPositionsMeta"]["error"]
    assert not is_authoritative_snapshot(snapshot)
    assert previous == original


@pytest.mark.asyncio
async def test_sync_snapshot_anomalous_empty_holdings_falls_back_to_previous_snapshot(collector):
    snapshot = await collector.sync_snapshot("lead", {}, [], with_analysis=False, previous_snapshot=previous_snapshot())
    assert snapshot["openPositions"][0]["symbol"] == "BTCUSDT"
    assert snapshot["openPositionsMeta"]["authoritative"] is False
    assert snapshot["openPositionsMeta"]["partial"] is True
    assert "Anomalous empty holdings response" in snapshot["openPositionsMeta"]["error"]
    assert not is_authoritative_snapshot(snapshot)
    assert collector.fetch_holdings.await_count == 2


@pytest.mark.asyncio
async def test_anomalous_empty_retry_can_recover(collector):
    collector.fetch_holdings.side_effect = [[], [HOLDING]]
    snapshot = await collector.sync_snapshot("lead", {}, [], with_analysis=False, previous_snapshot=previous_snapshot())
    assert is_authoritative_snapshot(snapshot)
    assert snapshot["openPositions"][0]["qtyExact"] == "2"
    assert snapshot["openPositionsMeta"]["error"] is None


@pytest.mark.asyncio
async def test_anomalous_empty_retry_error_uses_stale_fallback(collector):
    collector.fetch_holdings.side_effect = [[], httpx.ReadTimeout("retry failed")]
    snapshot = await collector.sync_snapshot("lead", {}, [], with_analysis=False, previous_snapshot=previous_snapshot())
    assert snapshot["openPositions"] and snapshot["openPositionsMeta"]["partial"]
    assert not is_authoritative_snapshot(snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [[], httpx.ConnectTimeout("")])
async def test_expired_fallback_never_verifies_subsequent_empty_polls(collector, failure):
    if isinstance(failure, Exception):
        collector.fetch_holdings.side_effect = failure
    previous = previous_snapshot(age_ms=MAX_STALE_SNAPSHOT_SEC * 1000 + 1)
    snapshot = await collector.sync_snapshot("lead", {}, [], with_analysis=False, previous_snapshot=previous)
    assert snapshot["openPositions"] == []
    assert snapshot["openPositionsMeta"]["source"] == "derived-from-order-history"
    assert snapshot["openPositionsMeta"]["partial"] and not is_authoritative_snapshot(snapshot)
    collector.fetch_holdings.side_effect = None
    for _ in range(3):
        snapshot = await collector.sync_snapshot("lead", {}, [], with_analysis=False, previous_snapshot=snapshot)
        assert snapshot["openPositions"] == [] and not is_authoritative_snapshot(snapshot)
        assert snapshot["openPositionsMeta"]["partial"]


@pytest.mark.asyncio
async def test_repeated_stale_fallback_does_not_refresh_original_age(collector, clock):
    collector.fetch_holdings.side_effect = httpx.ConnectTimeout("")
    previous = previous_snapshot()
    snapshot = await collector.sync_snapshot("lead", {}, [], with_analysis=False, previous_snapshot=previous)
    clock.now += 30
    snapshot = await collector.sync_snapshot("lead", {}, [], with_analysis=False, previous_snapshot=snapshot)
    assert snapshot["openPositions"]
    clock.now += 30
    snapshot = await collector.sync_snapshot("lead", {}, [], with_analysis=False, previous_snapshot=snapshot)
    assert snapshot["openPositions"] == [] and not is_authoritative_snapshot(snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize("age_ms", [MAX_STALE_SNAPSHOT_SEC * 1000, -1000])
async def test_stale_age_boundary_and_future_snapshot(collector, age_ms):
    collector.fetch_holdings.side_effect = httpx.ConnectTimeout("")
    snapshot = await collector.sync_snapshot("lead", {}, [], with_analysis=False, previous_snapshot=previous_snapshot(age_ms))
    assert bool(snapshot["openPositions"]) == (age_ms >= 0)
    assert not is_authoritative_snapshot(snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", ["orders", "closed_cache", "closed_history"])
async def test_sync_snapshot_legitimate_empty_holdings_when_close_orders_present(collector, evidence):
    closed = {"positionId": "closed", "symbol": "BTCUSDT", "side": "LONG",
              "opened": POSITION["openTime"], "closed": NOW_MS - 500}
    snapshot = await collector.sync_snapshot(
        "lead", close_order() if evidence == "orders" else {},
        [closed] if evidence == "closed_cache" else [], with_analysis=False,
        closed_history=[closed] if evidence == "closed_history" else None,
        previous_snapshot=previous_snapshot(),
    )
    assert snapshot["openPositions"] == [] and is_authoritative_snapshot(snapshot)
    assert snapshot["openPositionsMeta"]["emptyVerified"] is True
    collector.fetch_holdings.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"symbol": "ETHUSDT"}, {"executedQty": "1"}, {"side": "BUY"}, {"positionSide": "SHORT"},
    {"orderTime": NOW_MS - 2000, "orderUpdateTime": NOW_MS - 500},
])
async def test_unrelated_partial_and_old_orders_do_not_verify_empty_holdings(collector, overrides):
    snapshot = await collector.sync_snapshot("lead", close_order(**overrides), [], with_analysis=False,
                                             previous_snapshot=previous_snapshot())
    assert snapshot["openPositions"] and not is_authoritative_snapshot(snapshot)
    assert collector.fetch_holdings.await_count == 2


@pytest.mark.asyncio
async def test_close_evidence_must_cover_every_previous_position(collector):
    positions = [POSITION, {**POSITION, "symbol": "ETHUSDT"}]
    snapshot = await collector.sync_snapshot("lead", close_order(), [], with_analysis=False,
                                             previous_snapshot=previous_snapshot(positions=positions))
    assert len(snapshot["openPositions"]) == 2 and not is_authoritative_snapshot(snapshot)


@pytest.mark.asyncio
async def test_old_closed_cycle_cannot_explain_reopened_position(collector):
    closed = {"symbol": "BTCUSDT", "side": "LONG", "opened": NOW_MS - 40_000, "closed": NOW_MS - 30_000}
    snapshot = await collector.sync_snapshot("lead", {}, [closed], with_analysis=False,
                                             previous_snapshot=previous_snapshot())
    assert snapshot["openPositions"] and not is_authoritative_snapshot(snapshot)


@pytest.mark.asyncio
async def test_empty_holdings_without_previous_positions_is_authoritative(collector):
    snapshot = await collector.sync_snapshot("lead", {}, [], with_analysis=False, previous_snapshot=previous_snapshot(positions=[]))
    assert snapshot["openPositions"] == [] and is_authoritative_snapshot(snapshot)
    collector.fetch_holdings.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("holdings", [[{**HOLDING, "positionAmt": "0"}], [{"symbol": "BTCUSDT"}], [None]])
async def test_zero_quantity_and_malformed_holdings_cannot_verify_empty(collector, holdings):
    collector.fetch_holdings.return_value = holdings
    snapshot = await collector.sync_snapshot("lead", {}, [], with_analysis=False, previous_snapshot=previous_snapshot())
    assert snapshot["openPositions"] and snapshot["openPositionsMeta"]["partial"]
    assert not is_authoritative_snapshot(snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"authoritative": False}, {"partial": True}, {"error": "holdings unavailable"},
    {"emptyVerified": False}, {"source": "stale-fallback"}, {"asOf": NOW_MS - 31_000},
])
async def test_reconcile_orphan_positions_safeguard_against_unverified_empty_lead(clock, overrides, caplog):
    router = CopyRouter(None)
    router._close_owned = AsyncMock()
    router.reconcile_pending_orders = AsyncMock()
    snapshot = previous_snapshot(positions=[])
    snapshot["openPositionsMeta"].update({"emptyVerified": True, **overrides})
    lead = SimpleNamespace(snapshot_json=json.dumps(snapshot))
    position = SimpleNamespace(pos=2, pos_side="long", inst_id="BTCUSDT")
    result = await router.reconcile_orphan_positions(
        AsyncMock(), lead, SimpleNamespace(), SimpleNamespace(), AsyncMock(), [], [position], {},
        resilience_cfg={"auto_close_orphan_positions": "auto_close"},
    )
    assert result == []
    router._close_owned.assert_not_awaited()
    router.reconcile_pending_orders.assert_not_awaited()
    assert "Skipping orphan auto-close" in caplog.text


@pytest.mark.asyncio
async def test_authoritative_empty_without_matching_close_events_never_closes(clock, caplog):
    router = CopyRouter(None)
    router._close_owned = AsyncMock()
    router.reconcile_pending_orders = AsyncMock(return_value=True)
    router._get_allocation = AsyncMock(return_value=SimpleNamespace(owned_qty=2, opened_at_ms=POSITION["openTime"]))
    snapshot = previous_snapshot(positions=[])
    snapshot["openPositionsMeta"]["emptyVerified"] = True
    lead = SimpleNamespace(snapshot_json=json.dumps(snapshot), orders_store_json="{}")
    position = SimpleNamespace(pos=2, pos_side="long", inst_id="BTCUSDT", symbol="BTCUSDT")
    adapter = SimpleNamespace(get_positions=AsyncMock(return_value=[position]))
    actions = await router.reconcile_orphan_positions(
        AsyncMock(), lead, SimpleNamespace(id=1), SimpleNamespace(id=1, symbol_map_json="{}"), adapter,
        [], [position], {"BTCUSDT": SimpleNamespace()},
        resilience_cfg={"auto_close_orphan_positions": "auto_close"}, notify_cfg={"notify_on_execution": False},
    )
    assert actions == []
    router._close_owned.assert_not_awaited()
    assert "without close events" in caplog.text
