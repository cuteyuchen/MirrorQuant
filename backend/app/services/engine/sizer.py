from __future__ import annotations

from dataclasses import dataclass
import math
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any

from app.services.adapters.base import InstrumentSpec


def exact(val: Any) -> Decimal:
    res = val if isinstance(val, Decimal) else Decimal(str(val))
    if not res.is_finite():
        raise ValueError("Non-finite financial input")
    return res


def quantize_down(val: Any, step: Any) -> Decimal:
    d_val = exact(val)
    d_step = exact(step)
    if d_val < 0 or d_step <= 0:
        return Decimal("0")
    return (d_val / d_step).to_integral_value(rounding=ROUND_DOWN) * d_step


def quantize_step(val: Any, step: Any) -> Decimal:
    d_val = exact(val)
    d_step = exact(step)
    if d_step <= 0:
        return d_val
    return (d_val / d_step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * d_step


def format_decimal(d: Decimal, step: Decimal) -> str:
    norm_step = step.normalize()
    exp = norm_step.as_tuple().exponent
    decimals = max(0, -exp) if isinstance(exp, int) and exp < 0 else 0
    return f"{d:.{decimals}f}"


@dataclass
class SizeResult:
    contracts: Decimal
    below_min: bool
    target_usd: float
    one_unit_usd: float
    min_usd: float
    estimated_notional: float


class PositionSizer:
    """Proportional copy-trade position sizing with debt accumulation and Decimal quantization."""

    @staticmethod
    def calculate_target_usd(
        follower_equity: Any,
        lead_margin_balance: Any,
        risk_scale: Any,
        lead_notional: Any,
        pending_debt_usd: Any = 0.0,
    ) -> Decimal:
        equity, balance, scale, notional, debt = map(
            exact, (follower_equity, lead_margin_balance, risk_scale, lead_notional, pending_debt_usd)
        )
        if equity <= 0 or balance <= 0 or notional <= 0 or scale <= 0:
            return Decimal("0")
        lead_order_ratio = notional / balance
        return max(Decimal("0"), equity * lead_order_ratio * scale + debt)

    @staticmethod
    def compute_dynamic_leverage(
        target_notional: Any,
        equity: Any,
        max_symbol_leverage: int = 100,
        target_im_fraction: float = 0.08,
    ) -> int:
        """Choose cross-margin initial-margin leverage without changing exposure size."""
        notional, eq, fraction = map(exact, (target_notional, equity, target_im_fraction))
        maximum = int(exact(max_symbol_leverage))
        if maximum < 1 or fraction <= 0 or fraction > 1:
            raise ValueError("Invalid leverage ceiling or initial margin fraction")
        if notional <= 0 or eq <= 0:
            return min(10, maximum)
        desired_lvr = math.ceil(notional / (eq * fraction))
        return min(maximum, max(2, desired_lvr))

    @staticmethod
    def size_open_order(
        target_usd: Any,
        ref_price: Any,
        spec: InstrumentSpec,
    ) -> SizeResult:
        target, price, ct_val = map(exact, (target_usd, ref_price, spec.ct_val))
        if price <= 0 or target <= 0 or ct_val <= 0:
            return SizeResult(
                contracts=Decimal("0"),
                below_min=True,
                target_usd=float(target),
                one_unit_usd=0.0,
                min_usd=0.0,
                estimated_notional=0.0,
            )

        one_unit_usd = price * ct_val
        raw_units = target / one_unit_usd
        qty = quantize_down(raw_units, spec.lot_sz)
        est_notional = qty * one_unit_usd
        min_usd = max(exact(spec.min_sz) * one_unit_usd, exact(spec.min_notional))
        below_min = qty <= 0 or (qty < spec.min_sz) or (est_notional < spec.min_notional)

        return SizeResult(
            contracts=qty,
            below_min=below_min,
            target_usd=float(target),
            one_unit_usd=float(one_unit_usd),
            min_usd=float(min_usd),
            estimated_notional=float(est_notional),
        )

    @staticmethod
    def size_close_order(
        local_pos_sz: Any,
        is_full_close: bool,
        lead_close_qty: Any,
        lead_remaining_qty: Any,
        spec: InstrumentSpec,
    ) -> Decimal:
        local, closing, remaining = map(exact, (local_pos_sz, lead_close_qty, lead_remaining_qty))
        if local <= 0 or closing <= 0:
            return Decimal("0")
        if remaining < 0:
            raise ValueError("Invalid lead inventory")
        if is_full_close:
            return quantize_down(local, spec.lot_sz)
        prior = closing + remaining
        if prior <= 0:
            return quantize_down(local, spec.lot_sz)
        fraction = min(Decimal("1"), closing / prior)
        qty = quantize_down(local * fraction, spec.lot_sz)
        return qty if qty >= spec.min_sz else Decimal("0")
