"""保留期、历史审计和告警重启恢复均不接触外部服务。"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from sqlalchemy import event, select, text
import pytest

from app.core.config import get_settings
from app.core.database import get_engine, get_session_factory
from app.core.security import CredentialVault
from app.maintenance.retention import cleanup_retained_records
from app.models.domain import (
    AuditLog, BindingDelivery, CopyBinding, ExecutionRecord, LeadSource, LoginAttemptGuard,
    NotificationOutbox, OrderIntent, SystemConfig, TradingAccount,
)
from app.services.engine.router import CopyRouter
from test_router_remediations import NOW_MS, OfflineExchange, seed_allocation, trading


def record(t, order_id, created_at, **extra):
    values = dict(binding_id=t.binding.id, account_id=t.account.id, lead_id=t.lead.id,
                  account_name=t.account.name, lead_name=t.lead.name, inst_id="BTCUSDT", symbol="BTCUSDT",
                  side="BUY", position_side="LONG", action="OPEN_LONG", status="EXECUTED",
                  order_id=order_id, created_at=created_at, notification_state="not_required")
    values.update(extra)
    return ExecutionRecord(**values)


async def test_retention_removes_expired_terminal_rows_but_keeps_unresolved_evidence(trading, monkeypatch):
    t = trading
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=91)
    monkeypatch.setattr(get_settings(), "retention_days", 90)
    done = record(t, "done", old, notification_state="pending")
    unsafe = record(t, "unsafe", old)
    recent = record(t, "recent", now)
    t.db.add_all([done, unsafe, recent])
    await t.db.flush()
    t.db.add(OrderIntent(id="retention-unknown", account_id=t.account.id, binding_id=t.binding.id,
        inst_id="BTCUSDT", pos_side="long", source_event_id="unknown", state="UNKNOWN",
        requested_qty="1", filled_qty="0", avg_px="0", reduce_only=False, exchange_order_id="unsafe"))
    for execution in (done, unsafe):
        t.db.add(NotificationOutbox(created_at=old, status="sent", payload=json.dumps({
            "execution_id": execution.id, "event_key": f"execution:{execution.id}",
            "channels": [], "delivered_channels": []})))
    pending = NotificationOutbox(created_at=old, status="pending", payload="{}")
    t.db.add_all([
        pending,
        AuditLog(action="OLD", username="test", resource_type="test", created_at=old),
        AuditLog(action="NEW", username="test", resource_type="test", created_at=now),
        LoginAttemptGuard(guard_key="old", key_type="ip", key_value="old", updated_at=old,
                          last_fail_at=old, locked_until=old),
        LoginAttemptGuard(guard_key="active", key_type="ip", key_value="active", updated_at=old,
                          last_fail_at=old, locked_until=now + timedelta(hours=1)),
    ])
    await t.db.commit()
    done_id, unsafe_id, recent_id = done.id, unsafe.id, recent.id
    counts = await cleanup_retained_records(t.db, now=now)
    assert counts == {"execution_records": 1, "notification_outbox": 1, "audit_logs": 1, "login_attempt_guards": 1}
    assert set((await t.db.scalars(select(ExecutionRecord.id))).all()) == {unsafe_id, recent_id}
    assert done_id not in (await t.db.scalars(select(ExecutionRecord.id))).all()
    assert (await t.db.get(NotificationOutbox, pending.id)).status == "pending"
    assert (await t.db.scalar(select(LoginAttemptGuard.guard_key))) == "active"


@pytest.mark.parametrize("progress,state,event", [
    ("REVIEW_REQUIRED", "PENDING", "review"),
    ("WAITING_ORDER", "PENDING", "waiting"),
    ("REVIEW_REQUIRED", "APPLIED", None),
])
async def test_retention_keeps_pending_or_review_delivery_evidence(trading, progress, state, event):
    # 投递未完结、人工复核或缺少旧关联键时，保留执行及其终态通知回执。
    t = trading
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=get_settings().retention_days + 1)
    execution = record(t, "review-order", old, delivery_event_id=event)
    t.db.add(execution)
    await t.db.flush()
    t.db.add_all([
        BindingDelivery(binding_id=t.binding.id, source_event_id=event or "legacy-review",
                        source_sequence=1, payload_json="{}", state=state, exit_progress=progress),
        NotificationOutbox(created_at=old, status="sent", payload=json.dumps({
            "execution_id": execution.id, "event_key": f"execution:{execution.id}",
            "channels": [], "delivered_channels": [],
        })),
    ])
    await t.db.commit()
    counts = await cleanup_retained_records(t.db, now=now)
    assert counts["execution_records"] == counts["notification_outbox"] == 0
    assert await t.db.get(ExecutionRecord, execution.id) is not None


async def test_retention_keeps_unreported_terminal_intent_evidence(trading):
    # 交易所终态不等价于本地账本已审计，未完成投影时不能清理历史证据。
    t = trading
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=get_settings().retention_days + 1)
    execution = record(t, "unreported-terminal", old)
    t.db.add(execution)
    t.db.add(OrderIntent(id="retention-unreported", account_id=t.account.id, binding_id=t.binding.id,
        inst_id="BTCUSDT", pos_side="long", source_event_id="unreported", state="FILLED",
        requested_qty="1", filled_qty="1", avg_px="100", reduce_only=False,
        exchange_order_id="unreported-terminal", reporting_complete=False))
    await t.db.commit()
    assert (await cleanup_retained_records(t.db, now=now))["execution_records"] == 0
    assert await t.db.get(ExecutionRecord, execution.id) is not None


async def test_reconcile_queries_only_unresolved_orders_and_skips_verified_terminal_history(trading):
    t = trading
    for i in range(10):
        t.db.add(OrderIntent(id=f"verified-{i}", account_id=t.account.id, binding_id=t.binding.id,
            inst_id="BTCUSDT", pos_side="long", source_event_id=f"past-{i}", state="CANCELED",
            requested_qty="1", filled_qty="0", avg_px="0", reduce_only=False, reporting_complete=True))
    t.db.add(OrderIntent(id="pending-one", account_id=t.account.id, binding_id=t.binding.id,
        inst_id="BTCUSDT", pos_side="long", source_event_id="pending", state="UNKNOWN",
        requested_qty="1", filled_qty="0", avg_px="0", reduce_only=False))
    await t.db.commit()
    exchange = OfflineExchange()
    exchange.get_order = AsyncMock(return_value={"ordId": "one", "accFillSz": "0", "avgPx": "0", "state": "canceled"})
    t.router._recover_intent_reporting = AsyncMock(wraps=t.router._recover_intent_reporting)
    assert await t.router.reconcile_pending_orders(t.db, t.account, exchange, {"BTCUSDT": t.spec})
    exchange.get_order.assert_awaited_once_with("BTCUSDT", cl_ord_id="pending-one")
    assert [call.args[1].id for call in t.router._recover_intent_reporting.await_args_list] == ["pending-one"]
    assert (await t.db.get(OrderIntent, "pending-one")).reporting_complete is True


async def test_notification_evidence_uses_indexed_scope_and_still_rejects_ambiguous_payload(trading):
    t = trading
    execution = record(t, "receipt", datetime.now(timezone.utc), notification_state="unknown", my_qty=1, my_price=100)
    t.db.add(execution)
    await t.db.flush()
    for i in range(50):
        t.db.add(NotificationOutbox(payload=json.dumps({"event_key": f"unrelated:{i}"}), status="sent"))
    receipt = NotificationOutbox(payload=json.dumps({"execution_id": execution.id, "event_key": f"execution:{execution.id}",
                                                    "channels": [], "delivered_channels": []}), status="sent")
    t.db.add(receipt)
    await t.db.commit()
    queries = []
    def capture(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().startswith("SELECT") and "notification_outbox" in statement:
            queries.append(statement)
    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert await t.router._notification_evidence(t.db, [execution])
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert execution.notification_state == "sent"
    assert queries and all("WHERE" in query and "execution_id IN" in query and "event_key IN" in query for query in queries)
    t.db.add(NotificationOutbox(payload=receipt.payload, status="sent"))
    await t.db.commit()
    assert not await t.router._notification_evidence(t.db, [execution])  # 双重回执必须人工判断。
    assert execution.notification_state == "unknown"


async def test_orphan_alert_cooldown_survives_new_router_and_session(trading):
    t = trading
    await seed_allocation(t)
    t.lead.snapshot_json = json.dumps({"meta": {"detailOk": True}, "openPositions": [],
        "openPositionsMeta": {"source": "api", "authoritative": True, "complete": True, "positionShow": True,
                              "emptyVerified": True, "asOf": NOW_MS}})
    await t.db.commit()
    exchange = OfflineExchange()
    await t.router.reconcile_orphan_positions(t.db, t.lead, t.account, t.binding, exchange, [], [t.pos],
        {"BTCUSDT": t.spec}, resilience_cfg={"auto_close_orphan_positions": "alert_only"}, notify_cfg={})
    # 新路由器和新会话只能从数据库恢复冷却，不能依赖原实例内存。
    async with get_session_factory()() as restarted_db:
        await CopyRouter(CredentialVault()).reconcile_orphan_positions(
            restarted_db, await restarted_db.get(LeadSource, t.lead.id),
            await restarted_db.get(TradingAccount, t.account.id),
            await restarted_db.get(CopyBinding, t.binding.id), exchange, [], [t.pos],
            {"BTCUSDT": t.spec}, resilience_cfg={"auto_close_orphan_positions": "alert_only"}, notify_cfg={})
    assert len((await t.db.scalars(select(ExecutionRecord).where(ExecutionRecord.status == "BLOCKED_ORPHAN_DETECTED"))).all()) == 1
    assert await t.db.scalar(select(SystemConfig.key).where(SystemConfig.key.like("orphan_alert:%"))) is not None
    assert exchange.orders == []


async def test_sqlite_timeout_matches_busy_timeout_and_writes_begin_immediate():
    engine = get_engine()
    async with engine.connect() as connection:
        assert await connection.scalar(text("PRAGMA busy_timeout")) == int(get_settings().sqlite_busy_timeout_sec * 1000)
        raw = await connection.get_raw_connection()
        driver = raw.driver_connection
        statements = []
        # sqlite trace 包含驱动发出的隐式事务语句，不依赖 SQLAlchemy 日志猜测。
        await driver.set_trace_callback(statements.append)
        try:
            # 只读请求不得提前拿写锁；真正写入时才使用 IMMEDIATE。
            await connection.scalar(text("SELECT value_json FROM system_configs WHERE key='global_kill_switch'"))
            assert not any(statement.upper().startswith("BEGIN") for statement in statements)
            await connection.execute(text("UPDATE system_configs SET value_json=value_json WHERE key='global_kill_switch'"))
            await connection.rollback()
        finally:
            await driver.set_trace_callback(None)
        assert any(statement.upper().startswith("BEGIN IMMEDIATE") for statement in statements)
