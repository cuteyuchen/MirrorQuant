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
    # 盈亏百分比按实际占用保证金（ROE）计算，兼容历史仅按名义价值存储的仓位快照。
    margin_usd = _number(
        position.get("marginUsd")
        or position.get("initialMargin")
        or position.get("positionInitialMargin")
        or position.get("margin")
        or position.get("imr")
        or position.get("positionIM")
        or position.get("marginSize")
    )
    lever = _number(position.get("lever") or position.get("leverage"))
    notional = abs(_number(position.get("notionalUsd")))
    if notional <= 0 and exchange != "okx":
        notional = abs(_number(position.get("pos"))) * current
    if margin_usd <= 0 and notional > 0 and lever > 0:
        margin_usd = notional / lever
    if margin_usd > 0:
        result["marginUsd"] = margin_usd
        if position.get("upl") is not None and (
            exchange in ("binance", "bybit", "bitget")
            or position.get("uplRatio") is None
            or "marginUsd" in position
        ):
            result["uplRatio"] = _number(position.get("upl")) / margin_usd
    return result
