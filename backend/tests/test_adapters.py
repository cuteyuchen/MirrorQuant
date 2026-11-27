from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.services.adapters.base import InstrumentSpec
from app.services.adapters.binance import BinanceAdapter
from app.services.adapters.okx import OKXAdapter
from app.services.adapters.bybit import BybitAdapter
from app.services.adapters.bitget import BitgetAdapter
from app.services.adapters.factory import create_exchange_adapter


def test_okx_symbol_resolution():
    adapter = OKXAdapter(
        credentials={"api_key": "k", "secret_key": "s", "passphrase": "p"},
        environment="demo",
    )
    spec = InstrumentSpec(
        inst_id="BTC-USDT-SWAP",
        ct_val=Decimal("0.01"),
        min_sz=Decimal("1"),
        lot_sz=Decimal("1"),
        tick_sz=Decimal("0.1"),
    )
    instruments = {"BTC-USDT-SWAP": spec}

    inst_id, ok = adapter.resolve_symbol("BTCUSDT", instruments)
    assert inst_id == "BTC-USDT-SWAP"
    assert ok is True

    inst_id2, ok2 = adapter.resolve_symbol("SOLUSDT", instruments)
    assert inst_id2 == "SOL-USDT-SWAP"
    assert ok2 is False


def test_binance_symbol_resolution():
    adapter = BinanceAdapter(
        credentials={"api_key": "k", "secret_key": "s"},
        environment="demo",
    )
    spec = InstrumentSpec(
        inst_id="BTCUSDT",
        ct_val=Decimal("1"),
        min_sz=Decimal("0.001"),
        lot_sz=Decimal("0.001"),
        tick_sz=Decimal("0.1"),
    )
    instruments = {"BTCUSDT": spec}

    inst_id, ok = adapter.resolve_symbol("BTCUSDT", instruments)
    assert inst_id == "BTCUSDT"
    assert ok is True


@pytest.fixture
def binance_symbol_metadata():
    return {
        "symbol": "NVDAUSDT",
        "baseAsset": "NVDA",
        "status": "TRADING",
        "quoteAsset": "USDT",
        "marginAsset": "USDT",
        "contractType": "TRADIFI_PERPETUAL",
        "filters": [
            {"filterType": "LOT_SIZE", "minQty": "0.01", "stepSize": "0.01", "maxQty": "10000"},
            {"filterType": "MARKET_LOT_SIZE", "minQty": "0.1", "stepSize": "0.1", "maxQty": "1000"},
            {"filterType": "PRICE_FILTER", "tickSize": "0.001"},
            {"filterType": "MIN_NOTIONAL", "notional": "5"},
        ],
    }


async def test_binance_get_instruments_includes_crypto_and_tradfi_perpetuals(binance_symbol_metadata):
    adapter = BinanceAdapter(credentials={"api_key": "k", "secret_key": "s"})
    contracts = (
        ("BTC", "PERPETUAL"),
        ("NVDA", "TRADIFI_PERPETUAL"),
        ("TSLA", "TRADIFI_PERPETUAL"),
        ("XAU", "TRADIFI_PERPETUAL"),
    )
    adapter._request = AsyncMock(return_value={
        "symbols": [
            {**binance_symbol_metadata, "symbol": f"{base}USDT", "baseAsset": base, "contractType": contract_type}
            for base, contract_type in contracts
        ],
    })

    instruments = await adapter.get_instruments()

    assert instruments == {
        f"{base}USDT": InstrumentSpec(
            inst_id=f"{base}USDT",
            ct_val=Decimal("1"),
            min_sz=Decimal("0.01"),
            lot_sz=Decimal("0.01"),
            max_sz=Decimal("10000"),
            market_min_sz=Decimal("0.1"),
            market_lot_sz=Decimal("0.1"),
            market_max_sz=Decimal("1000"),
            tick_sz=Decimal("0.001"),
            max_lvr=125,
            min_notional=Decimal("5"),
            settle_ccy="USDT",
            base_ccy=base,
        )
        for base, _ in contracts
    }
    adapter._request.assert_awaited_once_with("GET", "/fapi/v1/exchangeInfo", auth=False)
    assert adapter.resolve_symbol("NVDAUSDT", instruments) == ("NVDAUSDT", True)


@pytest.mark.parametrize("overrides", (
    {"status": "PENDING_TRADING"},
    {"quoteAsset": "USDC"},
    {"marginAsset": "USDC"},
    {"contractType": "CURRENT_QUARTER"},
    {"contractType": "NEXT_QUARTER"},
    {"contractType": "UNKNOWN"},
    {"contractType": None},
))
async def test_binance_get_instruments_excludes_ineligible_contracts(binance_symbol_metadata, overrides):
    adapter = BinanceAdapter(credentials={"api_key": "k", "secret_key": "s"})
    adapter._request = AsyncMock(return_value={"symbols": [{**binance_symbol_metadata, **overrides}]})

    assert await adapter.get_instruments() == {}


