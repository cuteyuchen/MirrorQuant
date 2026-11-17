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
            # sqlite 驱动仅在写入时隐式 BEGIN IMMEDIATE，纯读取不占写锁。
            timeout_sec = get_settings().sqlite_busy_timeout_sec
            connect_args = {"check_same_thread": False, "timeout": timeout_sec, "isolation_level": "IMMEDIATE"}
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
                cursor.execute(f"PRAGMA busy_timeout={int(timeout_sec * 1000)};")
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
    "public_monitoring_enabled": False,  # 新部署必须显式开启公开看板。
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
        "dingtalk_secret": "",
        "outbox_enabled": False,
        "outbox_path": "",
        "notify_on_execution": True,
        "notify_on_lead_order": True,
        "notify_on_risk_block": True,
        "notify_on_security_alert": True,
    },
    "risk_defaults": {
        "enable_circuit_breaker": True,
        "enable_leverage_cap": False,
        "max_total_leverage": 8.0,
        "max_single_leverage": 4.0,
        "max_instrument_leverage": 20,
        "max_drawdown_pct": 30.0,
        "daily_max_loss_pct": 10.0,
        "max_slippage_pct": 1.0,
        "max_spread_pct": 0.3,
        "ioc_offset_pct": 0.5,
        "order_mode": "ioc_then_market",
        # 滑点超限：block=拦截；limit=挂限价单，limit_order_ttl_sec 秒内未成交自动撤单。
        "slippage_action": "block",
        "limit_order_ttl_sec": 120,
        "adopt_existing_positions": True,
        "adopt_tolerance_pct": 10.0,
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
        "flip_close_first": False,  # 与兼容 schema 一致；真实顺序始终由事件证据决定。
        "clock_sync_interval_sec": 300,
        "max_consecutive_failures": 5,
    },
}


