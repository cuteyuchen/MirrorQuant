from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Dict, Optional

from app.services.adapters.base import BaseExchangeAdapter
from app.services.adapters.binance import BinanceAdapter
from app.services.adapters.bitget import BitgetAdapter
from app.services.adapters.bybit import BybitAdapter
from app.services.adapters.okx import OKXAdapter
from app.services.proxy.manager import ProxyManager


logger = logging.getLogger("mirrorquant.adapters")
_adapters: dict[tuple, tuple[tuple, BaseExchangeAdapter]] = {}
_closing: set[asyncio.Task] = set()


def _closed_adapter(task: asyncio.Task) -> None:
    # 取回后台清理异常，避免失效任务只被丢弃而无人报告。
    _closing.discard(task)
    if not task.cancelled() and (error := task.exception()) is not None:
        logger.error("Adapter cleanup failed (%s)", type(error).__name__)


async def invalidate_account_adapter(account_id: int) -> None:
    # 更新或删除账户后立即释放旧凭据对应的连接。
    for key in list(_adapters):
        if key[1] == account_id:
            _, adapter = _adapters.pop(key)
            await adapter.aclose()


async def invalidate_proxy_adapters(proxy_url: str) -> None:
    # 代理编辑不得继续复用旧出口。
    for key, (_, adapter) in list(_adapters.items()):
        if adapter.proxy_url == proxy_url:
            _adapters.pop(key)
            await adapter.aclose()


async def close_exchange_adapters() -> None:
    # 包含直接构造的适配器客户端，方便关闭和离线测试隔离。
    if _closing:
        await asyncio.gather(*list(_closing), return_exceptions=True)
    clients = list(BaseExchangeAdapter._live_clients)
    BaseExchangeAdapter._live_clients.clear()
    BaseExchangeAdapter._contract_cache.clear()
    BaseExchangeAdapter._rate_limit_until.clear()
    _adapters.clear()
    await ProxyManager.close_clients(clients)


def create_exchange_adapter(
    exchange: str,
    credentials: Dict[str, str],
    environment: str = "demo",
    proxy_url: Optional[str] = None,
    margin_mode: str = "cross",
    position_mode: str = "hedge",
    account_id: Optional[int] = None,
) -> BaseExchangeAdapter:
    ex = exchange.lower().strip()
    adapter_type = {"okx": OKXAdapter, "binance": BinanceAdapter, "bybit": BybitAdapter, "bitget": BitgetAdapter}.get(ex)
    if adapter_type is None:
        raise ValueError(f"Unsupported exchange: {exchange}")
    fingerprint = hashlib.sha256(json.dumps(credentials, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    identity = (ex, fingerprint, environment, proxy_url, margin_mode, position_mode)
    # 事件循环也隔离，避免测试或多循环环境复用已关闭的传输。
    key = (asyncio.get_running_loop(), account_id) if account_id is not None else None
    if key in _adapters:
        previous, adapter = _adapters[key]
        if previous == identity:
            return adapter
        _adapters.pop(key)
        task = asyncio.create_task(adapter.aclose())
        _closing.add(task)
        task.add_done_callback(_closed_adapter)
    adapter = adapter_type(credentials=credentials, environment=environment, proxy_url=proxy_url,
                           margin_mode=margin_mode, position_mode=position_mode)
    if key is not None:
        _adapters[key] = (identity, adapter)
    return adapter