def test_bybit_symbol_resolution_and_signature():
    adapter = BybitAdapter(
        credentials={"api_key": "test_bybit_k", "secret_key": "test_bybit_s"},
        environment="demo",
    )
    spec = InstrumentSpec(
        inst_id="BTCUSDT",
        ct_val=Decimal("1"),
        min_sz=Decimal("0.001"),
        lot_sz=Decimal("0.001"),
        tick_sz=Decimal("0.1"),
    )
    instruments = {"BTCUSDT": spec}

    inst_id, ok = adapter.resolve_symbol("BTCUSDT", instruments)
    assert inst_id == "BTCUSDT"
    assert ok is True

    # Test signature generation
    sig = adapter._sign("1700000000000", "5000", "category=linear&symbol=BTCUSDT")
    assert isinstance(sig, str) and len(sig) == 64


def test_bitget_symbol_resolution_and_signature():
    adapter = BitgetAdapter(
        credentials={"api_key": "test_bg_k", "secret_key": "test_bg_s", "passphrase": "test_bg_pass"},
        environment="demo",
    )
    spec = InstrumentSpec(
        inst_id="BTCUSDT",
        ct_val=Decimal("1"),
        min_sz=Decimal("0.001"),
        lot_sz=Decimal("0.001"),
        tick_sz=Decimal("0.1"),
    )
    instruments = {"BTCUSDT": spec}

    inst_id, ok = adapter.resolve_symbol("BTCUSDT", instruments)
    assert inst_id == "BTCUSDT"
    assert ok is True

    # Test bitget signature generation
    sig = adapter._sign("1700000000000", "GET", "/api/v2/mix/account/accounts", "")
    assert isinstance(sig, str) and len(sig) > 0


def test_adapter_factory_registration():
    for ex in ["okx", "binance", "bybit", "bitget"]:
        creds = {"api_key": "k", "secret_key": "s", "passphrase": "p"}
        ad = create_exchange_adapter(ex, creds, "demo")
        assert ad is not None
        assert hasattr(ad, "place_tp_sl")
        assert hasattr(ad, "cancel_tp_sl")
        assert hasattr(ad, "sync_time")


async def test_adapters_compute_margin_based_upl_ratio():
    bn = BinanceAdapter(credentials={"api_key": "k", "secret_key": "s"})
    bn._request = AsyncMock(return_value=[{
        "symbol": "ETHUSDT", "positionSide": "LONG", "positionAmt": "2",
        "entryPrice": "2000", "markPrice": "2100", "notional": "4200",
        "unRealizedProfit": "200", "leverage": "10", "marginType": "cross",
        "initialMargin": "420",
    }])
    bn_pos = await bn.get_positions()
    assert len(bn_pos) == 1
    assert bn_pos[0].margin_usd == pytest.approx(420.0)
    assert bn_pos[0].upl_ratio == pytest.approx(200.0 / 420.0)

    by = BybitAdapter(credentials={"api_key": "k", "secret_key": "s"})
    by._all_pages = AsyncMock(return_value=[{
        "symbol": "ETHUSDT", "side": "Buy", "positionIdx": 1, "size": "2",
        "avgPrice": "2000", "markPrice": "2100", "positionValue": "4200",
        "unrealisedPnl": "200", "leverage": "10", "tradeMode": 0, "positionIM": "420",
    }])
    by_pos = await by.get_positions()
    assert by_pos[0].margin_usd == pytest.approx(420.0)
    assert by_pos[0].upl_ratio == pytest.approx(200.0 / 420.0)

    bg = BitgetAdapter(credentials={"api_key": "k", "secret_key": "s", "passphrase": "p"})
    bg._request = AsyncMock(return_value=[{
        "symbol": "ETHUSDT", "holdSide": "long", "total": "2", "available": "2",
        "openPriceAvg": "2000", "markPrice": "2100", "unrealizedPL": "200",
        "leverage": "10", "marginMode": "crossed", "marginSize": "420",
    }])
    bg_pos = await bg.get_positions()
    assert bg_pos[0].margin_usd == pytest.approx(420.0)
    assert bg_pos[0].upl_ratio == pytest.approx(200.0 / 420.0)

    okx = OKXAdapter(credentials={"api_key": "k", "secret_key": "s", "passphrase": "p"})
    okx._request = AsyncMock(return_value=[{
        "instId": "ETH-USDT-SWAP", "posSide": "long", "pos": "2", "availPos": "2",
        "avgPx": "2000", "markPx": "2100", "notionalUsd": "4200", "upl": "200",
        "lever": "10", "mgnMode": "cross", "imr": "420",
    }])
    okx_pos = await okx.get_positions()
    assert okx_pos[0].margin_usd == pytest.approx(420.0)
    assert okx_pos[0].upl_ratio == pytest.approx(200.0 / 420.0)
