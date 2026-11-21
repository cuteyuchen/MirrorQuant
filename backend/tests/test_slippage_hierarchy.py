"""滑点层级：跟单设置优先，未设置时回退全局 risk_defaults。

覆盖：生效值解析、建绑定 schema（不传存 NULL）、更新显式 null 清回全局、
以及把旧库 copy_bindings 三列从 NOT NULL 放宽为可空的 SQLite 迁移。
"""
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, text

from app.core.database import DEFAULT_SYSTEM_CONFIG, _apply_sqlite_migrations
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
