from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from app.services.adapters.base import InstrumentSpec


def quantize_down(val: Any, step: Any) -> Decimal:
    d_val = Decimal(str(val))
    d_step = Decimal(str(step))
    if d_step <= 0:
        return d_val
    return (d_val // d_step) * d_step


def quantize_step(val: Any, step: Any) -> Decimal:
    d_val = Decimal(str(val))
    d_step = Decimal(str(step))
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
        follower_equity: float,
        lead_margin_balance: float,
        risk_scale: float,
        lead_notional: float,
        pending_debt_usd: float = 0.0,
    ) -> float:
        if follower_equity <= 0 or lead_margin_balance <= 0 or lead_notional <= 0:
            return max(0.0, pending_debt_usd)
        ratio = (follower_equity / lead_margin_balance) * risk_scale
        return max(0.0, lead_notional * ratio + pending_debt_usd)

    @staticmethod
    def size_open_order(
        target_usd: float,
        ref_price: float,
        spec: InstrumentSpec,
    ) -> SizeResult:
        if ref_price <= 0 or target_usd <= 0:
            return SizeResult(
                contracts=Decimal("0"),
                below_min=True,
                target_usd=target_usd,
                one_unit_usd=0.0,
                min_usd=0.0,
                estimated_notional=0.0,
            )

        one_unit_usd = ref_price * float(spec.ct_val)
        raw_units = target_usd / one_unit_usd if one_unit_usd > 0 else 0.0
        qty = quantize_down(raw_units, spec.lot_sz)
        est_notional = float(qty) * one_unit_usd
        min_usd = max(float(spec.min_sz) * one_unit_usd, float(spec.min_notional))
        below_min = (qty < spec.min_sz) or (est_notional < float(spec.min_notional))

        return SizeResult(
            contracts=qty,
            below_min=below_min,
            target_usd=target_usd,
            one_unit_usd=one_unit_usd,
            min_usd=min_usd,
            estimated_notional=est_notional,
        )

    @staticmethod
    def size_close_order(
        local_pos_sz: float,
        is_full_close: bool,
        lead_close_qty: float,
        lead_remaining_qty: float,
        spec: InstrumentSpec,
    ) -> Decimal:
        local_dec = Decimal(str(local_pos_sz))
        if is_full_close or local_dec <= spec.min_sz:
            return local_dec
        prior_lead_qty = lead_remaining_qty + lead_close_qty
        reduce_frac = min(1.0, lead_close_qty / prior_lead_qty) if prior_lead_qty > 0 else 1.0
        raw_close = local_dec * Decimal(str(reduce_frac))
        close_sz = quantize_down(raw_close, spec.lot_sz)
        if close_sz < spec.min_sz:
            close_sz = spec.min_sz if local_dec >= spec.min_sz else local_dec
        return min(close_sz, local_dec)
