from __future__ import annotations

import json
import logging
from typing import AsyncGenerator, Dict, List, Tuple

from sqlalchemy import event, select, text, update
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
        "display_unit": "notional",
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
        # 保留在配置里（设置界面仍展示），但补单漂移上限已改用生效滑点
        # （跟单 max_slippage_pct，未设置时回退全局 risk_defaults），此处不再使用。
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
        ("spread_pct", "FLOAT"),
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
        ("cycle_ratio_exact", "VARCHAR(100)"),
        ("expected_qty_exact", "VARCHAR(100)"),
        ("lead_qty_exact", "VARCHAR(100)"),
        ("generation", "INTEGER NOT NULL DEFAULT 0"),
        ("opened_at_ms", "BIGINT NOT NULL DEFAULT 0"),
    ],
    "order_intents": [("source_time_ms", "BIGINT NOT NULL DEFAULT 0"),
                      ("delivery_event_id", "VARCHAR(100)"),
                      ("reporting_complete", "BOOLEAN NOT NULL DEFAULT 0"),
                      ("expires_at_ms", "BIGINT"),
                      ("limit_px", "VARCHAR(100)"),
                      ("sizing_px", "VARCHAR(100)"),
                      ("expected_delta_qty", "VARCHAR(100)"),
                      ("lead_delta_qty", "VARCHAR(100)"),
                      ("cycle_ratio_exact", "VARCHAR(100)")],
    "lead_sources": [
        ("notification_config_json", "TEXT"),
        ("created_by_user_id", "INTEGER"),
        ("slot_watch_enabled", "BOOLEAN NOT NULL DEFAULT 0"),
        ("deleted_at", "DATETIME"),
    ],
    "notification_outbox": [
        ("execution_id", "INTEGER"),
        ("event_key", "VARCHAR(160)"),
        ("owner_user_id", "INTEGER"),
        ("lead_id", "INTEGER"),
        ("destination_type", "VARCHAR(32)"),
    ],
    "binding_debts": [("pending_usd_exact", "VARCHAR(100)")],
    "users": [
        ("session_version", "INTEGER NOT NULL DEFAULT 0"),
        ("totp_enabled", "BOOLEAN NOT NULL DEFAULT 0"),
        ("totp_secret_enc", "TEXT"),
        ("totp_pending_secret_enc", "TEXT"),
        ("deleted_at", "DATETIME"),
    ],
    "trading_accounts": [
        ("risk_equity_json", "TEXT"),
        ("day_start_equity", "FLOAT NOT NULL DEFAULT 0.0"),
        ("day_start_date", "VARCHAR(16)"),
        ("clock_offset_ms", "INTEGER NOT NULL DEFAULT 0"),
        ("owner_user_id", "INTEGER"),
        ("deleted_at", "DATETIME"),
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
        # 三列可空：NULL 表示“未设置”，生效时回退到全局 risk_defaults。
        ("slippage_action", "VARCHAR(16) DEFAULT 'block'"),
        ("limit_order_ttl_sec", "INTEGER DEFAULT 120"),
        ("adopt_existing_positions", "BOOLEAN NOT NULL DEFAULT 1"),
        ("adopt_tolerance_pct", "FLOAT NOT NULL DEFAULT 10.0"),
        ("adoption_completed", "BOOLEAN NOT NULL DEFAULT 0"),
        ("auto_blacklist_manual", "BOOLEAN NOT NULL DEFAULT 1"),
        ("owner_user_id", "INTEGER"),
        ("deleted_at", "DATETIME"),
    ],
}


