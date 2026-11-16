"""维护和生命周期故障只使用内存数据库与离线替身。"""
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.config import get_settings
from app.services.engine.orchestrator import EngineOrchestrator


main_module = importlib.import_module("app.main")
orchestrator_module = importlib.import_module("app.services.engine.orchestrator")


@pytest.mark.parametrize("stage", [
    None, "engine_start", "outbox_start", "engine_stop", "outbox_stop", "adapters_close", "body",
])
async def test_lifespan_releases_both_pools_even_when_start_or_stop_fails(monkeypatch, stage):
    # 不启动后台轮询或外部客户端，逐个注入故障验证 finally 的释放顺序。
    def operation(name):
        return AsyncMock(side_effect=RuntimeError(name) if stage == name else None)

    engine = SimpleNamespace(start=operation("engine_start"), stop=operation("engine_stop"))
    outbox = SimpleNamespace(start=operation("outbox_start"), stop=operation("outbox_stop"))
    close_adapters = operation("adapters_close")
    close_bapi = operation("bapi_close")
    monkeypatch.setattr(main_module.settings, "enable_background_engine", True)
    monkeypatch.setattr(main_module, "orchestrator", engine)
    monkeypatch.setattr(main_module, "notification_outbox", outbox)
    monkeypatch.setattr(main_module, "close_exchange_adapters", close_adapters)
    monkeypatch.setattr(main_module.BinanceBapiCollector, "close_clients", close_bapi)

    async def use_lifespan():
        async with main_module.lifespan(main_module.app):
            if stage == "body":
                raise RuntimeError("body")

    if stage is None:
        await use_lifespan()
    else:
        with pytest.raises(RuntimeError, match=stage):
            await use_lifespan()
    engine.stop.assert_awaited_once()
    outbox.stop.assert_awaited_once()
    close_adapters.assert_awaited_once()
    close_bapi.assert_awaited_once()


async def test_retention_failure_does_not_abort_sync_or_retry_every_tick(monkeypatch):
    # 清理失败仍执行只读轮询，下一次维护严格等待配置的低频间隔。
    engine = EngineOrchestrator()
    clock = SimpleNamespace(time=lambda: 10000.0, monotonic=lambda: 10000.0)
    cleanup = AsyncMock(side_effect=RuntimeError("synthetic maintenance failure"))
    monkeypatch.setattr(orchestrator_module, "time", clock)
    monkeypatch.setattr(orchestrator_module, "cleanup_retained_records", cleanup)
    monkeypatch.setattr(get_settings(), "retention_cleanup_interval_sec", 3600)
    await engine.tick()
    await engine.tick()
    cleanup.assert_awaited_once()
    assert engine._last_retention_cleanup == 10000.0
    clock.monotonic = lambda: 13601.0
    await engine.tick()
    assert cleanup.await_count == 2
