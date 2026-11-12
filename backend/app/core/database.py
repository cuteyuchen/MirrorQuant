from __future__ import annotations

import json
from typing import AsyncGenerator

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine(database_url: str | None = None) -> AsyncEngine:
    global _engine
    if _engine is None:
        url = database_url or get_settings().resolved_database_url()
        connect_args = {}
        if url.startswith("sqlite"):
            connect_args = {"check_same_thread": False, "timeout": 30}
        engine = create_async_engine(
            url,
            echo=False,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
        if url.startswith("sqlite"):
            @event.listens_for(engine.sync_engine, "connect")
            def _set_sqlite_pragmas(dbapi_conn, _conn_record):
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA journal_mode=WAL;")
                cursor.execute("PRAGMA synchronous=NORMAL;")
                cursor.execute("PRAGMA foreign_keys=ON;")
                cursor.execute("PRAGMA busy_timeout=5000;")
                cursor.close()
        _engine = engine
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _session_factory


def reset_engine_for_tests(database_url: str) -> AsyncEngine:
    global _engine, _session_factory
    _engine = None
    _session_factory = None
    return get_engine(database_url)


async def reset_db_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    factory = get_session_factory()
    async with factory() as session:
        yield session


DEFAULT_SYSTEM_CONFIG = {
    "global_kill_switch": False,
    "public_monitoring_enabled": True,
    "notifications": {
        "webhook_enabled": False,
        "webhook_url": "",
        "telegram_enabled": False,
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "wecom_enabled": False,
        "wecom_webhook_url": "",
        "notify_on_execution": True,
        "notify_on_lead_order": True,
        "notify_on_risk_block": True,
    },
    "risk_defaults": {
        "max_total_leverage": 8.0,
        "max_single_leverage": 4.0,
        "max_instrument_leverage": 20,
        "max_drawdown_pct": 30.0,
        "max_slippage_pct": 1.0,
        "ioc_offset_pct": 0.5,
    },
}


async def init_db() -> None:
    from app.models import domain  # noqa: F401

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = get_session_factory()
    async with factory() as session:
        for k, v in DEFAULT_SYSTEM_CONFIG.items():
            existing = await session.scalar(
                select(domain.SystemConfig).where(domain.SystemConfig.key == k)
            )
            if existing is None:
                session.add(
                    domain.SystemConfig(
                        key=k,
                        value_json=json.dumps(v, ensure_ascii=False),
                    )
                )
        await session.commit()
