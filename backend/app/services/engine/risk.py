from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional, Sequence

from app.services.adapters.base import ExchangePosition, InstrumentSpec
from app.services.engine.sizer import quantize_down


@dataclass
class RiskCheckResult:
    allowed: bool
    reason: Optional[str] = None
    value: float = 0.0


class RiskEngine:
    """Risk guards: symbol filters, slippage limits, HWM drawdown breaker, leverage caps."""

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
            return RiskCheckResult(False, f"{sym} not in whitelist")
        if sym in bl:
            return RiskCheckResult(False, f"{sym} is blacklisted")
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
            )
        return RiskCheckResult(True, value=round(slip_pct, 4))

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
            )
        return RiskCheckResult(True, value=round(dd_pct, 4))

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