def _relax_copy_binding_slippage_columns(sync_conn) -> None:
    """Allow NULL on the slippage columns so NULL can mean "use global".

    SQLite cannot ALTER a column's nullability, so rebuild copy_bindings when
    the columns are still NOT NULL. Idempotent: no-op when already nullable.
    Existing explicit values are preserved; only the constraint changes.
    The rebuild runs inside the caller's transaction (init_db BEGIN IMMEDIATE),
    so a failure rolls back instead of leaving a half-rebuilt table.
    init_db disables foreign-key enforcement outside the transaction before
    running migrations (PRAGMA foreign_keys is a no-op inside one), because
    child tables reference copy_bindings and would block DROP TABLE; it
    re-enables enforcement and runs foreign_key_check after commit.
    Unique constraints are rebuilt anonymous: the old table still holds the
    named autoindex until it is dropped, and a duplicate name would fail the
    CREATE TABLE. The constraint itself (not its name) is what blocks dupes.
    """
    table = "copy_bindings"
    targets = ("max_slippage_pct", "slippage_action", "limit_order_ttl_sec")
    info = sync_conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    if not info:
        return
    cols = {row[1]: row for row in info}
    if any(name not in cols for name in targets):
        return
    if all(cols[name][3] == 0 for name in targets):
        return
    # Never rebuild over corrupt references: a failed INSERT would abort init
    # and the app would not start. Leave the columns NOT NULL in that case.
    violations = sync_conn.execute(text(f"PRAGMA foreign_key_check({table})")).fetchall()
    if violations:
        logger.warning("Skipping %s nullability migration: %d foreign-key violations", table, len(violations))
        return
    col_defs = []
    pk_cols = []
    for row in info:
        _cid, name, ctype, notnull, dflt, pk = row[0], row[1], row[2], row[3], row[4], row[5]
        if name in targets:
            notnull = 0
        parts = [f'"{name}" {ctype}']
        if notnull:
            parts.append("NOT NULL")
        if dflt is not None:
            parts.append(f"DEFAULT {dflt}")
        col_defs.append(" ".join(parts))
        if pk:
            pk_cols.append((pk, name))
    if pk_cols:
        pk_cols.sort()
        col_defs.append("PRIMARY KEY (" + ", ".join(f'"{name}"' for _, name in pk_cols) + ")")
    for idx in sync_conn.execute(text(f"PRAGMA index_list({table})")).fetchall():
        _seq, _idx_name, is_unique, origin, _partial = idx[0], idx[1], idx[2], idx[3], idx[4]
        if not is_unique or origin != "u":
            continue
        idx_cols = [r[2] for r in sync_conn.execute(text(f"PRAGMA index_info({_idx_name})")).fetchall()]
        if not idx_cols:
            continue
        col_defs.append("UNIQUE (" + ", ".join(f'"{c}"' for c in idx_cols) + ")")
    fk_groups = {}
    for row in sync_conn.execute(text(f"PRAGMA foreign_key_list({table})")).fetchall():
        _id, seq, ref_table, from_col, to_col, on_update, on_delete, match = row
        fk_groups.setdefault(_id, []).append((seq, ref_table, from_col, to_col, on_update, on_delete, match))
    for _id in sorted(fk_groups):
        parts = sorted(fk_groups[_id])
        ref_table = parts[0][1]
        from_cols = ", ".join(f'"{p[2]}"' for p in parts)
        to_cols = ", ".join(f'"{p[3]}"' for p in parts)
        fk = f'FOREIGN KEY ({from_cols}) REFERENCES "{ref_table}" ({to_cols})'
        on_update, on_delete, match = parts[0][4], parts[0][5], parts[0][6]
        if on_update and str(on_update).upper() != "NO ACTION":
            fk += f" ON UPDATE {on_update}"
        if on_delete and str(on_delete).upper() != "NO ACTION":
            fk += f" ON DELETE {on_delete}"
        if match and str(match).upper() != "NONE":
            fk += f" MATCH {match}"
        col_defs.append(fk)
    new_table = f"{table}_new_slippage_nullable"
    col_list = ", ".join(f'"{row[1]}"' for row in info)
    logger.info("Rebuilding SQLite table %s to allow NULL on %s", table, ", ".join(targets))
    sync_conn.execute(text(f"DROP TABLE IF EXISTS {new_table}"))
    sync_conn.execute(text(f"CREATE TABLE {new_table} ({', '.join(col_defs)})"))
    sync_conn.execute(text(f"INSERT INTO {new_table} ({col_list}) SELECT {col_list} FROM {table}"))
    sync_conn.execute(text(f"DROP TABLE {table}"))
    sync_conn.execute(text(f"ALTER TABLE {new_table} RENAME TO {table}"))


