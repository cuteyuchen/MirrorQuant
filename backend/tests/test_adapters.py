from decimal import Decimal
from app.services.adapters.base import InstrumentSpec
from app.services.adapters.binance import BinanceAdapter
from app.services.adapters.okx import OKXAdapter


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
