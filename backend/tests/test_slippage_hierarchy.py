"""滑点层级：跟单设置优先，未设置时回退全局 risk_defaults。

覆盖：生效值解析、建绑定 schema（不传存 NULL）、更新显式 null 清回全局、
以及把旧库 copy_bindings 三列从 NOT NULL 放宽为可空的 SQLite 迁移。
"""
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, text

from app.core.database import (
    DEFAULT_SYSTEM_CONFIG,
    _apply_sqlite_migrations,
    init_db,
    reset_db_engine,
    reset_engine_for_tests,
)
from app.schemas.api import BindingCreate, BindingUpdate
from app.services.engine.router import _effective_slippage_settings


def _global_defaults(**overrides):
    rd = dict(DEFAULT_SYSTEM_CONFIG["risk_defaults"])
    rd.update(overrides)
    return rd


# ---- 生效值解析 ----

def test_effective_slippage_uses_global_when_binding_unset():
    binding = SimpleNamespace(max_slippage_pct=None, slippage_action=None, limit_order_ttl_sec=None)
    assert _effective_slippage_settings(binding, _global_defaults()) == (1.0, "block", 120)


def test_effective_slippage_binding_wins_when_set():
    binding = SimpleNamespace(max_slippage_pct=2.0, slippage_action="limit", limit_order_ttl_sec=600)
    assert _effective_slippage_settings(binding, _global_defaults()) == (2.0, "limit", 600)


def test_effective_slippage_partial_override():
    # 只设滑点：动作与 TTL 仍回退全局。
    binding = SimpleNamespace(max_slippage_pct=0.5, slippage_action=None, limit_order_ttl_sec=None)
    assert _effective_slippage_settings(binding, _global_defaults()) == (0.5, "block", 120)


def test_effective_slippage_global_change_propagates():
    binding = SimpleNamespace(max_slippage_pct=None, slippage_action=None, limit_order_ttl_sec=None)
    assert _effective_slippage_settings(binding, _global_defaults(max_slippage_pct=2.0)) == (2.0, "block", 120)
    assert _effective_slippage_settings(binding, _global_defaults(slippage_action="limit")) == (1.0, "limit", 120)
    assert _effective_slippage_settings(binding, _global_defaults(limit_order_ttl_sec=600)) == (1.0, "block", 600)


def test_effective_slippage_missing_global_keys_fall_back_to_defaults():
    binding = SimpleNamespace(max_slippage_pct=None, slippage_action=None, limit_order_ttl_sec=None)
    assert _effective_slippage_settings(binding, {}) == (1.0, "block", 120)
    assert _effective_slippage_settings(binding, None) == (1.0, "block", 120)
    # 全局字典里显式 null 也一样回退，不崩。
    assert _effective_slippage_settings(
        binding, {"max_slippage_pct": None, "slippage_action": None, "limit_order_ttl_sec": None}
    ) == (1.0, "block", 120)


# ---- 建绑定 / 更新 schema ----

def test_binding_create_defaults_slippage_fields_to_null():
    payload = BindingCreate(name="b", lead_id=1, account_id=1)
    assert payload.max_slippage_pct is None
    assert payload.slippage_action is None
    assert payload.limit_order_ttl_sec is None


def test_binding_create_stores_explicit_slippage_values():
    payload = BindingCreate(name="b", lead_id=1, account_id=1,
                            max_slippage_pct=2.0, slippage_action="limit", limit_order_ttl_sec=600)
    assert (payload.max_slippage_pct, payload.slippage_action, payload.limit_order_ttl_sec) == (2.0, "limit", 600)


def test_binding_update_allows_explicit_null_to_clear_back_to_global():
    # 显式 null 通过校验，且 exclude_unset 会把它带上（更新端据此清回 NULL）。
    payload = BindingUpdate(max_slippage_pct=None, slippage_action=None, limit_order_ttl_sec=None)
    dumped = payload.model_dump(exclude_unset=True)
    assert dumped == {"max_slippage_pct": None, "slippage_action": None, "limit_order_ttl_sec": None}


def test_binding_update_omitted_slippage_fields_stay_unset():
    payload = BindingUpdate(name="renamed")
    assert payload.model_dump(exclude_unset=True) == {"name": "renamed"}


def test_binding_update_rejects_null_for_non_nullable_fields():
    with pytest.raises(ValidationError):
        BindingUpdate(risk_scale=None)