def _drop_legacy_table_unique_constraints(sync_conn) -> None:
    """Rebuild users / lead_sources / copy_bindings if legacy full-table UNIQUE
    constraints exist, so soft-deleted rows stop blocking re-creation.
    """
    for table in ("users", "lead_sources", "copy_bindings"):
        info = sync_conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        if not info:
            continue
        col_names = {row[1] for row in info}
        if "deleted_at" not in col_names:
            continue
        legacy_unique = [
            idx for idx in sync_conn.execute(text(f"PRAGMA index_list({table})")).fetchall()
            if idx[2] == 1 and idx[3] == "u"
        ]
        if not legacy_unique:
            continue
        violations = sync_conn.execute(text(f"PRAGMA foreign_key_check({table})")).fetchall()
        if violations:
            logger.warning("Skipping %s unique-constraint migration: %d foreign-key violations", table, len(violations))
            continue
        col_defs = []
        pk_cols = []
        for row in info:
            _cid, name, ctype, notnull, dflt, pk = row[0], row[1], row[2], row[3], row[4], row[5]
            parts = [f'"{name}" {ctype}']
            if notnull:
                parts.append("NOT NULL")
            if dflt is not None:
                parts.append(f"DEFAULT {dflt}")
            col_defs.append(" ".join(parts))
            if pk:
                pk_cols.append((pk, name))
        if pk_cols:
            pk_cols.sort()
            col_defs.append("PRIMARY KEY (" + ", ".join(f'"{name}"' for _, name in pk_cols) + ")")
        fk_groups = {}
        for row in sync_conn.execute(text(f"PRAGMA foreign_key_list({table})")).fetchall():
            _id, seq, ref_table, from_col, to_col, on_update, on_delete, match = row
            fk_groups.setdefault(_id, []).append((seq, ref_table, from_col, to_col, on_update, on_delete, match))
        for _id in sorted(fk_groups):
            parts = sorted(fk_groups[_id])
            ref_table = parts[0][1]
            from_cols = ", ".join(f'"{p[2]}"' for p in parts)
            to_cols = ", ".join(f'"{p[3]}"' for p in parts)
            fk = f'FOREIGN KEY ({from_cols}) REFERENCES "{ref_table}" ({to_cols})'
            on_update, on_delete, match = parts[0][4], parts[0][5], parts[0][6]
            if on_update and str(on_update).upper() != "NO ACTION":
                fk += f" ON UPDATE {on_update}"
            if on_delete and str(on_delete).upper() != "NO ACTION":
                fk += f" ON DELETE {on_delete}"
            if match and str(match).upper() != "NONE":
                fk += f" MATCH {match}"
            col_defs.append(fk)
        new_table = f"{table}_new_partial_unique"
        col_list = ", ".join(f'"{row[1]}"' for row in info)
        logger.info("Rebuilding SQLite table %s to replace legacy UNIQUE with partial unique index", table)
        sync_conn.execute(text(f"DROP TABLE IF EXISTS {new_table}"))
        sync_conn.execute(text(f"CREATE TABLE {new_table} ({', '.join(col_defs)})"))
        sync_conn.execute(text(f"INSERT INTO {new_table} ({col_list}) SELECT {col_list} FROM {table}"))
        sync_conn.execute(text(f"DROP TABLE {table}"))
        sync_conn.execute(text(f"ALTER TABLE {new_table} RENAME TO {table}"))


# 熬鹰资本的币安跟单组合 ID：名额监控的默认开启对象。
SLOT_WATCH_SEED_PORTFOLIO_ID = "5075281354358777856"
SLOT_WATCH_SEED_MARKER = "slot_watch_seeded"


