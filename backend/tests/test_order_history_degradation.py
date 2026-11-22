"""Order history is secondary confirmation only: when it is rate-limited,
the snapshot must still publish from holdings (positions are the source
of truth for following). 2026-11-22: BapiRateLimitError 90801003 on
order-history used to kill the whole sync (after ~10s of retries), so
the fast path starved for ~14s. Now the rate limit fails fast (no retry)
and sync_snapshot degrades instead of raising.
"""
import copy
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.lead_collector.binance_bapi import (
    BapiRateLimitError, BinanceBapiCollector, is_authoritative_snapshot,
    RATE_LIMIT_CODE, slim_order,
)
from app.services.proxy.manager import ProxyManager


PROFILE = {
    "nickname": "degradation test", "positionShow": True, "marginBalance": 1000,
    "status": "ONGOING", "isPaused": False, "aumAmount": 12345.6,
}
ORDER = {
    "orderId": "known-order", "orderTime": 1000, "orderUpdateTime": 1000,
    "symbol": "BTCUSDT", "side": "BUY", "positionSide": "LONG", "type": "MARKET",
    "executedQty": "2", "avgPrice": "100",
}
HOLDINGS = [{
    "symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": "2",
    "entryPrice": "100", "markPrice": "110",
}]


def mock_http(monkeypatch, outcomes):
    calls = []

    def respond(request):
        calls.append(request)
        value = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if isinstance(value, Exception):
            raise value
        return value

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(ProxyManager, "build_client", lambda **kwargs: httpx.AsyncClient(transport=transport))
    sleep = AsyncMock()
    monkeypatch.setattr("app.services.lead_collector.binance_bapi.asyncio.sleep", sleep)
    return calls, sleep


async def test_rate_limit_error_is_not_retried(monkeypatch):
    # 限流是退避信号：订单流调用方传 retry_rate_limit=False，一次就失败、不重试。
    calls, sleep = mock_http(monkeypatch, [
        httpx.Response(200, json={"code": RATE_LIMIT_CODE, "message": "请求次数过多，请使用 WebSocket 更新。"}),
    ])
    with pytest.raises(RuntimeError, match="90801003"):
        await BinanceBapiCollector()._http_json(
            "https://example.invalid", retries=4, retry_rate_limit=False)
    assert len(calls) == 1
    sleep.assert_not_awaited()


def collector_with_mocks(monkeypatch, *, order_history_error=None):
    collector = BinanceBapiCollector()
    collector.fetch_detail = AsyncMock(return_value=copy.deepcopy(PROFILE))
    if order_history_error is not None:
        collector.fetch_order_history = AsyncMock(side_effect=order_history_error)
    else:
        collector.fetch_order_history = AsyncMock(return_value=[copy.deepcopy(ORDER)])
    collector.fetch_position_history = AsyncMock(return_value={"list": [], "total": 0})
    collector.fetch_holdings = AsyncMock(return_value=copy.deepcopy(HOLDINGS))
    return collector


async def test_sync_snapshot_publishes_positions_when_order_history_rate_limited(monkeypatch):
    collector = collector_with_mocks(
        monkeypatch, order_history_error=BapiRateLimitError("bapi rate limit 90801003: 请求次数过多"))
    known = slim_order(copy.deepcopy(ORDER))
    store = {known["h"]: known}
    snapshot = await collector.sync_snapshot(
        "test-portfolio", store, [], with_analysis=False)
    assert snapshot["meta"]["orderHistoryComplete"] is False
    assert "90801003" in (snapshot["meta"]["orderHistoryError"] or "")
    assert snapshot["openPositionsMeta"]["source"] == "api"
    assert snapshot["openPositionsMeta"]["authoritative"] is True
    assert len(snapshot["openPositions"]) == 1
    assert snapshot["openPositions"][0]["symbol"] == "BTCUSDT"
    assert is_authoritative_snapshot(snapshot) is True
    # 已知订单不丢失：store 保持上轮内容，路由去重不会重复处理。
    assert store == {known["h"]: known}
    # 持仓接口只拉一次（没有因订单流失败而多拉）。
    assert collector.fetch_holdings.await_count == 1


async def test_sync_snapshot_publishes_positions_when_closed_history_fails(monkeypatch):
    collector = collector_with_mocks(monkeypatch)
    collector.fetch_position_history = AsyncMock(
        side_effect=BapiRateLimitError("bapi rate limit 90801003: 请求次数过多"))
    snapshot = await collector.sync_snapshot(
        "test-portfolio", {}, [], with_analysis=False)
    assert snapshot["meta"]["closedHistoryComplete"] is False
    assert snapshot["openPositionsMeta"]["source"] == "api"
    assert is_authoritative_snapshot(snapshot) is True


async def test_non_rate_limit_order_history_failure_still_raises(monkeypatch):
    # 只有“限流”才降级：其它订单流失败（数据损坏、连续性断裂等）仍抛错，
    # 走同步失败记录，不能把真问题藏起来。
    collector = collector_with_mocks(
        monkeypatch, order_history_error=RuntimeError("order history unavailable"))
    with pytest.raises(RuntimeError, match="order history unavailable"):
        await collector.sync_snapshot("test-portfolio", {}, [], with_analysis=False)


async def test_sync_snapshot_still_publishes_complete_history_when_healthy(monkeypatch):
    collector = collector_with_mocks(monkeypatch)
    snapshot = await collector.sync_snapshot(
        "test-portfolio", {}, [], with_analysis=False)
    assert snapshot["meta"]["orderHistoryComplete"] is True
    assert snapshot["meta"]["orderHistoryError"] is None
    assert snapshot["openPositionsMeta"]["source"] == "api"
