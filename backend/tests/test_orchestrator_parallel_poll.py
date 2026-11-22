"""Parallel lead polling: tick dispatches without waiting, one session per lead.

2026-11-22 user instruction: push Jinmu's effective poll interval under 5s;
sequential polling (each lead ~3s, one after another) capped the effective
interval at ~10s even with poll_interval_sec=5. tick() now dispatches each
due lead as a background task with its own DB session, guarded against
overlapping syncs of the same lead, and the floor is 1s (was 5s).
"""
import asyncio
import importlib
import time as real_time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.database import get_session_factory
from app.models.domain import LeadSource
from app.services.engine.orchestrator import EngineOrchestrator


orchestrator_module = importlib.import_module("app.services.engine.orchestrator")


@pytest.fixture
def engine(monkeypatch):
    eng = EngineOrchestrator()
    clock = SimpleNamespace(now=10000.0, mono=10000.0)
    clock_ns = SimpleNamespace(time=lambda: clock.now, monotonic=lambda: clock.mono)
    monkeypatch.setattr(orchestrator_module, "time", clock_ns)
    eng._last_retention_cleanup = clock.mono
    return SimpleNamespace(engine=eng, clock=clock)


async def make_lead(poll_interval_sec=2):
    async with get_session_factory()() as db:
        lead = LeadSource(name="parallel test", portfolio_id="test-portfolio",
                          status="running", is_enabled=True,
                          poll_interval_sec=poll_interval_sec)
        db.add(lead)
        await db.commit()
        return lead.id


async def cancel_tasks(eng):
    for task in list(eng._lead_sync_tasks):
        task.cancel()
    if eng._lead_sync_tasks:
        await asyncio.gather(*eng._lead_sync_tasks, return_exceptions=True)
    eng._lead_sync_in_flight.clear()


async def test_tick_dispatches_due_leads_without_waiting(engine, monkeypatch):
    # 同步耗时 5 秒时 tick 仍应立即返回（后台派发，不等待），而不是被拖到 5 秒。
    eng = engine.engine
    lead_id = await make_lead(poll_interval_sec=2)
    calls = []
    async def slow_sync(lead_id_arg):
        calls.append(lead_id_arg)
        await asyncio.sleep(5)
    monkeypatch.setattr(eng, "_sync_lead_isolated", slow_sync)
    started = real_time.monotonic()
    await eng.tick()
    elapsed = real_time.monotonic() - started
    assert elapsed < 1.0
    assert calls == [lead_id]
    assert lead_id in eng._lead_sync_in_flight
    await cancel_tasks(eng)


async def test_tick_does_not_redispatch_in_flight_lead(engine, monkeypatch):
    # 间隔已过、但上一轮同步还没跑完时，不重复派发同一 lead。
    eng = engine.engine
    lead_id = await make_lead(poll_interval_sec=2)
    calls = []
    async def slow_sync(lead_id_arg):
        calls.append(lead_id_arg)
        await asyncio.sleep(5)
    monkeypatch.setattr(eng, "_sync_lead_isolated", slow_sync)
    await eng.tick()
    assert calls == [lead_id]
    engine.clock.now += 5.0
    engine.clock.mono += 5.0
    await eng.tick()
    assert calls == [lead_id]
    await cancel_tasks(eng)


async def test_poll_interval_floor_is_one_second(engine, monkeypatch):
    # poll_interval_sec=0 也按 1 秒下限算：0.5 秒未到不派发，1.5 秒到才派发。
    eng = engine.engine
    lead_id = await make_lead(poll_interval_sec=0)
    calls = []
    async def record_sync(lead_id_arg):
        calls.append(lead_id_arg)
    monkeypatch.setattr(eng, "_sync_lead_isolated", record_sync)
    eng._last_lead_poll[lead_id] = engine.clock.now - 0.5
    await eng.tick()
    assert calls == []
    eng._last_lead_poll[lead_id] = engine.clock.now - 1.5
    await eng.tick()
    assert calls == [lead_id]
    await cancel_tasks(eng)


async def test_isolated_sync_refetches_lead_in_own_session(engine, monkeypatch):
    # 隔离同步在自己的会话里重取 lead，不串用 tick 的会话；跑完清除在途标记。
    eng = engine.engine
    lead_id = await make_lead(poll_interval_sec=2)
    seen = []
    async def fake_sync(db, lead, proxies=None):
        seen.append((lead.id, lead.name, db is not None))
    monkeypatch.setattr(eng, "sync_lead", fake_sync)
    eng._lead_sync_in_flight.add(lead_id)
    await eng._sync_lead_isolated(lead_id)
    assert seen == [(lead_id, "parallel test", True)]
    assert lead_id not in eng._lead_sync_in_flight


async def test_isolated_sync_clears_in_flight_on_failure(engine, monkeypatch):
    # 同步抛错也不把 lead 永远锁在在途集合里（否则该 lead 再也不会被轮询）。
    eng = engine.engine
    lead_id = await make_lead(poll_interval_sec=2)
    async def failing_sync(db, lead, proxies=None):
        raise RuntimeError("synthetic sync failure")
    monkeypatch.setattr(eng, "sync_lead", failing_sync)
    eng._lead_sync_in_flight.add(lead_id)
    await eng._sync_lead_isolated(lead_id)
    assert lead_id not in eng._lead_sync_in_flight
