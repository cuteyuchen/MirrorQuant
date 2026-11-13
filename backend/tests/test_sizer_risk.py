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

    # Spread
    spread_ok = RiskEngine.check_spread(best_bid=60000.0, best_ask=60030.0, max_spread_pct=0.1)
    assert spread_ok.allowed is True
    spread_bad = RiskEngine.check_spread(best_bid=60000.0, best_ask=60300.0, max_spread_pct=0.1)
    assert spread_bad.allowed is False

    # Drawdown Breaker
    dd_ok = RiskEngine.check_drawdown(hwm_equity=10000.0, current_equity=8500.0, max_drawdown_pct=20.0)
    assert dd_ok.allowed is True
    dd_bad = RiskEngine.check_drawdown(hwm_equity=10000.0, current_equity=7500.0, max_drawdown_pct=20.0)
    assert dd_bad.allowed is False

    # Daily Loss Breaker
    dl_ok = RiskEngine.check_daily_loss(day_start_equity=10000.0, current_equity=9600.0, daily_max_loss_pct=5.0)
    assert dl_ok.allowed is True
    dl_bad = RiskEngine.check_daily_loss(day_start_equity=10000.0, current_equity=9400.0, daily_max_loss_pct=5.0)
    assert dl_bad.allowed is False


def test_missed_order_catchup_and_stale_guards():
    now_ms = 1700000100000  # 100s after

    # Case 1: Fresh order (age 10s <= max_age 120s) -> allowed
    order_time_fresh = now_ms - 10000
    res_fresh = RiskEngine.check_missed_order_catchup(
        order_time_ms=order_time_fresh,
        now_ms=now_ms,
        max_age_sec=120,
        ref_price=60050.0,
        lead_avg_price=60000.0,
        catchup_max_price_dev_pct=0.8,
    )
    assert res_fresh.allowed is True

    # Case 2: Stale order (age 150s > max_age 120s) -> blocked as stale
    order_time_stale = now_ms - 150000
    res_stale = RiskEngine.check_missed_order_catchup(
        order_time_ms=order_time_stale,
        now_ms=now_ms,
        max_age_sec=120,
        ref_price=60050.0,
        lead_avg_price=60000.0,
        catchup_max_price_dev_pct=0.8,
    )
    assert res_stale.allowed is False
    assert res_stale.code == "SKIPPED_STALE_MISSED_ORDER"

    # Case 3: Within max age (40s), but price has run away (> 0.8% drift) -> blocked
    order_time_delayed = now_ms - 40000
    res_drift = RiskEngine.check_missed_order_catchup(
        order_time_ms=order_time_delayed,
        now_ms=now_ms,
        max_age_sec=120,
        ref_price=60600.0,  # 1.0% higher
        lead_avg_price=60000.0,
        catchup_max_price_dev_pct=0.8,
    )
    assert res_drift.allowed is False
    assert res_drift.code == "BLOCKED_CATCHUP_DRIFT"

    # Case 4: BAPI aggregation lag (150s) but price is still inside the lead's cost
    # window -> allowed. Price drift, not wall-clock age, is the authoritative gate.
    res_lag_in_price = RiskEngine.check_missed_order_catchup(
        order_time_ms=now_ms - 150000,
        now_ms=now_ms,
        max_age_sec=900,
        ref_price=60100.0,  # 0.17% above lead entry
        lead_avg_price=60000.0,
        catchup_max_price_dev_pct=0.5,
    )
    assert res_lag_in_price.allowed is True

    # Case 5: Ancient order (> wide backstop) is still rejected even at a perfect price.
    res_ancient = RiskEngine.check_missed_order_catchup(
        order_time_ms=now_ms - 1000000,
        now_ms=now_ms,
        max_age_sec=900,
        ref_price=60000.0,
        lead_avg_price=60000.0,
        catchup_max_price_dev_pct=0.5,
    )
    assert res_ancient.allowed is False
    assert res_ancient.code == "SKIPPED_STALE_MISSED_ORDER"


