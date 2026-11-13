from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import math
from typing import List, Optional, Sequence, Tuple

from app.services.adapters.base import ExchangePosition, InstrumentSpec
from app.services.engine.sizer import exact, quantize_down


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
        if not all(math.isfinite(v) for v in (ref_price, lead_avg_price, max_slippage_pct)) or ref_price <= 0 or lead_avg_price <= 0 or max_slippage_pct < 0:
            return RiskCheckResult(False, "Invalid slippage price or limit", code="BLOCKED_SLIPPAGE")
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
        if not all(math.isfinite(v) for v in (best_bid, best_ask, max_spread_pct)) or not 0 < best_bid <= best_ask:
            return RiskCheckResult(False, "Executable orderbook unavailable or crossed", code="BLOCKED_SPREAD")
        if max_spread_pct <= 0:
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
        if not all(math.isfinite(v) for v in (hwm_equity, current_equity, max_drawdown_pct)):
            return RiskCheckResult(False, "Invalid account equity or drawdown limit", code="BLOCKED_DRAWDOWN")
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
        if not all(math.isfinite(v) for v in (day_start_equity, current_equity, daily_max_loss_pct)):
            return RiskCheckResult(False, "Invalid account equity or daily loss limit", code="BLOCKED_DAILY_LOSS")
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
        """Catch-up gate: price drift decides, age is only a wide sanity backstop.

        Rationale: Binance public copy-trade BAPI exposes lead fills with a 2-3 minute
        aggregation lag. Rejecting on age alone silently drops every position the lead
        opens. The real economic risk of a late entry is paying a worse price than the
        lead, so the drift gate is authoritative; max_age_sec is only a hard ceiling
        against replaying ancient history.
        """
        if order_time_ms <= 0 or order_time_ms > now_ms:
            return RiskCheckResult(False, "Missing or future source order timestamp", code="SKIPPED_STALE_MISSED_ORDER")
        age_sec = max(0.0, (now_ms - order_time_ms) / 1000.0)

        # Authoritative gate: price must still be inside the lead's entry cost window.
        if lead_avg_price > 0 and catchup_max_price_dev_pct > 0:
            dev_pct = abs(ref_price - lead_avg_price) / lead_avg_price * 100.0
            if dev_pct > catchup_max_price_dev_pct:
                return RiskCheckResult(
                    False,
                    f"Catch-up price drift {dev_pct:.2f}% > max {catchup_max_price_dev_pct:.2f}% (age {age_sec:.0f}s)",
                    value=round(dev_pct, 4),
                    code="BLOCKED_CATCHUP_DRIFT",
                )

        # Wide backstop: never replay genuinely ancient history even if price matches.
        if max_age_sec > 0 and age_sec > max_age_sec:
            return RiskCheckResult(
                False,
                f"Missed order stale: age {age_sec:.0f}s > max {max_age_sec}s",
                value=round(age_sec, 1),
                code="SKIPPED_STALE_MISSED_ORDER",
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
        equity, price, single_limit, total_limit = map(
            exact, (follower_equity, ref_price, max_single_leverage, max_total_leverage)
        )
        if equity <= 0 or price <= 0:
            return Decimal("0"), "Follower equity or reference price <= 0"

        one_unit_usd = price * exact(spec.ct_val)
        if one_unit_usd <= 0:
            return Decimal("0"), "Invalid contract value"

        cur_total_notional = sum((abs(exact(p.notional_usd)) for p in positions), Decimal("0"))
        cur_sym_notional = sum((abs(exact(p.notional_usd)) for p in positions if p.inst_id == inst_id), Decimal("0"))
        qty = quantize_down(requested_qty, spec.lot_sz)
        reason = None

        req_notional = qty * one_unit_usd
        if (cur_sym_notional + req_notional) / equity > single_limit:
            avail = max(Decimal("0"), single_limit * equity - cur_sym_notional)
            capped = quantize_down(avail / one_unit_usd, spec.lot_sz)
            if capped < spec.min_sz:
                return Decimal("0"), f"Single symbol leverage cap ({max_single_leverage}x) reached for {inst_id}"
            qty = capped
            req_notional = qty * one_unit_usd
            reason = f"Capped by single symbol leverage ({max_single_leverage}x)"

        if (cur_total_notional + req_notional) / equity > total_limit:
            avail = max(Decimal("0"), total_limit * equity - cur_total_notional)
            capped = quantize_down(avail / one_unit_usd, spec.lot_sz)
            if capped < spec.min_sz:
                return Decimal("0"), f"Total portfolio leverage cap ({max_total_leverage}x) reached"
            qty = capped
            reason = f"Capped by total leverage ({max_total_leverage}x)"

        if qty < spec.min_sz or qty * one_unit_usd < spec.min_notional:
            return Decimal("0"), "Exposure cap leaves quantity below minimum order"
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
        requested_qty = quantize_down(requested_qty, spec.lot_sz)
        price, available, usage = map(exact, (ref_price, avail_equity, max_usage))
        if requested_qty <= 0 or price <= 0:
            return Decimal("0"), "Invalid qty or price", None
        one_unit_usd = price * exact(spec.ct_val)
        if one_unit_usd <= 0:
            return Decimal("0"), "Invalid contract value", None
        lvr = max(1, int(leverage))
        if usage <= 0 or usage > 1:
            return Decimal("0"), "Invalid margin usage fraction", None
        usable_margin = max(Decimal("0"), available * usage)
        req_notional = requested_qty * one_unit_usd
        if requested_qty < spec.min_sz or req_notional < spec.min_notional:
            return Decimal("0"), "Quantity below minimum order", None
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
        if scaled < spec.min_sz or (scaled * one_unit_usd) < spec.min_notional:
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
