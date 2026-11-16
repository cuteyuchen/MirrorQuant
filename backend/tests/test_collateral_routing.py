"""Synthetic collateral balances only; no exchange or account access."""
import copy
import json
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.domain import ExecutionRecord, NotificationOutbox, OrderIntent, SystemConfig
from app.services.adapters.binance import BinanceAdapter
from test_router_remediations import trading, execute_batch, NOW_MS
from test_follow_lifecycle_simulation import prepare_simulation


ACCOUNT = {"multiAssetsMargin": False, "updateTime": NOW_MS, "assets": [
    {"asset": "USDT", "marginBalance": "1000", "availableBalance": "734.45",
     "walletBalance": "1000", "unrealizedProfit": "0"},
    {"asset": "USDC", "marginBalance": "5000", "availableBalance": "5000",
     "walletBalance": "5000", "unrealizedProfit": "0"}],
    "totalMarginBalance": "6000", "availableBalance": "5734.45", "totalWalletBalance": "6000",
    "totalUnrealizedProfit": "0"}


async def test_single_asset_contract_balance_does_not_sum_usdc(trading):
    adapter = BinanceAdapter({})
    adapter._request = AsyncMock(side_effect=[{"multiAssetsMargin": False}, copy.deepcopy(ACCOUNT)])
    balance = await adapter.get_contract_balance(trading.spec)
    assert balance.eq == 1000 and balance.avail_eq == 734.45
    assert balance.currency == "USDT"


@pytest.mark.parametrize("issue", ("mode_missing", "mode_string", "multi_assets", "contradiction",
    "asset_missing", "asset_duplicate", "available_missing", "equity_missing", "negative", "nan",
    "inf", "boolean", "timestamp_invalid", "timestamp_future", "timestamp_boolean"))
async def test_ambiguous_contract_balance_fails_closed(trading, issue):
    adapter = BinanceAdapter({})
    account = copy.deepcopy(ACCOUNT)
    mode = {"multiAssetsMargin": False}
    if issue == "mode_missing": mode = {}
    elif issue == "mode_string": mode = {"multiAssetsMargin": "false"}
    elif issue == "multi_assets": mode = {"multiAssetsMargin": True}
    elif issue == "contradiction": account["multiAssetsMargin"] = True
    elif issue == "asset_missing": account["assets"] = [account["assets"][1]]
    elif issue == "asset_duplicate": account["assets"].append(copy.deepcopy(account["assets"][0]))
    elif issue == "available_missing": del account["assets"][0]["availableBalance"]
    elif issue == "equity_missing": del account["assets"][0]["marginBalance"]
    elif issue in ("negative", "nan", "inf", "boolean"):
        account["assets"][0]["availableBalance"] = {"negative": "-1", "nan": "nan", "inf": "inf", "boolean": True}[issue]
    elif issue == "timestamp_invalid": account["updateTime"] = "unparseable"
    elif issue == "timestamp_future": account["updateTime"] = NOW_MS + 1
    elif issue == "timestamp_boolean": account["updateTime"] = False  # 布尔假值不能伪装成合法零时间戳。
    adapter._request = AsyncMock(side_effect=[mode, account])
    with pytest.raises(ValueError):
        await adapter.get_contract_balance(trading.spec)


@pytest.mark.parametrize("timestamp", [None, 0, NOW_MS - 30001, 1])
async def test_idle_collateral_timestamp_is_not_request_freshness(trading, timestamp):
    # 余额未变动不是请求过期，账户级和资产级时间都允许陈旧或缺失。
    adapter = BinanceAdapter({})
    data = copy.deepcopy(ACCOUNT)
    data["updateTime"] = timestamp
    for asset in data["assets"]:
        asset["updateTime"] = timestamp
    adapter._request = AsyncMock(side_effect=[{"multiAssetsMargin": False}, data])
    assert (await adapter.get_contract_balance(trading.spec)).eq == 1000


async def test_slow_collateral_request_still_fails_closed(trading, monkeypatch):
    # 超过三十秒的请求不能被闲置余额的例外放行。
    adapter = BinanceAdapter({})
    adapter._request = AsyncMock(side_effect=[{"multiAssetsMargin": False}, copy.deepcopy(ACCOUNT)])
    clock = [10.0]
    async def response(method, path):
        if path.endswith("multiAssetsMargin"):
            return {"multiAssetsMargin": False}
        clock[0] = 40.001
        return copy.deepcopy(ACCOUNT)
    adapter._request = AsyncMock(side_effect=response)
    with monkeypatch.context() as patch:
        patch.setattr("app.services.adapters.binance.time.monotonic", lambda: clock[0])
        with pytest.raises(ValueError, match="request is stale"):
            await adapter.get_contract_balance(trading.spec)


@pytest.mark.parametrize("available", ("0", "734.45"))
async def test_routing_uses_only_relevant_available_margin(trading, monkeypatch, available):
    t = trading
    sim, engine, _ = await prepare_simulation(t, monkeypatch)
    original = sim.respond
    def respond(request):
        if request.url.path == "/fapi/v2/account":
            import httpx
            payload = copy.deepcopy(ACCOUNT)
            payload["assets"][0]["availableBalance"] = available
            payload["updateTime"] = sim.now
            return httpx.Response(200, json=payload)
        return original(request)
    sim.respond = respond
    sim.event("BUY")
    await engine.sync_lead(t.db, t.lead, proxies=[])
    if available == "0":
        assert sim.orders == {} and (await t.db.scalar(select(ExecutionRecord))).status == "BLOCKED_INSUFFICIENT_MARGIN"
    else:
        assert len(sim.orders) == 1
        assert Decimal(next(iter(sim.orders.values()))["quantity"]) == 1  # Equity 1000, not 6000.
    assert t.account.usdt_equity == 1000 and t.account.avail_equity == float(available)


@pytest.mark.parametrize("mode", (None, "false", True))
async def test_routing_ambiguous_mode_has_no_intent_order_or_success(trading, monkeypatch, mode):
    t = trading
    sim, engine, _ = await prepare_simulation(t, monkeypatch)
    original = sim.respond
    def respond(request):
        if request.url.path == "/fapi/v1/multiAssetsMargin":
            import httpx
            return httpx.Response(200, json={} if mode is None else {"multiAssetsMargin": mode})
        return original(request)
    sim.respond = respond
    sim.event("BUY")
    await engine.sync_lead(t.db, t.lead, proxies=[])
    assert sim.orders == {} and await t.db.scalar(select(OrderIntent)) is None
    records = (await t.db.scalars(select(ExecutionRecord))).all()
    assert len(records) == 1 and records[0].status == "BLOCKED_COLLATERAL_BALANCE"
    messages = (await t.db.scalars(select(NotificationOutbox))).all()
    assert len(messages) == 1 and "✅" not in json.loads(messages[0].payload)["message"]

