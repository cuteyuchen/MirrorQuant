"""Offline regressions for exchange mutation, metadata and capital boundaries."""
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.adapters.base import ExchangeAPIError, InstrumentSpec, OrderBook
from app.services.adapters.binance import BinanceAdapter
from app.services.adapters.bitget import BitgetAdapter
from app.services.adapters.bybit import BybitAdapter
from app.services.adapters.okx import OKXAdapter
from app.services.proxy.manager import ProxyManager


ADAPTERS = (BinanceAdapter, BitgetAdapter, BybitAdapter, OKXAdapter)


def adapter(kind):
    return kind({"api_key": "test", "secret_key": "test", "passphrase": "test"})


@pytest.mark.parametrize("kind", ADAPTERS)
async def test_order_mutation_is_never_retried_after_timeout(monkeypatch, kind):
    calls = []

    def timed_out(request):
        calls.append(request)
        raise httpx.ReadTimeout("response lost after acceptance", request=request)

    monkeypatch.setattr(ProxyManager, "build_client", staticmethod(
        lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(timed_out))))
    with pytest.raises(httpx.ReadTimeout):
        await adapter(kind)._request("POST", "/test-order")
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ADAPTERS)
async def test_idempotent_reads_may_retry(monkeypatch, kind):
    calls = []

    def timed_out(request):
        calls.append(request)
        raise httpx.ReadTimeout("read timed out", request=request)

    monkeypatch.setattr(ProxyManager, "build_client", staticmethod(
        lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(timed_out))))
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    with pytest.raises(httpx.ReadTimeout):
        await adapter(kind)._request("GET", "/test-read")
    assert len(calls) == 3


@pytest.mark.parametrize("kind,absent", (
    (BinanceAdapter, "-2013"), (BinanceAdapter, "-2011"),
    (BitgetAdapter, "40768"), (BitgetAdapter, "40762"),
    (BybitAdapter, "110001"), (BybitAdapter, "170213"),
    (OKXAdapter, "51603"), (OKXAdapter, "51001"),
))
async def test_only_documented_absent_orders_return_none(kind, absent):
    venue = adapter(kind)
    venue._request = AsyncMock(side_effect=ExchangeAPIError(venue.exchange, absent, "absent"))
    assert await venue.get_order("BTCUSDT", cl_ord_id="known-id") is None
    error = ExchangeAPIError(venue.exchange, "429", "rate limited")
    venue._request.side_effect = error
    with pytest.raises(ExchangeAPIError) as caught:
        await venue.get_order("BTCUSDT", cl_ord_id="known-id")
    assert caught.value is error


@pytest.mark.parametrize("kind", ADAPTERS)
async def test_zero_fill_is_preserved(kind):
    venue = adapter(kind)
    rows = {
        BinanceAdapter: {"orderId": 1, "executedQty": "0", "status": "NEW"},
        BitgetAdapter: {"orderId": "1", "baseVolume": "0", "state": "live"},
        BybitAdapter: {"list": [{"orderId": "1", "cumExecQty": "0", "orderStatus": "New"}]},
        OKXAdapter: [{"ordId": "1", "accFillSz": "0", "state": "live"}],
    }
    venue._request = AsyncMock(return_value=rows[kind])
    assert (await venue.get_order("BTCUSDT", ord_id="1"))["accFillSz"] == 0


async def test_bybit_queries_history_after_empty_realtime():
    venue = adapter(BybitAdapter)
    venue._request = AsyncMock(side_effect=[{"list": []}, {"list": [{"orderId": "1", "cumExecQty": "2"}]}])
    assert (await venue.get_order("BTCUSDT", cl_ord_id="known"))["accFillSz"] == 2
    assert venue._request.call_args.args[1] == "/v5/order/history"


async def test_binance_equity_includes_isolated_losses():
    venue = adapter(BinanceAdapter)
    venue._request = AsyncMock(return_value={"assets": [{"asset": "USDT", "marginBalance": "80", "walletBalance": "100", "unrealizedProfit": "-20", "availableBalance": "0"}]})
    balance = await venue.get_usdt_balance()
    assert (balance.eq, balance.avail_eq, balance.upl) == (80, 0, -20)


