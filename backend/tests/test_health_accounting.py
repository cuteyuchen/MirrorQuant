"""Liveness/readiness and cash-flow accounting regression coverage."""
import importlib
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import database
from app.models.domain import ExecutionRecord
from app.services.engine.accounting import cash_flow_adjusted_realized_pnl
from app.services.engine.orchestrator import EngineOrchestrator

main_module = importlib.import_module("app.main")
orchestrator_module = importlib.import_module("app.services.engine.orchestrator")


def set_engine(monkeypatch, *, age=1.0, running=True, enabled=True):
    monkeypatch.setattr(main_module.settings, "enable_background_engine", enabled)
    monkeypatch.setattr(main_module.settings, "readiness_heartbeat_max_age_sec", 30.0)
    monkeypatch.setattr(main_module, "orchestrator", SimpleNamespace(
        heartbeat_age_sec=lambda: age, is_running=running,
    ))


@pytest.mark.asyncio
async def test_liveness_does_not_probe_database_or_engine(async_client, monkeypatch):
    def unavailable():
        raise AssertionError("Liveness must not access dependencies")

    monkeypatch.setattr(main_module, "get_session_factory", unavailable)
    monkeypatch.setattr(main_module, "orchestrator", object())
    response = await async_client.get("/api/health")
    assert response.status_code == 200 and response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_ready_requires_reachable_database_and_fresh_engine(async_client, monkeypatch):
    set_engine(monkeypatch, age=5.0)
    response = await async_client.get("/api/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {
        "database": {"ok": True},
        "engine": {"ok": True, "enabled": True, "heartbeatAgeSec": 5.0, "maxAgeSec": 30.0},
    }}


@pytest.mark.asyncio
@pytest.mark.parametrize("age,running,enabled", [
    (None, True, True), (31.0, True, True), (1.0, False, True), (1.0, True, False),
])
async def test_absent_stale_stopped_or_disabled_engine_is_degraded(async_client, monkeypatch, age, running, enabled):
    set_engine(monkeypatch, age=age, running=running, enabled=enabled)
    response = await async_client.get("/api/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["checks"]["engine"]["ok"] is False


@pytest.mark.asyncio
async def test_database_failure_is_degraded_without_connection_details(async_client, monkeypatch):
    set_engine(monkeypatch)

    def unavailable():
        raise RuntimeError("private-database-credential")

    monkeypatch.setattr(main_module, "get_session_factory", unavailable)
    response = await async_client.get("/api/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["database"] == {"ok": False, "error": "Database unavailable"}
    assert "private-database-credential" not in response.text


@pytest.mark.asyncio
async def test_database_probe_timeout_is_bounded(async_client, monkeypatch):
    import asyncio

    set_engine(monkeypatch)
    monkeypatch.setattr(main_module.settings, "readiness_db_timeout_sec", 0.01)

    async def execute(*args):
        await asyncio.Event().wait()

    session = SimpleNamespace(execute=execute)

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(main_module, "get_session_factory", lambda: SessionContext)
    response = await asyncio.wait_for(async_client.get("/api/ready"), timeout=1.0)
    assert response.status_code == 503
    assert response.json()["checks"]["database"]["ok"] is False


@pytest.mark.asyncio
async def test_every_loop_tick_updates_monotonic_heartbeat(monkeypatch):
    monitor = EngineOrchestrator()
    clock = SimpleNamespace(monotonic=lambda: 123.0)
    monkeypatch.setattr(orchestrator_module, "time", clock)
    monkeypatch.setattr(orchestrator_module.asyncio, "sleep", AsyncMock())

    async def tick():
        assert monitor.last_heartbeat_monotonic == 123.0
        monitor._running = False

    monitor.tick = tick
    monitor._running = True
    await monitor._loop()
    clock.monotonic = lambda: 130.0
    assert monitor.heartbeat_age_sec() == 7.0
    await monitor.stop()
    assert monitor.heartbeat_age_sec() is None and monitor.is_running is False


def execution(**values):
    return ExecutionRecord(lead_id=1, account_id=1, lead_name="lead", account_name="account",
        symbol="BTCUSDT", inst_id="BTC-USDT-SWAP", side="BUY", position_side="LONG",
        action="OPEN_LONG", status="EXECUTED", **values)


@pytest.mark.asyncio
async def test_execution_fee_funding_and_fill_identifier_persist():
    async with database.get_session_factory()() as db:
        known = execution(fee=1.25, funding=-0.5, exchange_fill_id="test-fill")
        unknown = execution()
        db.add_all([known, unknown])
        await db.commit()
        await db.refresh(known)
        await db.refresh(unknown)
        assert (known.fee, known.funding, known.exchange_fill_id) == (1.25, -0.5, "test-fill")
        assert (unknown.fee, unknown.funding, unknown.exchange_fill_id) == (None, None, None)


@pytest.mark.asyncio
async def test_existing_execution_rows_survive_idempotent_accounting_upgrade(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'accounting.sqlite'}")
    monkeypatch.setattr(database, "_engine", engine)
    monkeypatch.setattr(database, "_session_factory", async_sessionmaker(engine, expire_on_commit=False))
    try:
        await database.init_db()
        async with database.get_session_factory()() as db:
            db.add(execution(details="preserved"))
            await db.commit()
        async with engine.begin() as conn:
            for column in ("fee", "funding", "exchange_fill_id"):
                await conn.execute(text(f"ALTER TABLE execution_records DROP COLUMN {column}"))
        await database.init_db()
        await database.init_db()
        async with database.get_session_factory()() as db:
            row = await db.scalar(select(ExecutionRecord))
            assert row.details == "preserved"
            assert (row.fee, row.funding, row.exchange_fill_id) == (None, None, None)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("start,end,kwargs,expected", [
    (1000, 1280, {"deposits": 200, "end_unrealized_pnl": 30}, "50"),
    (1000, 920, {"withdrawals": 100}, "20"),
    (1000, "1037.9", {"start_unrealized_pnl": 10, "end_unrealized_pnl": 20}, "27.9"),
    # Equity already incorporates a 2 fee and 1 funding payment against 30 gross PnL.
    (1000, 1027, {}, "27"),
])
def test_cash_flow_adjusted_pnl_removes_transfers_and_unrealized_growth(start, end, kwargs, expected):
    assert cash_flow_adjusted_realized_pnl(start, end, **kwargs) == Decimal(expected)


@pytest.mark.parametrize("kwargs", [{"deposits": -1}, {"withdrawals": -1}, {"end_unrealized_pnl": float("nan")}])
def test_cash_flow_adjusted_pnl_rejects_invalid_accounting_inputs(kwargs):
    with pytest.raises(ValueError):
        cash_flow_adjusted_realized_pnl(1000, 1000, **kwargs)