def _seed_slot_watch(sync_conn) -> None:
    """One-time seed: enable slot watch for 熬鹰资本 only.

    Guarded by a system_configs marker so a later user opt-out is never
    re-enabled by a restart. No-op when the lead_sources table or the
    slot_watch_enabled column is not present yet.
    """
    cols = {r[1] for r in sync_conn.execute(text("PRAGMA table_info(lead_sources)")).fetchall()}
    if not {"slot_watch_enabled", "portfolio_id"} <= cols:
        # 旧版表（例如迁移测试里的遗留结构）缺少列时跳过，不挡启动。
        return
    tables = {r[0] for r in sync_conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}
    if "system_configs" not in tables:
        return
    marker = sync_conn.execute(
        text("SELECT COUNT(*) FROM system_configs WHERE key=:key"),
        {"key": SLOT_WATCH_SEED_MARKER},
    ).scalar()
    if marker:
        return
    sync_conn.execute(
        text("UPDATE lead_sources SET slot_watch_enabled=1 WHERE portfolio_id=:pid"),
        {"pid": SLOT_WATCH_SEED_PORTFOLIO_ID},
    )
    sync_conn.execute(
        text("INSERT INTO system_configs (key, value_json, updated_at) "
             "VALUES (:key, :value, datetime('now'))"),
        {"key": SLOT_WATCH_SEED_MARKER, "value": json.dumps("1.3.5", ensure_ascii=False)},
    )
    logger.info("Seeded slot watch for portfolio %s", SLOT_WATCH_SEED_PORTFOLIO_ID)


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
    # 补列完成后才放宽可空性：重建 copy_bindings 需要三列都已存在。
    _relax_copy_binding_slippage_columns(sync_conn)
    _drop_legacy_table_unique_constraints(sync_conn)
    # 名额监控一次性种子：只给熬鹰资本默认开启，标记防重复。
    _seed_slot_watch(sync_conn)
    # create_all 不会为既有表补索引，升级时显式且幂等创建。
    for table, column in (("order_intents", "reporting_complete"),
                          ("notification_outbox", "execution_id"), ("notification_outbox", "event_key"),
                          ("notification_outbox", "owner_user_id"), ("notification_outbox", "lead_id"),
                          ("notification_outbox", "destination_type"),
                          ("trading_accounts", "owner_user_id"), ("copy_bindings", "owner_user_id"),
                          ("notification_outbox", "created_at"), ("login_attempt_guards", "updated_at")):
        if column in {row[1] for row in sync_conn.execute(text(f"PRAGMA table_info({table})"))}:
            sync_conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{table}_{column} ON {table} ({column})"))
    # 部分唯一索引：软删除记录不再阻断重新创建。
    for idx_name, table, cols in (
        ("uq_users_username_active", "users", "username"),
        ("uq_lead_platform_portfolio_active", "lead_sources", "platform, portfolio_id"),
        ("uq_binding_lead_account_active", "copy_bindings", "lead_id, account_id"),
    ):
        table_cols = {row[1] for row in sync_conn.execute(text(f"PRAGMA table_info({table})"))}
        required_cols = {col.strip() for col in cols.split(",")} | {"deleted_at"}
        if required_cols <= table_cols:
            sync_conn.execute(text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {idx_name} ON {table} ({cols}) WHERE deleted_at IS NULL"
            ))
    # 执行记录复合索引，加速分页与分组统计。
    exec_cols = {row[1] for row in sync_conn.execute(text("PRAGMA table_info(execution_records)"))}
    if {"binding_id", "account_id", "status", "created_at"} <= exec_cols:
        sync_conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_exec_binding_created ON execution_records (binding_id, created_at DESC)"
        ))
        sync_conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_exec_account_created ON execution_records (account_id, created_at DESC)"
        ))
        sync_conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_exec_status_created ON execution_records (status, created_at DESC)"
        ))
    # 账户详情新表索引幂等补建：兼容从历史副本升级。
    for ddl in (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_acct_eq_snap_bucket ON account_equity_snapshots (account_id, currency, resolution, bucket_start_ms)",
        "CREATE INDEX IF NOT EXISTS ix_acct_eq_snap_captured ON account_equity_snapshots (account_id, currency, captured_at_ms)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_acct_cashflow_ext ON account_cashflow_entries (account_id, exchange, external_id, raw_type) WHERE external_id IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_acct_cashflow_dedupe ON account_cashflow_entries (account_id, exchange, dedupe_key)",
        "CREATE INDEX IF NOT EXISTS ix_acct_cashflow_time ON account_cashflow_entries (account_id, occurred_at_ms, id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_acct_trade_fill ON account_trade_fills (account_id, exchange, exchange_fill_id)",
        "CREATE INDEX IF NOT EXISTS ix_acct_fill_time ON account_trade_fills (account_id, occurred_at_ms, id)",
        "CREATE INDEX IF NOT EXISTS ix_acct_fill_sym_time ON account_trade_fills (account_id, symbol, occurred_at_ms)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_acct_order_hist_order_id ON account_order_history (account_id, exchange, exchange_order_id) WHERE exchange_order_id IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_acct_order_hist_algo_id ON account_order_history (account_id, exchange, algo_id) WHERE algo_id IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_acct_order_hist_dedupe ON account_order_history (account_id, exchange, dedupe_key)",
        "CREATE INDEX IF NOT EXISTS ix_acct_order_hist_time ON account_order_history (account_id, updated_at_ms, id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_acct_dataset_sync ON account_dataset_sync_states (account_id, dataset)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_acct_alert_state ON account_alert_states (account_id, code, scope_key)",
        "CREATE INDEX IF NOT EXISTS ix_acct_alert_active ON account_alert_states (account_id, status, severity)",
    ):
        table_name = ddl.split(" ON ", 1)[1].split(" ", 1)[0]
        if sync_conn.execute(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"), {"n": table_name}).scalar():
            sync_conn.execute(text(ddl))


async def init_db() -> None:
    from app.models import domain  # noqa: F401

    engine = get_engine()
    # 外键开关只能在无事务时切换（事务内 PRAGMA foreign_keys 是 no-op），
    # 且 SQLAlchemy 的 execute 会自动开启事务，所以开关必须绕过它、直接走
    # DBAPI cursor（run_sync + sync_conn.connection），否则 conn.begin()
    # 会因已自动开启事务而报错。迁移里重建 copy_bindings 要 DROP 父表，
    # 子表外键会挡住，所以先在事务外关掉，提交后再重开并校验；失败回滚，
    # 开关状态不影响已提交数据。
    async with engine.connect() as conn:
        if engine.dialect.name == "sqlite":
            def _fk_off(sync_conn):
                cur = sync_conn.connection.cursor()
                cur.execute("PRAGMA foreign_keys=OFF")
                cur.close()

            def _fk_on(sync_conn):
                cur = sync_conn.connection.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()

            await conn.run_sync(_fk_off)
        async with conn.begin():
            if engine.dialect.name == "sqlite":
                # 驱动的隐式事务只覆盖 DML，DDL 升级也要失败时完整回滚。
                await conn.execute(text("BEGIN IMMEDIATE"))
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_apply_sqlite_migrations)
        if engine.dialect.name == "sqlite":
            await conn.run_sync(_fk_on)
            violations = (await conn.execute(text("PRAGMA foreign_key_check"))).fetchall()
            if violations:
                raise RuntimeError(f"Foreign key violations after migration: {violations[:5]}")

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

        # Backfill default admin's ownership and preferences if admin exists
        first_admin = await session.scalar(
            select(domain.User).where(domain.User.role == "admin").order_by(domain.User.id)
        )
        if first_admin is not None:
            await session.execute(
                update(domain.TradingAccount)
                .where(domain.TradingAccount.owner_user_id.is_(None))
                .values(owner_user_id=first_admin.id)
            )
            await session.execute(
                update(domain.CopyBinding)
                .where(domain.CopyBinding.owner_user_id.is_(None))
                .values(owner_user_id=first_admin.id)
            )
            admin_pref = await session.get(domain.UserPreference, first_admin.id)
            if admin_pref is None:
                notify_cfg = await session.scalar(
                    select(domain.SystemConfig).where(domain.SystemConfig.key == "notifications")
                )
                session.add(
                    domain.UserPreference(
                        user_id=first_admin.id,
                        notifications_json=notify_cfg.value_json if notify_cfg else "{}",
                    )
                )
        await session.commit()
