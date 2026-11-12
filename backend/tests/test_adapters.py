from decimal import Decimal
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