async def test_okx_has_no_total_account_equity_fallback_and_normalizes_net_side():
    venue = adapter(OKXAdapter)
    venue._request = AsyncMock(return_value=[{"totalEq": "10000", "details": []}])
    assert (await venue.get_usdt_balance()).eq == 0
    venue._request.return_value = [{"instId": "BTC-USDT-SWAP", "posSide": "net", "pos": "-3"}]
    positions = await venue.get_positions()
    assert (positions[0].pos_side, positions[0].pos) == ("short", 3)


@pytest.mark.parametrize("available", (None, "", "0", 0))
async def test_bybit_missing_or_zero_available_balance_never_uses_equity(available):
    venue = adapter(BybitAdapter)
    venue._request = AsyncMock(return_value={"list": [{"totalAvailableBalance": available, "coin": [{"coin": "USDT", "equity": "10000", "walletBalance": "10000"}]}]})
    balance = await venue.get_usdt_balance()
    assert balance.eq == 10000 and balance.avail_eq == 0


async def test_bybit_multi_collateral_margin_is_not_qualified_usdt_capital():
    venue = adapter(BybitAdapter)
    venue._request = AsyncMock(return_value={"list": [{"totalAvailableBalance": "500", "coin": [{"coin": "USDT", "equity": "1000"}, {"coin": "BTC", "equity": "1"}]}]})
    assert (await venue.get_usdt_balance()).avail_eq == 0


async def test_bitget_zero_available_is_preserved():
    venue = adapter(BitgetAdapter)
    venue._request = AsyncMock(return_value=[{"marginCoin": "USDT", "accountEquity": "10000", "crossedMaxAvailable": 0, "available": "10000"}])
    assert (await venue.get_usdt_balance()).avail_eq == 0


async def test_bybit_position_pagination_and_repeated_cursor_failure():
    venue = adapter(BybitAdapter)
    venue._request = AsyncMock(side_effect=[
        {"list": [{"symbol": "BTCUSDT", "size": "1", "positionIdx": 1}], "nextPageCursor": "next"},
        {"list": [{"symbol": "ETHUSDT", "size": "2", "positionIdx": 2}]},
    ])
    assert [p.symbol for p in await venue.get_positions()] == ["BTCUSDT", "ETHUSDT"]
    assert venue._request.call_args.kwargs["params"]["cursor"] == "next"
    venue._request = AsyncMock(return_value={"list": [], "nextPageCursor": "repeat"})
    with pytest.raises(RuntimeError, match="Repeated exchange pagination"):
        await venue.get_positions()


def okx_metadata(**overrides):
    return dict({"instId": "BTC-USDT-SWAP", "state": "live", "ctType": "linear", "settleCcy": "USDT", "ctValCcy": "BTC", "ctVal": "0.01", "minSz": "1", "lotSz": "1", "tickSz": "0.1", "lever": "100", "maxLmtSz": "10000", "maxMktSz": "1000"}, **overrides)


async def test_okx_only_complete_linear_base_unit_contracts_are_admitted():
    venue = adapter(OKXAdapter)
    venue._request = AsyncMock(return_value=[
        okx_metadata(), okx_metadata(instId="BTC-USD-SWAP", ctType="inverse", settleCcy="BTC"),
        okx_metadata(instId="ETH-USDT-SWAP", ctValCcy="ETH", ctVal=None),
        okx_metadata(instId="SOL-USDT-SWAP", ctValCcy="USD"),
    ])
    specs = await venue.get_instruments()
    assert list(specs) == ["BTC-USDT-SWAP"] and specs["BTC-USDT-SWAP"].ct_val == Decimal("0.01")
    venue._request.return_value = []
    assert await venue.get_instruments(force_refresh=True) == {}


async def test_bitget_price_end_step_defines_actual_tick():
    venue = adapter(BitgetAdapter)
    venue._request = AsyncMock(return_value=[{"symbol": "BTCUSDT", "symbolStatus": "normal", "symbolType": "perpetual", "quoteCoin": "USDT", "baseCoin": "BTC", "pricePlace": "2", "priceEndStep": "5", "sizeMultiplier": "0.001", "minTradeNum": "0.001", "maxOrderQty": "100", "maxMarketOrderQty": "10", "maxLever": "100", "minTradeUSDT": "5"}])
    assert (await venue.get_instruments())["BTCUSDT"].tick_sz == Decimal("0.05")


