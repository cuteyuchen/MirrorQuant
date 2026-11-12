from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional, Sequence, Tuple

from app.services.adapters.base import ExchangePosition, InstrumentSpec
from app.services.engine.sizer import quantize_down


@dataclass
class RiskCheckResult:
    allowed: bool
    reason: Optional[str] = None
    value: float = 0.0
    code: str = "OK"


class RiskEngine:
    """Risk guards: symbol filters, slippage, spread, drawdown, daily loss, catch-up, margin & TP/SL."""

    @staticmethod
    def check_symbol_filter(
        symbol: str,
        whitelist: Sequence[str],
        blacklist: Sequence[str],
    ) -> RiskCheckResult:
        sym = symbol.upper()
        wl = {s.upper() for s in whitelist if s}
        bl = {s.upper() for s in blacklist if s}
        if wl and sym not in wl:
            return RiskCheckResult(False, f"{sym} not in whitelist", code="BLOCKED_FILTER")
        if sym in bl:
            return RiskCheckResult(False, f"{sym} is blacklisted", code="BLOCKED_FILTER")
        return RiskCheckResult(True)

    @staticmethod
    def check_slippage(
        ref_price: float,
        lead_avg_price: float,
        max_slippage_pct: float,
    ) -> RiskCheckResult:
        if lead_avg_price <= 0:
            return RiskCheckResult(True, value=0.0)
        slip_pct = abs(ref_price - lead_avg_price) / lead_avg_price * 100.0
        if slip_pct > max_slippage_pct:
            return RiskCheckResult(
                False,
                f"Slippage {slip_pct:.2f}% exceeds limit {max_slippage_pct:.2f}%",
                value=round(slip_pct, 4),
                code="BLOCKED_SLIPPAGE",
            )
        return RiskCheckResult(True, value=round(slip_pct, 4))

    @staticmethod
    def check_spread(
        best_bid: float,
        best_ask: float,
        max_spread_pct: float,
    ) -> RiskCheckResult:
        if max_spread_pct <= 0 or best_bid <= 0 or best_ask <= 0:
            return RiskCheckResult(True, value=0.0)
        mid = (best_bid + best_ask) / 2.0
        if mid <= 0:
            return RiskCheckResult(True, value=0.0)
        spread_pct = max(0.0, (best_ask - best_bid) / mid * 100.0)
        if spread_pct > max_spread_pct:
            return RiskCheckResult(
                False,
                f"Orderbook spread {spread_pct:.3f}% exceeds guard {max_spread_pct:.3f}%",
                value=round(spread_pct, 4),
                code="BLOCKED_SPREAD",
            )
        return RiskCheckResult(True, value=round(spread_pct, 4))

    @staticmethod
    def check_drawdown(
        hwm_equity: float,
        current_equity: float,
        max_drawdown_pct: float,
    ) -> RiskCheckResult:
        if hwm_equity <= 0:
            return RiskCheckResult(True, value=0.0)
        dd_pct = max(0.0, (hwm_equity - current_equity) / hwm_equity * 100.0)
        if dd_pct > max_drawdown_pct:
            return RiskCheckResult(
                False,
                f"Drawdown {dd_pct:.2f}% exceeds breaker {max_drawdown_pct:.2f}%",
                value=round(dd_pct, 4),
                code="BLOCKED_DRAWDOWN",
            )
        return RiskCheckResult(True, value=round(dd_pct, 4))

    @staticmethod
    def check_daily_loss(
        day_start_equity: float,
        current_equity: float,
        daily_max_loss_pct: float,
    ) -> RiskCheckResult:
        if day_start_equity <= 0 or daily_max_loss_pct <= 0:
            return RiskCheckResult(True, value=0.0)
        loss_pct = max(0.0, (day_start_equity - current_equity) / day_start_equity * 100.0)
        if loss_pct > daily_max_loss_pct:
            return RiskCheckResult(
                False,
                f"Daily loss {loss_pct:.2f}% exceeds breaker {daily_max_loss_pct:.2f}%",
                value=round(loss_pct, 4),
                code="BLOCKED_DAILY_LOSS",
            )
        return RiskCheckResult(True, value=round(loss_pct, 4))

    @staticmethod
    def check_missed_order_catchup(
        order_time_ms: int,
        now_ms: int,
        max_age_sec: int,
        ref_price: float,
        lead_avg_price: float,
        catchup_max_price_dev_pct: float,
    ) -> RiskCheckResult:
        if order_time_ms <= 0 or max_age_sec <= 0:
            return RiskCheckResult(True)
        age_sec = max(0.0, (now_ms - order_time_ms) / 1000.0)
        if age_sec > max_age_sec:
            return RiskCheckResult(
                False,
                f"Missed order stale: age {age_sec:.0f}s > max {max_age_sec}s",
                value=round(age_sec, 1),
                code="SKIPPED_STALE_MISSED_ORDER",
            )
        # If delayed (>= 20s), apply tighter catch-up price deviation guard
        if age_sec >= 20.0 and lead_avg_price > 0 and catchup_max_price_dev_pct > 0:
            dev_pct = abs(ref_price - lead_avg_price) / lead_avg_price * 100.0
            if dev_pct > catchup_max_price_dev_pct:
                return RiskCheckResult(
                    False,
                    f"Catch-up price drift {dev_pct:.2f}% > max {catchup_max_price_dev_pct:.2f}% (age {age_sec:.0f}s)",
                    value=round(dev_pct, 4),
                    code="BLOCKED_CATCHUP_DRIFT",
                )
        return RiskCheckResult(True, value=round(age_sec, 1))

    @staticmethod
    def cap_by_leverage(
        requested_qty: Decimal,
        ref_price: float,
        spec: InstrumentSpec,
        follower_equity: float,
        positions: List[ExchangePosition],
        inst_id: str,
        max_single_leverage: float,
        max_total_leverage: float,
    ) -> tuple[Decimal, Optional[str]]:
        if follower_equity <= 0 or ref_price <= 0:
            return Decimal("0"), "Follower equity or reference price <= 0"

        one_unit_usd = ref_price * float(spec.ct_val)
        if one_unit_usd <= 0:
            return Decimal("0"), "Invalid contract value"

        cur_total_notional = sum(p.notional_usd for p in positions)
        cur_sym_notional = sum(p.notional_usd for p in positions if p.inst_id == inst_id)
        qty = requested_qty
        reason = None

        req_notional = float(qty) * one_unit_usd
        if (cur_sym_notional + req_notional) / follower_equity > max_single_leverage:
            avail = max(0.0, max_single_leverage * follower_equity - cur_sym_notional)
            capped = quantize_down(avail / one_unit_usd, spec.lot_sz)
            if capped < spec.min_sz:
                return Decimal("0"), f"Single symbol leverage cap ({max_single_leverage}x) reached for {inst_id}"
            qty = capped
            req_notional = float(qty) * one_unit_usd
            reason = f"Capped by single symbol leverage ({max_single_leverage}x)"

        if (cur_total_notional + req_notional) / follower_equity > max_total_leverage:
            avail = max(0.0, max_total_leverage * follower_equity - cur_total_notional)
            capped = quantize_down(avail / one_unit_usd, spec.lot_sz)
            if capped < spec.min_sz:
                return Decimal("0"), f"Total portfolio leverage cap ({max_total_leverage}x) reached"
            qty = capped
            reason = f"Capped by total leverage ({max_total_leverage}x)"

        return qty, reason

    @staticmethod
    def adjust_for_available_margin(
        requested_qty: Decimal,
        ref_price: float,
        spec: InstrumentSpec,
        avail_equity: float,
        leverage: int,
        policy: str = "auto_scale_down",
        max_usage: float = 0.92,
    ) -> Tuple[Decimal, Optional[str], Optional[str]]:
        """Check available margin; either auto-scale down or block with alert."""
        if requested_qty <= 0 or ref_price <= 0:
            return Decimal("0"), "Invalid qty or price", None
        one_unit_usd = ref_price * float(spec.ct_val)
        if one_unit_usd <= 0:
            return Decimal("0"), "Invalid contract value", None
        lvr = max(1, int(leverage))
        usable_margin = max(0.0, avail_equity * max_usage)
        req_notional = float(requested_qty) * one_unit_usd
        req_margin = req_notional / lvr
        if req_margin <= usable_margin:
            return requested_qty, None, None

        if policy == "skip_alert":
            return (
                Decimal("0"),
                f"Insufficient margin: need ${req_margin:.2f} > usable ${usable_margin:.2f}",
                None,
            )

        max_notional = usable_margin * lvr
        scaled = quantize_down(max_notional / one_unit_usd, spec.lot_sz)
        if scaled < spec.min_sz or (float(scaled) * one_unit_usd) < float(spec.min_notional):
            return (
                Decimal("0"),
                f"Insufficient margin after auto-scale: usable ${usable_margin:.2f} below min order",
                None,
            )
        return (
            scaled,
            None,
            f"Auto-scaled from {requested_qty} to {scaled} for avail margin ${avail_equity:.2f}",
        )

    @staticmethod
    def compute_tp_sl_prices(
        pos_side: str,
        entry_px: float,
        tp_sl_mode: str,
        enable_tp: bool,
        tp_pct: float,
        enable_sl: bool,
        sl_pct: float,
        hard_sl_pct: float,
    ) -> Tuple[Optional[float], Optional[float]]:
        """Calculate TP and SL trigger prices according to tp_sl_mode."""
        if entry_px <= 0 or tp_sl_mode == "follow_lead":
            return None, None
        is_long = pos_side.lower() == "long"

        tp_px: Optional[float] = None
        sl_px: Optional[float] = None

        if tp_sl_mode == "hybrid_guard":
            # Hybrid guard: follow lead normally, attach hard disaster SL (and optional TP)
            if hard_sl_pct > 0:
                sl_px = entry_px * (1.0 - hard_sl_pct / 100.0) if is_long else entry_px * (1.0 + hard_sl_pct / 100.0)
            if enable_tp and tp_pct > 0:
                tp_px = entry_px * (1.0 + tp_pct / 100.0) if is_long else entry_px * (1.0 - tp_pct / 100.0)
            return tp_px, sl_px

        if tp_sl_mode == "independent":
            if enable_tp and tp_pct > 0:
                tp_px = entry_px * (1.0 + tp_pct / 100.0) if is_long else entry_px * (1.0 - tp_pct / 100.0)
            if enable_sl and sl_pct > 0:
                sl_px = entry_px * (1.0 - sl_pct / 100.0) if is_long else entry_px * (1.0 + sl_pct / 100.0)
            return tp_px, sl_px

        return None, None
