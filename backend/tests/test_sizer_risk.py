from decimal import Decimal
from app.services.adapters.base import InstrumentSpec
from app.services.engine.risk import RiskEngine
from app.services.engine.sizer import PositionSizer, format_decimal, quantize_down, quantize_step


def test_quantize_and_format():
    lot_sz = Decimal("0.001")
    tick_sz = Decimal("0.01")

    assert quantize_down(Decimal("1.23456"), lot_sz) == Decimal("1.234")
    assert quantize_step(Decimal("100.236"), tick_sz) == Decimal("100.24")
    assert format_decimal(Decimal("1.234"), lot_sz) == "1.234"
    assert format_decimal(Decimal("100.20"), tick_sz) == "100.20"


def test_position_sizer_proportional():
    follower_equity = 2000.0
    lead_margin_balance = 20000.0  # 1:10 ratio
    risk_scale = 1.0
    lead_notional = 1000.0  # $1,000 lead notional -> $100 follower notional

    target_usd = PositionSizer.calculate_target_usd(
        follower_equity=follower_equity,
        lead_margin_balance=lead_margin_balance,
        risk_scale=risk_scale,
        lead_notional=lead_notional,
    )
    assert target_usd == 100.0

    spec = InstrumentSpec(
        inst_id="BTC-USDT-SWAP",
        ct_val=Decimal("0.01"),  # 1 contract = 0.01 BTC
        min_sz=Decimal("1"),
        lot_sz=Decimal("1"),
        tick_sz=Decimal("0.1"),
        min_notional=Decimal("5"),
    )
    # BTC at $60,000 -> 1 contract = $600
    # $100 target is below minimum contract ($600) -> should accumulate debt
    res = PositionSizer.size_open_order(target_usd=100.0, ref_price=60000.0, spec=spec)
    assert res.below_min is True
    assert res.contracts == Decimal("0")

    # If target is $1,300 -> 2 contracts = $1,200
    res2 = PositionSizer.size_open_order(target_usd=1300.0, ref_price=60000.0, spec=spec)
    assert res2.below_min is False
    assert res2.contracts == Decimal("2")
    assert res2.estimated_notional == 1200.0


def test_risk_engine_checks():
    # Whitelist & Blacklist
    assert RiskEngine.check_symbol_filter("BTCUSDT", whitelist=["BTCUSDT", "ETHUSDT"], blacklist=[]).allowed is True
    assert RiskEngine.check_symbol_filter("DOGEUSDT", whitelist=["BTCUSDT", "ETHUSDT"], blacklist=[]).allowed is False
    assert RiskEngine.check_symbol_filter("BTCUSDT", whitelist=[], blacklist=["BTCUSDT"]).allowed is False

    # Slippage
    slip_ok = RiskEngine.check_slippage(ref_price=60200.0, lead_avg_price=60000.0, max_slippage_pct=0.5)
    assert slip_ok.allowed is True
    slip_bad = RiskEngine.check_slippage(ref_price=61000.0, lead_avg_price=60000.0, max_slippage_pct=0.5)
    assert slip_bad.allowed is False

    # Drawdown Breaker
    dd_ok = RiskEngine.check_drawdown(hwm_equity=10000.0, current_equity=8500.0, max_drawdown_pct=20.0)
    assert dd_ok.allowed is True
    dd_bad = RiskEngine.check_drawdown(hwm_equity=10000.0, current_equity=7500.0, max_drawdown_pct=20.0)
    assert dd_bad.allowed is False