@pytest.mark.parametrize("kind", ADAPTERS)
def test_symbol_mapping_cannot_change_economic_underlying(kind):
    venue = adapter(kind)
    spec = InstrumentSpec("ETHUSDT", Decimal(1), Decimal("0.01"), Decimal("0.01"), Decimal("0.01"), base_ccy="ETH")
    assert venue.resolve_symbol("BTCUSDT", {"ETHUSDT": spec}, {"BTCUSDT": "ETHUSDT"}) == ("ETHUSDT", False)


def test_market_specific_and_final_notional_filters():
    spec = InstrumentSpec("BTCUSDT", Decimal(1), Decimal("0.001"), Decimal("0.001"), Decimal("0.1"), max_sz=Decimal(100), market_min_sz=Decimal("0.01"), market_lot_sz=Decimal("0.01"), market_max_sz=Decimal(10), min_notional=Decimal(5))
    spec.validate_order("ioc", "0.005", "1000")
    with pytest.raises(ValueError, match="quantity"):
        spec.validate_order("market", "0.005", "1000")
    with pytest.raises(ValueError, match="quantity"):
        spec.validate_order("market", "11", "1000")
    with pytest.raises(ValueError, match="notional"):
        spec.validate_order("ioc", "0.004", "1000")
    with pytest.raises(ValueError, match="price"):
        spec.validate_order("ioc", "1", "1000.05")


@pytest.mark.parametrize("kind,requested,expected", ((BinanceAdapter, 1, 5), (BinanceAdapter, 2, 5), (BitgetAdapter, 2, "5")))
async def test_book_depth_is_normalized_to_supported_wire_values(kind, requested, expected):
    venue = adapter(kind)
    venue._request = AsyncMock(return_value={"bids": [["99", "1"]] * 5, "asks": [["100", "1"]] * 5})
    book = await venue.get_orderbook("BTCUSDT", depth=requested)
    assert venue._request.call_args.kwargs["params"]["limit"] == expected
    assert len(book.bids) == requested


@pytest.mark.parametrize("kind", ADAPTERS)
async def test_unqualified_modes_block_entries_before_mutation(kind):
    venue = adapter(kind)
    venue._request = AsyncMock()
    with pytest.raises(ExchangeAPIError, match="cross margin and hedge"):
        await venue.ensure_leverage("BTCUSDT", 10, mgn_mode="isolated")
    venue._request.assert_not_awaited()


async def test_okx_59000_does_not_report_leverage_success():
    venue = adapter(OKXAdapter)
    venue._request = AsyncMock(side_effect=[[{"posMode": "long_short_mode", "acctLv": "2"}], ExchangeAPIError("okx", "59000", "setting failed")])
    assert await venue.ensure_leverage("BTC-USDT-SWAP", 10) is False


async def test_binance_requires_matching_leverage_readback():
    venue = adapter(BinanceAdapter)
    venue._request = AsyncMock(side_effect=[{"dualSidePosition": True}, {"multiAssetsMargin": False}, [{"marginType": "cross"}], {"leverage": 10}, [{"leverage": 5}]])
    assert await venue.ensure_leverage("BTCUSDT", 10) is False


async def test_binance_projected_notional_selects_actual_tier():
    venue = adapter(BinanceAdapter)
    venue._request = AsyncMock(return_value=[{"symbol": "BTCUSDT", "brackets": [
        {"notionalFloor": "0", "notionalCap": "1000", "initialLeverage": 100},
        {"notionalFloor": "1000", "notionalCap": "10000", "initialLeverage": 20},
    ]}])
    assert await venue.get_max_leverage("BTCUSDT", 999) == 100
    assert await venue.get_max_leverage("BTCUSDT", 1000) == 20
    with pytest.raises(ValueError, match="exceeds"):
        await venue.get_max_leverage("BTCUSDT", 10000)


