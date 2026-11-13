from __future__ import annotations

import json
import logging
from typing import AsyncGenerator, Dict, List, Tuple

from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

logger = logging.getLogger("mirrorquant.database")


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
                cursor.execute("PRAGMA synchronous=FULL;")
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
    "bootstrap_claimed": False,
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
        "dingtalk_enabled": False,
        "dingtalk_webhook_url": "",
        "outbox_enabled": False,
        "outbox_path": "",
        "notify_on_execution": True,
        "notify_on_lead_order": True,
        "notify_on_risk_block": True,
        "notify_on_security_alert": True,
    },
    "risk_defaults": {
        "max_total_leverage": 8.0,
        "max_single_leverage": 4.0,
        "max_instrument_leverage": 20,
        "max_drawdown_pct": 30.0,
        "daily_max_loss_pct": 10.0,
        "max_slippage_pct": 1.0,
        "max_spread_pct": 0.3,
        "ioc_offset_pct": 0.5,
        "order_mode": "ioc_then_market",
        "tp_sl_mode": "follow_lead",
        "enable_stop_loss": False,
        "stop_loss_pct": 5.0,
        "enable_take_profit": False,
        "take_profit_pct": 10.0,
        "enable_trailing_stop": False,
        "trailing_callback_pct": 2.0,
        "hard_stop_loss_pct": 15.0,
    },
    "resilience": {
        "missed_order_max_age_sec": 900,
        "catchup_max_price_dev_pct": 0.5,
        "reconcile_interval_sec": 60,
        "auto_close_orphan_positions": "alert_only",  # alert_only | auto_close
        "insufficient_margin_policy": "auto_scale_down",  # auto_scale_down | skip_alert
        "flip_close_first": True,
        "clock_sync_interval_sec": 300,
        "max_consecutive_failures": 5,
    },
}


# Idempotent SQLite column additions for upgrading existing databases without wiping data
SQLITE_MIGRATIONS: Dict[str, List[Tuple[str, str]]] = {
    "position_allocations": [
        ("owned_qty_exact", "VARCHAR(100)"),
        ("entry_cost_exact", "VARCHAR(100)"),
        ("generation", "INTEGER NOT NULL DEFAULT 0"),
        ("opened_at_ms", "BIGINT NOT NULL DEFAULT 0"),
    ],
    "order_intents": [("source_time_ms", "BIGINT NOT NULL DEFAULT 0")],
    "binding_debts": [("pending_usd_exact", "VARCHAR(100)")],
    "users": [
        ("session_version", "INTEGER NOT NULL DEFAULT 0"),
        ("totp_enabled", "BOOLEAN NOT NULL DEFAULT 0"),
        ("totp_secret_enc", "TEXT"),
        ("totp_pending_secret_enc", "TEXT"),
    ],
    "trading_accounts": [
        ("day_start_equity", "FLOAT NOT NULL DEFAULT 0.0"),
        ("day_start_date", "VARCHAR(16)"),
        ("clock_offset_ms", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "copy_bindings": [
        ("daily_max_loss_pct", "FLOAT NOT NULL DEFAULT 10.0"),
        ("max_spread_pct", "FLOAT NOT NULL DEFAULT 0.3"),
        ("tp_sl_mode", "VARCHAR(32) NOT NULL DEFAULT 'follow_lead'"),
        ("enable_stop_loss", "BOOLEAN NOT NULL DEFAULT 0"),
        ("stop_loss_pct", "FLOAT NOT NULL DEFAULT 5.0"),
        ("enable_take_profit", "BOOLEAN NOT NULL DEFAULT 0"),
        ("take_profit_pct", "FLOAT NOT NULL DEFAULT 10.0"),
        ("enable_trailing_stop", "BOOLEAN NOT NULL DEFAULT 0"),
        ("trailing_callback_pct", "FLOAT NOT NULL DEFAULT 2.0"),
        ("hard_stop_loss_pct", "FLOAT NOT NULL DEFAULT 15.0"),
        ("consecutive_failures", "INTEGER NOT NULL DEFAULT 0"),
    ],
}


def _apply_sqlite_migrations(sync_conn) -> None:
    dialect_name = getattr(sync_conn.dialect, "name", "")
    if dialect_name != "sqlite":
        return
    for table, columns in SQLITE_MIGRATIONS.items():
        rows = sync_conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        existing_cols = {r[1] for r in rows}
        if not existing_cols:
            continue
        for col_name, col_def in columns:
            if col_name not in existing_cols:
                logger.info("Auto-migrating SQLite column %s.%s", table, col_name)
                sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}"))


async def init_db() -> None:
    from app.models import domain  # noqa: F401

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_apply_sqlite_migrations)

    factory = get_session_factory()
    async with factory() as session:
        for k, default_val in DEFAULT_SYSTEM_CONFIG.items():
            existing = await session.scalar(
                select(domain.SystemConfig).where(domain.SystemConfig.key == k)
            )
            if existing is None:
                session.add(
                    domain.SystemConfig(
                        key=k,
                        value_json=json.dumps(default_val, ensure_ascii=False),
                    )
                )
            elif isinstance(default_val, dict):
                try:
                    cur = json.loads(existing.value_json)
                    if isinstance(cur, dict):
                        merged = {**default_val, **cur}
                        if merged != cur:
                            existing.value_json = json.dumps(merged, ensure_ascii=False)
                except Exception:
                    pass
        await session.commit()