def test_insufficient_margin_auto_scale_down():
    spec = InstrumentSpec(
        inst_id="BTC-USDT-SWAP",
        ct_val=Decimal("0.01"),  # 1 contract = 0.01 BTC ($600 at $60,000)
        min_sz=Decimal("1"),
        lot_sz=Decimal("1"),
        tick_sz=Decimal("0.1"),
        min_notional=Decimal("5"),
    )
    ref_price = 60000.0
    # Follower has $100 available equity, leverage 5x -> total purchasing power = $500 (max usable 92% = $92 margin -> $460 notional)
    # Requested 2 contracts ($1200 notional, requires $240 margin)
    qty, err, note = RiskEngine.adjust_for_available_margin(
        requested_qty=Decimal("2"),
        ref_price=ref_price,
        spec=spec,
        avail_equity=100.0,
        leverage=5,
        policy="auto_scale_down",
        max_usage=0.92,
    )
    # Available margin is $92, purchasing power is $460 -> below 1 contract ($600)
    assert qty == Decimal("0")
    assert err is not None and "below min order" in err

    # If follower has $200 available equity (usable $184 margin * 5x = $920 purchasing power -> fits 1 contract ($600))
    qty2, err2, note2 = RiskEngine.adjust_for_available_margin(
        requested_qty=Decimal("2"),
        ref_price=ref_price,
        spec=spec,
        avail_equity=200.0,
        leverage=5,
        policy="auto_scale_down",
        max_usage=0.92,
    )
    assert err2 is None
    assert qty2 == Decimal("1")
    assert note2 is not None and "Auto-scaled" in note2

    # If policy is skip_alert
    qty3, err3, _ = RiskEngine.adjust_for_available_margin(
        requested_qty=Decimal("2"),
        ref_price=ref_price,
        spec=spec,
        avail_equity=200.0,
        leverage=5,
        policy="skip_alert",
        max_usage=0.92,
    )
    assert qty3 == Decimal("0")
    assert err3 is not None and "Insufficient margin" in err3


def test_tp_sl_price_calculation():
    # Follow lead mode produces None
    tp, sl = RiskEngine.compute_tp_sl_prices(
        pos_side="long",
        entry_px=100.0,
        tp_sl_mode="follow_lead",
        enable_tp=True,
        tp_pct=10.0,
        enable_sl=True,
        sl_pct=5.0,
        hard_sl_pct=15.0,
    )
    assert tp is None
    assert sl is None

    # Independent mode
    tp_ind, sl_ind = RiskEngine.compute_tp_sl_prices(
        pos_side="long",
        entry_px=100.0,
        tp_sl_mode="independent",
        enable_tp=True,
        tp_pct=10.0,
        enable_sl=True,
        sl_pct=5.0,
        hard_sl_pct=15.0,
    )
    assert tp_ind is not None and round(tp_ind, 2) == 110.0
    assert sl_ind is not None and round(sl_ind, 2) == 95.0

    # Short independent mode
    tp_s, sl_s = RiskEngine.compute_tp_sl_prices(
        pos_side="short",
        entry_px=100.0,
        tp_sl_mode="independent",
        enable_tp=True,
        tp_pct=10.0,
        enable_sl=True,
        sl_pct=5.0,
        hard_sl_pct=15.0,
    )
    assert tp_s is not None and round(tp_s, 2) == 90.0
    assert sl_s is not None and round(sl_s, 2) == 105.0

    # Hybrid guard mode: hard SL attached
    _, sl_hyb = RiskEngine.compute_tp_sl_prices(
        pos_side="long",
        entry_px=100.0,
        tp_sl_mode="hybrid_guard",
        enable_tp=False,
        tp_pct=0.0,
        enable_sl=False,
        sl_pct=0.0,
        hard_sl_pct=20.0,
    )
    assert sl_hyb is not None and round(sl_hyb, 2) == 80.0
