"""Read compatibility for position snapshots saved before current prices existed."""
from __future__ import annotations

import math
from typing import Any


def _number(value: Any) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def with_current_price(position: dict, exchange: str = "") -> dict:
    result = dict(position)
    current = next((n for key in ("curPx", "currentPrice", "markPx", "markPrice")
                    if (n := _number(position.get(key))) > 0), 0.0)
    source = "exchange" if current else "unavailable"
    if not current:
        size = abs(_number(position.get("pos")))
        notional = abs(_number(position.get("notionalUsd")))
        # OKX pos is contracts, not base currency. Never divide USD by contracts
        # unless the saved snapshot also supplies the contract's base-unit value.
        contract_value = _number(position.get("ctVal")) if exchange == "okx" else 1.0
        if size and notional and contract_value > 0:
            current = _number(notional / size / contract_value)
            source = "estimated" if current else "unavailable"
        if not current:
            current = max(0.0, _number(position.get("avgPx")))
            source = "entry" if current else "unavailable"
    result.update(curPx=current, markPx=current, currentPrice=current, markPrice=current,
                  priceSource=position.get("priceSource") or source)
    return result
