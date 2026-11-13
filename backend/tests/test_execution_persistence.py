"""SQLite upgrade durability and executor ownership regressions."""
import asyncio
import json

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import database
from app.models.domain import User
from app.services.engine.router import CopyRouter
from app.services.engine.serialization import execution_lock


@pytest.mark.asyncio
async def test_existing_file_database_gets_session_and_ledger_upgrade(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'upgrade.sqlite'}")
    monkeypatch.setattr(database, "_engine", engine)
    monkeypatch.setattr(database, "_session_factory", async_sessionmaker(engine, expire_on_commit=False))
    try:
        await database.init_db()
        async with database.get_session_factory()() as db:
            db.add(User(username="preserved", password_hash="test-placeholder", role="admin"))
            await db.commit()
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE users DROP COLUMN session_version"))
            await conn.execute(text("DROP TABLE position_allocations"))
        await database.init_db()
        await database.init_db()
        async with database.get_session_factory()() as db:
            user = await db.scalar(select(User).where(User.username == "preserved"))
            assert user.session_version == 0
            names = set((await db.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))).scalars())
            assert {"position_allocations", "order_intents", "binding_deliveries"} <= names
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_executor_serializes_tasks_and_is_reentrant():
    entered = asyncio.Event()
    release = asyncio.Event()
    sequence = []

    async def first():
        async with execution_lock():
            async with execution_lock():
                sequence.append("first")
                entered.set()
                await release.wait()
                sequence.append("first-done")

    async def second():
        await entered.wait()
        async with execution_lock():
            sequence.append("second")

    left = asyncio.create_task(first())
    right = asyncio.create_task(second())
    await entered.wait()
    await asyncio.sleep(0)
    assert sequence == ["first"]
    release.set()
    await asyncio.wait_for(asyncio.gather(left, right), timeout=2)
    assert sequence == ["first", "first-done", "second"]


@pytest.mark.asyncio
async def test_persisted_invalid_risk_configuration_is_rejected():
    from app.models.domain import SystemConfig
    from app.core.database import DEFAULT_SYSTEM_CONFIG
    from pydantic import ValidationError

    async with database.get_session_factory()() as db:
        cfg = await db.get(SystemConfig, "resilience")
        cfg.value_json = json.dumps({"max_consecutive_failures": -1})
        await db.commit()
        with pytest.raises(ValidationError):
            await CopyRouter._get_config(db, "resilience", DEFAULT_SYSTEM_CONFIG["resilience"])