# ---- SQLite 迁移：旧库三列从 NOT NULL 放宽为可空 ----

OLD_DDL = """
CREATE TABLE copy_bindings (
    id INTEGER NOT NULL,
    owner_user_id INTEGER,
    name VARCHAR(120) NOT NULL,
    lead_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    is_enabled BOOLEAN NOT NULL,
    max_slippage_pct FLOAT NOT NULL,
    slippage_action VARCHAR(16) NOT NULL,
    limit_order_ttl_sec INTEGER NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_binding_lead_account UNIQUE (lead_id, account_id),
    FOREIGN KEY(lead_id) REFERENCES lead_sources (id) ON DELETE CASCADE,
    FOREIGN KEY(account_id) REFERENCES trading_accounts (id) ON DELETE CASCADE
)
"""


def _nullability(conn):
    rows = conn.execute(text("PRAGMA table_info(copy_bindings)")).fetchall()
    return {r[1]: r[3] for r in rows}


def test_migration_relaxes_slippage_columns_to_nullable():
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE lead_sources (id INTEGER NOT NULL, PRIMARY KEY (id))"))
        conn.execute(text("CREATE TABLE trading_accounts (id INTEGER NOT NULL, PRIMARY KEY (id))"))
        conn.execute(text(OLD_DDL))
        conn.execute(text("CREATE INDEX ix_copy_bindings_owner_user_id ON copy_bindings (owner_user_id)"))
        conn.execute(text("INSERT INTO lead_sources (id) VALUES (1), (2)"))
        conn.execute(text("INSERT INTO trading_accounts (id) VALUES (1), (2)"))
        conn.execute(text(
            "INSERT INTO copy_bindings (id, name, lead_id, account_id, is_enabled, "
            "max_slippage_pct, slippage_action, limit_order_ttl_sec) "
            "VALUES (1, 'primary', 1, 1, 1, 1.0, 'limit', 600)"
        ))
        conn.commit()
        before = _nullability(conn)
        assert before["max_slippage_pct"] == 1 and before["slippage_action"] == 1 and before["limit_order_ttl_sec"] == 1

        _apply_sqlite_migrations(conn)
        after = _nullability(conn)
        assert after["max_slippage_pct"] == 0 and after["slippage_action"] == 0 and after["limit_order_ttl_sec"] == 0
        conn.commit()

        # 旧行显式值原样保留。
        row = conn.execute(text(
            "SELECT id, name, max_slippage_pct, slippage_action, limit_order_ttl_sec "
            "FROM copy_bindings WHERE id=1")).fetchone()
        assert tuple(row) == (1, "primary", 1.0, "limit", 600)
        # 唯一约束仍在。
        with pytest.raises(Exception):
            conn.execute(text(
                "INSERT INTO copy_bindings (id, name, lead_id, account_id, is_enabled, "
                "max_slippage_pct, slippage_action, limit_order_ttl_sec) "
                "VALUES (2, 'dup', 1, 1, 1, 1.0, 'block', 120)"
            ))
        conn.rollback()
        # 现在可以存 NULL。
        conn.execute(text(
            "INSERT INTO copy_bindings (id, name, lead_id, account_id, is_enabled, "
            "max_slippage_pct, slippage_action, limit_order_ttl_sec) "
            "VALUES (3, 'nulls', 2, 2, 1, NULL, NULL, NULL)"
        ))
        row3 = conn.execute(text(
            "SELECT max_slippage_pct, slippage_action, limit_order_ttl_sec "
            "FROM copy_bindings WHERE id=3")).fetchone()
        assert tuple(row3) == (None, None, None)
        conn.commit()
        # 外键定义仍在。
        fks = conn.execute(text("PRAGMA foreign_key_list(copy_bindings)")).fetchall()
        assert ("lead_sources", "lead_id", "id") in [(r[2], r[3], r[4]) for r in fks]
        assert ("trading_accounts", "account_id", "id") in [(r[2], r[3], r[4]) for r in fks]
        # 索引仍在。
        idx = conn.execute(text("PRAGMA index_list(copy_bindings)")).fetchall()
        assert any(r[1] == "ix_copy_bindings_owner_user_id" for r in idx)
        # 幂等：再跑一次无变化、不丢数据。
        _apply_sqlite_migrations(conn)
        assert _nullability(conn)["max_slippage_pct"] == 0
        assert conn.execute(text("SELECT COUNT(*) FROM copy_bindings")).scalar() == 2
        conn.commit()


