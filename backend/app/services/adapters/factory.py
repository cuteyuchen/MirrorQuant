from __future__ import annotations

from typing import Dict, Optional

from app.services.adapters.base import BaseExchangeAdapter
from app.services.adapters.binance import BinanceAdapter
from app.services.adapters.bitget import BitgetAdapter
from app.services.adapters.bybit import BybitAdapter
from app.services.adapters.okx import OKXAdapter


def create_exchange_adapter(
    exchange: str,
    credentials: Dict[str, str],
    environment: str = "demo",
    proxy_url: Optional[str] = None,
    margin_mode: str = "cross",
    position_mode: str = "hedge",
) -> BaseExchangeAdapter:
    ex = exchange.lower().strip()
    if ex == "okx":
        return OKXAdapter(
            credentials=credentials,
            environment=environment,
            proxy_url=proxy_url,
            margin_mode=margin_mode,
            position_mode=position_mode,
        )
    if ex == "binance":
        return BinanceAdapter(
            credentials=credentials,
            environment=environment,
            proxy_url=proxy_url,
            margin_mode=margin_mode,
            position_mode=position_mode,
        )
    if ex == "bybit":
        return BybitAdapter(
            credentials=credentials,
            environment=environment,
            proxy_url=proxy_url,
            margin_mode=margin_mode,
            position_mode=position_mode,
        )
    if ex == "bitget":
        return BitgetAdapter(
            credentials=credentials,
            environment=environment,
            proxy_url=proxy_url,
            margin_mode=margin_mode,
            position_mode=position_mode,
        )
    raise ValueError(f"Unsupported exchange: {exchange}")