# Idempotent SQLite column additions for upgrading existing databases without wiping data
SQLITE_MIGRATIONS: Dict[str, List[Tuple[str, str]]] = {
    "binding_position_progress": [("cycle_tracked", "BOOLEAN NOT NULL DEFAULT 0"),
                                  ("source_remaining_qty", "VARCHAR(100)")],
    "execution_records": [
        ("notification_state", "VARCHAR(32) NOT NULL DEFAULT 'unknown'"),
        ("fee", "FLOAT"),
        ("funding", "FLOAT"),
        ("exchange_fill_id", "VARCHAR(100)"),
        ("my_qty_exact", "VARCHAR(100)"),
        ("my_price_exact", "VARCHAR(100)"),
        ("delivery_event_id", "VARCHAR(100)"),
    ],
    "binding_deliveries": [
        ("exit_superseded_by", "VARCHAR(100)"),
        ("exit_account_id", "INTEGER"),
        ("exit_inst_id", "VARCHAR(64)"),
        ("exit_pos_side", "VARCHAR(16)"),
        ("exit_allocation_id", "INTEGER"),
        ("exit_generation", "INTEGER"),
        ("exit_initial_owned_qty", "VARCHAR(100)"),
        ("exit_target_qty", "VARCHAR(100)"),
        ("exit_filled_qty", "VARCHAR(100)"),
        ("exit_remaining_qty", "VARCHAR(100)"),
        ("exit_progress", "VARCHAR(32)"),
    ],
    "position_allocations": [
        ("owned_qty_exact", "VARCHAR(100)"),
        ("entry_cost_exact", "VARCHAR(100)"),
        ("generation", "INTEGER NOT NULL DEFAULT 0"),
        ("opened_at_ms", "BIGINT NOT NULL DEFAULT 0"),
    ],
    "order_intents": [("source_time_ms", "BIGINT NOT NULL DEFAULT 0"),
                      ("delivery_event_id", "VARCHAR(100)"),
                      ("reporting_complete", "BOOLEAN NOT NULL DEFAULT 0"),
                      ("expires_at_ms", "BIGINT"),
                      ("limit_px", "VARCHAR(100)")],
    "notification_outbox": [("execution_id", "INTEGER"), ("event_key", "VARCHAR(160)")],
    "binding_debts": [("pending_usd_exact", "VARCHAR(100)")],
    "users": [
        ("session_version", "INTEGER NOT NULL DEFAULT 0"),
        ("totp_enabled", "BOOLEAN NOT NULL DEFAULT 0"),
        ("totp_secret_enc", "TEXT"),
        ("totp_pending_secret_enc", "TEXT"),
    ],
    "trading_accounts": [
        ("risk_equity_json", "TEXT"),
        ("day_start_equity", "FLOAT NOT NULL DEFAULT 0.0"),
        ("day_start_date", "VARCHAR(16)"),
        ("clock_offset_ms", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "copy_bindings": [
        ("enable_circuit_breaker", "BOOLEAN NOT NULL DEFAULT 1"),
        ("enable_leverage_cap", "BOOLEAN NOT NULL DEFAULT 0"),
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
        # 旧绑定升级后保持原行为（拦截），需要挂限价单时在绑定里手动开启。
        ("slippage_action", "VARCHAR(16) NOT NULL DEFAULT 'block'"),
        ("limit_order_ttl_sec", "INTEGER NOT NULL DEFAULT 120"),
        ("adopt_existing_positions", "BOOLEAN NOT NULL DEFAULT 1"),
        ("adopt_tolerance_pct", "FLOAT NOT NULL DEFAULT 10.0"),
        ("adoption_completed", "BOOLEAN NOT NULL DEFAULT 0"),
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
        if table == "notification_outbox" and not {"execution_id", "event_key"} <= existing_cols:
            # 只在升级时回填旧关联键；日常证据检索使用索引，不再扫描整个队列。
            for row_id, raw in sync_conn.execute(text("SELECT id, payload FROM notification_outbox")):
                try:
                    payload = json.loads(raw)
                    execution_id = payload.get("execution_id")
                    event_key = payload.get("event_key")
                    sync_conn.execute(text(
                        "UPDATE notification_outbox SET execution_id=:execution_id, event_key=:event_key WHERE id=:id"
                    ), {"id": row_id, "execution_id": execution_id if type(execution_id) is int else None,
                        "event_key": event_key if isinstance(event_key, str) else None})
                except (ValueError, TypeError, AttributeError):
                    continue
        if table == "binding_deliveries" and "exit_target_qty" not in existing_cols:
            deliveries = sync_conn.execute(text(
                "SELECT binding_id, source_event_id, payload_json FROM binding_deliveries"
            )).fetchall()
            for binding_id, event_id, payload_json in deliveries:
                try:
                    payload = json.loads(payload_json)
                    is_exit = "_closed" in payload or (payload.get("side"), payload.get("positionSide")) in (
                        ("SELL", "LONG"), ("BUY", "SHORT"))
                except (ValueError, TypeError, AttributeError):
                    is_exit = True
                if is_exit:
                    sync_conn.execute(text(
                        "UPDATE binding_deliveries SET state='PENDING', exit_progress='REVIEW_REQUIRED' "
                        "WHERE binding_id=:binding_id AND source_event_id=:event_id"
                    ), {"binding_id": binding_id, "event_id": event_id})
    # create_all 不会为既有表补索引，升级时显式且幂等创建。
    for table, column in (("order_intents", "reporting_complete"),
                          ("notification_outbox", "execution_id"), ("notification_outbox", "event_key"),
                          ("notification_outbox", "created_at"), ("login_attempt_guards", "updated_at")):
        if column in {row[1] for row in sync_conn.execute(text(f"PRAGMA table_info({table})"))}:
            sync_conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{table}_{column} ON {table} ({column})"))


async def init_db() -> None:
    from app.models import domain  # noqa: F401

    engine = get_engine()
    async with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            # 驱动的隐式事务只覆盖 DML，DDL 升级也要失败时完整回滚。
            await conn.execute(text("BEGIN IMMEDIATE"))
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