@pytest.mark.asyncio
async def test_init_db_migrates_with_child_fk_references():
    """回归：生产库子表外键指着 copy_bindings 时，init_db 不能崩。

    2026-11-21 1.3.1 首次部署失败：_relax_copy_binding_slippage_columns
    的 DROP TABLE copy_bindings 被子表外键挡住（FK 强制开启），容器起不来。
    修复：init_db 在事务外先关外键、提交后再重开并校验。本测试复现生产
    场景——旧 schema + 子表外键 + FK 强制开启——走真实 init_db 验证。
    """
    tmp = Path(tempfile.mkdtemp()) / "fk-migration.db"
    raw = sqlite3.connect(tmp)
    try:
        raw.execute("PRAGMA foreign_keys=ON")
        raw.execute("CREATE TABLE lead_sources (id INTEGER NOT NULL, PRIMARY KEY (id))")
        raw.execute("CREATE TABLE trading_accounts (id INTEGER NOT NULL, PRIMARY KEY (id))")
        raw.execute(OLD_DDL)
        # 生产同款：子表外键引用 copy_bindings（持仓/意图/执行记录皆如此）。
        raw.execute(
            "CREATE TABLE position_allocations ("
            "id INTEGER NOT NULL, binding_id INTEGER NOT NULL, symbol VARCHAR(32) NOT NULL, "
            "PRIMARY KEY (id), FOREIGN KEY(binding_id) REFERENCES copy_bindings (id))"
        )
        raw.execute("INSERT INTO lead_sources (id) VALUES (1)")
        raw.execute("INSERT INTO trading_accounts (id) VALUES (1)")
        raw.execute(
            "INSERT INTO copy_bindings (id, name, lead_id, account_id, is_enabled, "
            "max_slippage_pct, slippage_action, limit_order_ttl_sec) "
            "VALUES (1, 'primary', 1, 1, 1, 1.0, 'limit', 600)"
        )
        raw.execute("INSERT INTO position_allocations (id, binding_id, symbol) VALUES (1, 1, 'ZECUSDT')")
        raw.commit()
        # 确认外键强制确实开着：插一条指向不存在绑定的子表行必须失败。
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute("INSERT INTO position_allocations (id, binding_id, symbol) VALUES (2, 999, 'BTCUSDT')")
        raw.rollback()
    finally:
        raw.close()

    try:
        reset_engine_for_tests(f"sqlite+aiosqlite:///{tmp}")
        await init_db()  # 真实入口：事务外关外键 -> 迁移 -> 提交 -> 重开并校验
        # 迁移成功：三列已放宽，数据与子表行完好。
        check = sqlite3.connect(tmp)
        try:
            nulls = {r[1]: r[3] for r in check.execute("PRAGMA table_info(copy_bindings)")}
            assert nulls["max_slippage_pct"] == 0 and nulls["slippage_action"] == 0
            assert nulls["limit_order_ttl_sec"] == 0
            assert check.execute("SELECT COUNT(*) FROM copy_bindings").fetchone()[0] == 1
            assert check.execute(
                "SELECT max_slippage_pct, slippage_action, limit_order_ttl_sec "
                "FROM copy_bindings WHERE id=1").fetchone() == (1.0, "limit", 600)
            assert check.execute("SELECT COUNT(*) FROM position_allocations").fetchone()[0] == 1
            assert check.execute("PRAGMA foreign_key_check").fetchall() == []
            # 外键仍在强制：坏引用依旧拒绝。
            check.execute("PRAGMA foreign_keys=ON")
            with pytest.raises(sqlite3.IntegrityError):
                check.execute(
                    "INSERT INTO position_allocations (id, binding_id, symbol) VALUES (2, 999, 'BTCUSDT')")
            check.rollback()
        finally:
            check.close()
        # 幂等：再跑一次 init_db 无变化、不丢数据。
        await init_db()
        again = sqlite3.connect(tmp)
        try:
            assert again.execute("SELECT COUNT(*) FROM copy_bindings").fetchone()[0] == 1
            assert again.execute("SELECT COUNT(*) FROM position_allocations").fetchone()[0] == 1
        finally:
            again.close()
    finally:
        await reset_db_engine()
