"""Adapter DTO, Binance detail endpoint, and multi-exchange normalization tests."""
from __future__ import annotations

from decimal import Decimal
import time
from unittest.mock import AsyncMock

import pytest

from app.services.adapters.base import ExchangeAPIError
from app.services.adapters.binance import BinanceAdapter
from app.services.adapters.bitget import BitgetAdapter
from app.services.adapters.bybit import BybitAdapter
from app.services.adapters.okx import OKXAdapter


@pytest.mark.asyncio
async def test_binance_positions_extend_risk_fields_in_single_call():
    bn = BinanceAdapter(credentials={"api_key": "k", "secret_key": "s"})
    bn._request = AsyncMock(
        return_value=[
            {
                "symbol": "BTCUSDT",
                "positionSide": "LONG",
                "positionAmt": "0.5",
                "entryPrice": "68000",
                "markPrice": "70000",
                "breakEvenPrice": "68050",
                "liquidationPrice": "55000",
                "maintMargin": "175.0",
                "initialMargin": "3500.0",
                "openOrderInitialMargin": "100.0",
                "isolatedWallet": "0",
                "notional": "35000",
                "unRealizedProfit": "1000",
                "leverage": "10",
                "marginType": "cross",
            }
        ]
    )

    positions = await bn.get_positions()
    assert len(positions) == 1
    assert bn._request.call_count == 1
    pos = positions[0]
    assert pos.liquidation_price == pytest.approx(55000.0)
    assert pos.maintenance_margin == pytest.approx(175.0)
    assert pos.initial_margin == pytest.approx(3500.0)
    assert pos.open_order_initial_margin == pytest.approx(100.0)
    assert pos.break_even_price == pytest.approx(68050.0)
    assert pos.to_dict()["liquidationPrice"] == pytest.approx(55000.0)


@pytest.mark.asyncio
async def test_binance_cashflow_categories_multi_fill_id_and_unknown_preservation():
    bn = BinanceAdapter(credentials={"api_key": "k", "secret_key": "s"})
    bn._request = AsyncMock(
        side_effect=[
            [
                {"tranId": "100", "tradeId": "1", "incomeType": "COMMISSION", "income": "-0.25", "asset": "USDT", "symbol": "BTCUSDT", "time": 1_700_000_001_000},
                {"tranId": "100", "tradeId": "2", "incomeType": "COMMISSION", "income": "-0.30", "asset": "USDT", "symbol": "BTCUSDT", "time": 1_700_000_002_000},
                {"tranId": "101", "incomeType": "TRANSFER", "income": "500.0", "asset": "USDT", "symbol": "", "time": 1_700_000_003_000},
                {"tranId": "102", "incomeType": "NEW_EXCHANGE_REWARD", "income": "12.5", "asset": "USDT", "symbol": "", "time": 1_700_000_004_000},
            ],
        ]
    )

    page = await bn.get_cashflow_history(1_700_000_000_000, 1_700_000_010_000)
    assert page.complete is True
    assert len(page.items) == 4
    ext_ids = [i.external_id for i in page.items]
    assert "100:1" in ext_ids and "100:2" in ext_ids  # Multi-fill same tranId stays distinct
    by_type = {i.raw_type: i for i in page.items}
    assert by_type["TRANSFER"].category == "TRANSFER_IN"
    assert by_type["NEW_EXCHANGE_REWARD"].category == "UNKNOWN"
    assert by_type["NEW_EXCHANGE_REWARD"].signed_amount == Decimal("12.5")