async def test_binance_protection_uses_algo_order_endpoint():
    venue = adapter(BinanceAdapter)
    venue._request = AsyncMock(return_value={"algoId": "protection"})
    result = await venue.place_tp_sl("BTCUSDT", "long", "1", sl_px="90")
    assert result["slOrderId"] == "protection"
    call = venue._request.call_args
    assert call.args == ("POST", "/fapi/v1/algoOrder")
    assert call.kwargs["params"]["triggerPrice"] == "90"
    assert "stopPrice" not in call.kwargs["params"]


async def test_bitget_oneway_protection_uses_buy_sell_hold_side():
    venue = adapter(BitgetAdapter)
    venue.position_mode = "one_way"
    venue._request = AsyncMock(return_value={"orderId": "protection"})
    await venue.place_tp_sl("BTCUSDT", "short", "1", sl_px="110")
    assert venue._request.call_args.kwargs["body"]["holdSide"] == "sell"


def test_clock_calibration_rejects_uncertain_sample_and_retains_previous_offset(monkeypatch):
    venue = adapter(OKXAdapter)
    venue._time_offset_ms = 123
    monkeypatch.setattr("app.services.adapters.base.time.time", lambda: 1000)
    with pytest.raises(RuntimeError, match="uncertain"):
        venue._calibrate_time(1_000_000, 995_000)
    assert venue._time_offset_ms == 123
    assert venue._calibrate_time(1_000_050, 999_900) == 100


@pytest.mark.parametrize("kind", ADAPTERS)
async def test_pending_inventory_includes_conditional_orders(kind):
    venue = adapter(kind)
    if kind is BinanceAdapter:
        venue._request = AsyncMock(side_effect=[[{"orderId": "ordinary"}], [{"algoId": "conditional"}]])
    elif kind is BybitAdapter:
        venue._request = AsyncMock(side_effect=[{"list": [{"orderId": "ordinary"}], "nextPageCursor": "next"}, {"list": [{"orderId": "conditional"}]}])
    elif kind is BitgetAdapter:
        venue._request = AsyncMock(side_effect=[{"entrustedList": [{"orderId": "ordinary"}]}, {"entrustedList": [{"orderId": "conditional"}]}, {"entrustedList": []}, {"entrustedList": []}])
    else:
        venue._request = AsyncMock(side_effect=[[{"ordId": "ordinary"}], [{"algoId": "conditional"}]] + [[] for _ in range(5)])
    assert len(await venue.get_open_orders()) == 2


async def test_bybit_malformed_inventory_does_not_mean_empty():
    venue = adapter(BybitAdapter)
    venue._request = AsyncMock(return_value={})
    with pytest.raises(ValueError, match="Incomplete"):
        await venue.get_open_orders()


async def test_bitget_truncated_inventory_fails_closed():
    venue = adapter(BitgetAdapter)
    venue._request = AsyncMock(return_value={"entrustedList": [{"orderId": str(i)} for i in range(100)]})
    with pytest.raises(ValueError, match="exceeds"):
        await venue.get_open_orders()


@pytest.mark.parametrize("kind", ADAPTERS)
@pytest.mark.parametrize("http_status", (429, 500))
async def test_error_responses_never_replay_mutations_and_share_rate_cooldown(monkeypatch, kind, http_status):
    from app.services.adapters.base import BaseExchangeAdapter

    monkeypatch.setattr(BaseExchangeAdapter, "_rate_limit_until", {})
    calls = []

    def failed(request):
        calls.append(request)
        return httpx.Response(http_status, json={"code": -1003 if http_status == 429 else -1000, "msg": "test failure"}, headers={"Retry-After": "30"})

    monkeypatch.setattr(ProxyManager, "build_client", staticmethod(
        lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(failed))))
    with pytest.raises(ExchangeAPIError):
        await adapter(kind)._request("POST", "/test-order")
    assert len(calls) == 1
    if http_status == 429:
        with pytest.raises(ExchangeAPIError, match="cooldown"):
            await adapter(kind)._request("GET", "/other-account-read")
        assert len(calls) == 1