@pytest.mark.asyncio
async def test_binance_orders_algo_separation_force_orders_and_daily_snapshot_capability():
    base_ms = int(time.time() * 1000) - 3_600_000
    bn_demo = BinanceAdapter(credentials={"api_key": "k", "secret_key": "s"}, environment="demo")
    caps_demo = bn_demo.get_dataset_capabilities()
    assert caps_demo["daily_snapshots"].supported is False
    demo_daily = await bn_demo.get_daily_account_snapshots(base_ms, base_ms + 60_000)
    assert demo_daily.supported is False

    bn_live = BinanceAdapter(credentials={"api_key": "k", "secret_key": "s"}, environment="live")

    async def fake_req(method, path, params=None, signed=False, **kwargs):
        if path == "/fapi/v1/allOrders":
            return [{"orderId": 1001, "clientOrderId": "mq_1", "symbol": "BTCUSDT", "side": "BUY", "positionSide": "LONG", "type": "LIMIT", "status": "FILLED", "origQty": "0.1", "executedQty": "0.1", "avgPrice": "70000", "price": "70000", "time": base_ms + 1000, "updateTime": base_ms + 5000}]
        if path == "/fapi/v1/allAlgoOrders":
            return [{"algoId": "alg_9", "actualOrderId": "1001", "clientAlgoId": "ca_9", "symbol": "BTCUSDT", "side": "SELL", "positionSide": "LONG", "orderType": "STOP_MARKET", "algoStatus": "NEW", "quantity": "0.1", "triggerPrice": "65000", "closePosition": False, "reduceOnly": True, "createTime": base_ms + 10_000, "updateTime": base_ms + 10_000}]
        if path == "/fapi/v1/forceOrders":
            assert int(params["limit"]) <= 100
            return [{"orderId": 9901, "symbol": "ETHUSDT", "side": "SELL", "positionSide": "LONG", "type": "LIMIT", "status": "FILLED", "origQty": "1", "executedQty": "1", "avgPrice": "3000", "time": base_ms + 20_000, "updateTime": base_ms + 20_000}]
        if path == "/fapi/v1/openOrders":
            return [{"orderId": 1002, "clientOrderId": "c2", "symbol": "BTCUSDT", "side": "BUY", "positionSide": "LONG", "type": "LIMIT", "status": "NEW", "origQty": "0.2", "executedQty": "0", "price": "66000", "time": base_ms + 1000, "updateTime": base_ms + 1000}]
        if path == "/fapi/v1/openAlgoOrders":
            return [{"algoId": "alg_9", "clientAlgoId": "ca_9", "symbol": "BTCUSDT", "side": "SELL", "positionSide": "LONG", "orderType": "STOP_MARKET", "algoStatus": "NEW", "quantity": "0.1", "triggerPrice": "65000", "closePosition": False, "reduceOnly": True}]
        if path == "/sapi/v1/accountSnapshot":
            raise ExchangeAPIError("binance", "-2015", "Invalid API-key, IP, or permissions")
        return []

    bn_live._request = AsyncMock(side_effect=fake_req)
    orders_page = await bn_live.get_order_history(base_ms, base_ms + 100_000, symbols=["BTCUSDT"])
    algo_rows = [o for o in orders_page.items if o.algo_id == "alg_9"]
    ord_rows = [o for o in orders_page.items if o.exchange_order_id == "1001"]
    assert len(ord_rows) == 1 and len(algo_rows) == 1
    assert algo_rows[0].exchange_order_id is None  # Avoid partial unique index collision with actualOrderId
    assert algo_rows[0].category == "stop_loss"

    force_page = await bn_live.get_force_orders(base_ms, base_ms + 100_000)
    assert len(force_page.items) == 1 and force_page.items[0].exchange_order_id == "9901"

    open_orders = await bn_live.get_open_orders_normalized()
    assert {o.category for o in open_orders} == {"ordinary", "stop_loss"}

    # Fails cleanly to unsupported rather than raising if SAPI scope missing
    daily_page = await bn_live.get_daily_account_snapshots(base_ms, base_ms + 86_400_000)
    assert daily_page.supported is False


@pytest.mark.asyncio
async def test_okx_mgnratio_inversion_and_oco_split_plus_bybit_bitget_caps():
    okx = OKXAdapter(credentials={"api_key": "k", "secret_key": "s", "passphrase": "p"})

    async def okx_req(method, path, params=None):
        if path == "/api/v5/account/positions":
            # OKX mgnRatio = 2.5 means eq / mmr = 2.5 -> mmr / eq = 0.40
            return [{"instId": "BTC-USDT-SWAP", "posSide": "long", "mgnMode": "cross", "pos": "1", "availPos": "1", "avgPx": "60000", "markPx": "61000", "upl": "100", "uplRatio": "0.01", "notionalUsd": "6100", "lever": "5", "liqPx": "50000", "mmr": "61", "imr": "1220", "mgnRatio": "2.5"}]
        if path == "/api/v5/account/balance":
            return [{"totalEq": "5000", "details": [{"ccy": "USDT", "eq": "5000", "availEq": "3700", "cashBal": "4900", "upl": "100", "imr": "1220", "mmr": "2000", "mgnRatio": "2.5"}]}]
        if path == "/api/v5/trade/orders-algo-pending":
            if params.get("ordType") == "oco":
                return [{"algoId": "oco-1", "instId": "BTC-USDT-SWAP", "side": "sell", "posSide": "long", "ordType": "oco", "state": "live", "sz": "1", "tpTriggerPx": "65000", "slTriggerPx": "58000", "reduceOnly": "true", "cTime": "1700000000000", "uTime": "1700000001000"}]
            return []
        if path == "/api/v5/trade/orders-pending":
            return []
        return []

    okx._request = AsyncMock(side_effect=okx_req)
    okx_pos = await okx.get_positions()
    assert okx_pos[0].margin_ratio == pytest.approx(0.4)
    risk = await okx.get_account_risk_snapshot()
    assert risk.margin_ratio == Decimal("0.4")
    open_orders = await okx.get_open_orders_normalized()
    cats = {o.order_id: o.category for o in open_orders}
    assert cats.get("oco-1:tp") == "take_profit"
    assert cats.get("oco-1:sl") == "stop_loss"

    by = BybitAdapter(credentials={"api_key": "k", "secret_key": "s"})
    bg = BitgetAdapter(credentials={"api_key": "k", "secret_key": "s", "passphrase": "p"})
    assert by.get_dataset_capabilities()["force_orders"].supported is False
    assert bg.get_dataset_capabilities()["commission_rates"].supported is False

    # Fail-closed get_open_orders() contract preserved when upstream raises
    bn_fail = BinanceAdapter(credentials={"api_key": "k", "secret_key": "s"})
    bn_fail._request = AsyncMock(side_effect=RuntimeError("network down"))
    with pytest.raises(RuntimeError):
        await bn_fail.get_open_orders()
