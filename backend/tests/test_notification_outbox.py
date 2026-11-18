"""Transactional notification delivery and nonblocking sink regressions."""
import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import database
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.core.security import CredentialVault
from app.models.domain import CopyBinding, ExecutionRecord, LeadSource, NotificationOutbox, SystemConfig, TradingAccount
from app.services.engine.router import CopyRouter
from app.services.notifier.outbox import NotificationOutboxDrainer, enqueue_notification
from app.services.notifier.service import NotificationService


async def configure_sink(db, **values):
    row = await db.get(SystemConfig, "notifications")
    row.value_json = json.dumps({"webhook_enabled": True, "webhook_url": "https://example.test/notify", **values})
    await db.commit()


async def record_execution(db):
    lead = LeadSource(name="notification-lead", portfolio_id="notification-lead")
    account = TradingAccount(name="notification-account", exchange="okx",
        api_key_masked="***", encrypted_credentials="test-placeholder")
    db.add_all([lead, account])
    await db.flush()
    binding = CopyBinding(name="notification-binding", lead_id=lead.id, account_id=account.id)
    db.add(binding)
    await db.commit()
    return CopyRouter(CredentialVault()), binding, lead, account


@pytest.mark.asyncio
async def test_rollback_removes_execution_and_outbox(monkeypatch):
    dispatch = AsyncMock()
    monkeypatch.setattr(NotificationService, "dispatch", dispatch)
    async with get_session_factory()() as db:
        router, binding, lead, account = await record_execution(db)
        monkeypatch.setattr(db, "commit", AsyncMock(side_effect=RuntimeError("commit failed")))
        with pytest.raises(RuntimeError, match="commit failed"):
            await router._record_execution(db, binding, lead, account,
                "BTCUSDT", "BTC-USDT-SWAP", "BUY", "LONG", "OPEN_LONG", "EXECUTED",
                1, 100, 100, notify_cfg={"webhook_enabled": True})
        await db.rollback()
    async with get_session_factory()() as db:
        assert await db.scalar(select(func.count()).select_from(ExecutionRecord)) == 0
        assert await db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_commit_enqueues_once_before_broadcast_without_dispatch(monkeypatch):
    dispatch = AsyncMock()
    monkeypatch.setattr(NotificationService, "dispatch", dispatch)
    async with get_session_factory()() as db:
        router, binding, lead, account = await record_execution(db)

        async def broadcast(event):
            async with get_session_factory()() as reader:
                row = await reader.scalar(select(NotificationOutbox))
                assert json.loads(row.payload)["execution_id"] == event["data"]["id"]

        router.broadcast_fn = broadcast
        rec = await router._record_execution(db, binding, lead, account,
            "BTCUSDT", "BTC-USDT-SWAP", "BUY", "LONG", "OPEN_LONG", "EXECUTED",
            1, 100, 100, notify_cfg={"webhook_enabled": True, "webhook_url": "private-test-destination"})
        await db.commit()
        rows = list((await db.scalars(select(NotificationOutbox))).all())
        assert len(rows) == 1 and rows[0].status == "pending"
        assert json.loads(rows[0].payload)["execution_id"] == rec.id
        assert "private-test-destination" not in rows[0].payload
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_drainer_marks_sent_and_does_not_send_again(monkeypatch):
    dispatch = AsyncMock(return_value={"webhook": {"ok": True}})
    monkeypatch.setattr(NotificationService, "dispatch", dispatch)
    async with get_session_factory()() as db:
        await configure_sink(db)
        row = enqueue_notification(db, {"webhook_enabled": True}, "title", "body")
        await db.commit()
        row_id = row.id
    drainer = NotificationOutboxDrainer()
    assert await drainer.drain_once() == 1
    assert await drainer.drain_once() == 0
    async with get_session_factory()() as db:
        row = await db.get(NotificationOutbox, row_id)
        assert row.status == "sent" and row.attempts == 1 and row.last_error is None
    dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_sink_retries_with_backoff_then_enters_dead_letter_queue(monkeypatch):
    dispatch = AsyncMock(return_value={"webhook": {"ok": False, "error": "private-sink-error"}})
    monkeypatch.setattr(NotificationService, "dispatch", dispatch)
    async with get_session_factory()() as db:
        await configure_sink(db)
        row = enqueue_notification(db, {"webhook_enabled": True}, "title", "body")
        await db.commit()
        row_id = row.id
    drainer = NotificationOutboxDrainer(max_attempts=3)
    assert await drainer.drain_once() == 1
    assert await drainer.drain_once() == 0
    assert await drainer.drain_once(ignore_backoff=True) == 1
    assert await drainer.drain_once(ignore_backoff=True) == 1
    assert await drainer.drain_once(ignore_backoff=True) == 0
    async with get_session_factory()() as db:
        row = await db.get(NotificationOutbox, row_id)
        assert row.status == "failed" and row.attempts == 3
        assert row.last_error == "Delivery failed for channels: webhook"
    assert dispatch.await_count == 3


@pytest.mark.asyncio
async def test_successful_channels_are_not_duplicated_when_another_sink_retries(monkeypatch):
    dispatch = AsyncMock(side_effect=[
        {"webhook": {"ok": True}, "telegram": {"ok": False}},
        {"telegram": {"ok": True}},
    ])
    monkeypatch.setattr(NotificationService, "dispatch", dispatch)
    async with get_session_factory()() as db:
        await configure_sink(db, telegram_enabled=True, telegram_bot_token="test-token", telegram_chat_id="test-chat")
        enqueue_notification(db, {"webhook_enabled": True, "telegram_enabled": True}, "title", "body")
        await db.commit()
    drainer = NotificationOutboxDrainer()
    await drainer.drain_once()
    await drainer.drain_once(ignore_backoff=True)
    assert dispatch.await_args_list[0].args[0]["webhook_enabled"] is True
    assert dispatch.await_args_list[1].args[0]["webhook_enabled"] is False
    async with get_session_factory()() as db:
        assert (await db.scalar(select(NotificationOutbox))).status == "sent"


@pytest.mark.asyncio
async def test_restart_recovers_expired_claims_and_exhausted_attempts(monkeypatch):
    monkeypatch.setattr(NotificationService, "dispatch", AsyncMock(return_value={"webhook": {"ok": True}}))
    async with get_session_factory()() as db:
        await configure_sink(db)
        pending = enqueue_notification(db, {"webhook_enabled": True}, "retry", "body")
        exhausted = enqueue_notification(db, {"webhook_enabled": True}, "failed", "body")
        for row, attempts in [(pending, 1), (exhausted, 3)]:
            row.attempts = attempts
            row.claimed_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
        ids = pending.id, exhausted.id
    assert await NotificationOutboxDrainer(max_attempts=3).drain_once() == 1
    async with get_session_factory()() as db:
        pending, exhausted = [await db.get(NotificationOutbox, row_id) for row_id in ids]
        assert pending.status == "sent" and pending.attempts == 2
        assert exhausted.status == "failed" and exhausted.attempts == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("linked", [False, True])
async def test_restart_exhaustion_updates_only_proven_execution_receipt(monkeypatch, linked):
    # 耗尽租约与回执同事务落盘，损坏的关联键不得伪造执行通知终态。
    dispatch = AsyncMock()
    monkeypatch.setattr(NotificationService, "dispatch", dispatch)
    async with get_session_factory()() as db:
        router, binding, lead, account = await record_execution(db)
        execution = await router._record_execution(db, binding, lead, account,
            "BTCUSDT", "BTC-USDT-SWAP", "BUY", "LONG", "OPEN_LONG", "EXECUTED",
            1, 100, 100, notify_cfg={"webhook_enabled": True})
        row = await db.scalar(select(NotificationOutbox))
        if not linked:
            payload = json.loads(row.payload)
            payload["event_key"] = "unrelated"
            row.payload = json.dumps(payload)
        row.attempts = 3
        row.claimed_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
        execution_id, row_id = execution.id, row.id
    assert await NotificationOutboxDrainer(max_attempts=3).drain_once() == 0
    async with get_session_factory()() as db:
        assert (await db.get(NotificationOutbox, row_id)).status == "failed"
        assert (await db.get(ExecutionRecord, execution_id)).notification_state == ("failed" if linked else "pending")
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_sealed_group_sends_once_with_merged_message(monkeypatch):
    # 同一带单事件已封口的多行只发一次：带单员部分一次，各账户结果在下方合并。
    dispatch = AsyncMock(return_value={"webhook": {"ok": True}})
    monkeypatch.setattr(NotificationService, "dispatch", dispatch)
    async with get_session_factory()() as db:
        await configure_sink(db)
        router, binding, lead, account = await record_execution(db)
        executions = []
        for index in range(2):
            execution = await router._record_execution(db, binding, lead, account,
                "BTCUSDT", "BTC-USDT-SWAP", "BUY", "LONG", "OPEN_LONG", "EXECUTED",
                1, 100, 100, my_qty=1, my_price=100, my_notional=100, order_id=f"synthetic-order-{index}")
            execution.notification_state = "pending"
            executions.append(execution)
        await db.commit()
        for execution in executions:
            enqueue_notification(db, {"webhook_enabled": True}, "MirrorQuant", "single",
                event_type="EXECUTED", execution_id=execution.id, event_key=f"execution:{execution.id}",
                merge_key="event-1", lead_section="LEAD",
                follower_section=f"FOLLOWER-{execution.id}")
        await db.flush()
        rows = (await db.scalars(select(NotificationOutbox))).all()
        for row in rows:
            payload = json.loads(row.payload)
            payload["sealed"] = True
            row.payload = json.dumps(payload)
        await db.commit()
        ids = [execution.id for execution in executions]
    assert await NotificationOutboxDrainer().drain_once() == 2
    dispatch.assert_awaited_once()
    args = dispatch.await_args.args
    assert args[1] == "MirrorQuant"
    assert args[2] == f"LEAD\n\nFOLLOWER-{ids[0]}\n\nFOLLOWER-{ids[1]}"
    async with get_session_factory()() as db:
        states = [(await db.get(ExecutionRecord, execution_id)).notification_state for execution_id in ids]
        assert states == ["sent", "sent"]


@pytest.mark.asyncio
async def test_unsealed_fresh_rows_wait_for_the_batch_to_finish(monkeypatch):
    dispatch = AsyncMock(return_value={"webhook": {"ok": True}})
    monkeypatch.setattr(NotificationService, "dispatch", dispatch)
    async with get_session_factory()() as db:
        await configure_sink(db)
        enqueue_notification(db, {"webhook_enabled": True}, "MirrorQuant", "single",
            event_type="EXECUTED", execution_id=1, event_key="execution:1",
            merge_key="event-1", lead_section="LEAD", follower_section="FOLLOWER")
        await db.commit()
    assert await NotificationOutboxDrainer().drain_once() == 0
    dispatch.assert_not_awaited()
    async with get_session_factory()() as db:
        row = await db.scalar(select(NotificationOutbox))
        assert row.status == "pending" and row.attempts == 0 and row.claimed_until is None


@pytest.mark.asyncio
async def test_aged_unsealed_rows_send_grouped_after_crash(monkeypatch):
    # 批次中途死亡没封口：超过宽限期的行照常按事件分组发出，不丢通知。
    dispatch = AsyncMock(return_value={"webhook": {"ok": True}})
    monkeypatch.setattr(NotificationService, "dispatch", dispatch)
    async with get_session_factory()() as db:
        await configure_sink(db)
        for index in range(2):
            enqueue_notification(db, {"webhook_enabled": True}, "MirrorQuant", "single",
                event_type="EXECUTED", execution_id=index + 1, event_key=f"execution:{index + 1}",
                merge_key="event-1", lead_section="LEAD", follower_section=f"FOLLOWER-{index + 1}")
        await db.flush()
        rows = (await db.scalars(select(NotificationOutbox))).all()
        for row in rows:
            row.created_at = datetime.now(timezone.utc) - timedelta(seconds=300)
        await db.commit()
    assert await NotificationOutboxDrainer().drain_once() == 2
    dispatch.assert_awaited_once()
    assert dispatch.await_args.args[2] == "LEAD\n\nFOLLOWER-1\n\nFOLLOWER-2"


@pytest.mark.asyncio
async def test_workers_cannot_claim_the_same_pending_row(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'outbox.sqlite'}")
    monkeypatch.setattr(database, "_engine", engine)
    monkeypatch.setattr(database, "_session_factory", async_sessionmaker(engine, expire_on_commit=False))
    try:
        await database.init_db()
        async with get_session_factory()() as db:
            await configure_sink(db)
            enqueue_notification(db, {"webhook_enabled": True}, "title", "body")
            await db.commit()
        entered, release = asyncio.Event(), asyncio.Event()

        async def dispatch(*args, **kwargs):
            entered.set()
            await release.wait()
            return {"webhook": {"ok": True}}

        sink = AsyncMock(side_effect=dispatch)
        monkeypatch.setattr(NotificationService, "dispatch", sink)
        first = asyncio.create_task(NotificationOutboxDrainer().drain_once())
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert await NotificationOutboxDrainer().drain_once(ignore_backoff=True) == 0
        release.set()
        assert await first == 1
        sink.assert_awaited_once()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_graceful_shutdown_preserves_backoff_and_pending_work(monkeypatch):
    dispatch = AsyncMock(return_value={"webhook": {"ok": False}})
    monkeypatch.setattr(NotificationService, "dispatch", dispatch)
    async with get_session_factory()() as db:
        await configure_sink(db)
        enqueue_notification(db, {"webhook_enabled": True}, "title", "body")
        await db.commit()
    drainer = NotificationOutboxDrainer(max_attempts=2)
    # 先制造一次失败，停机不得跳过退避强行耗尽剩余重试。
    await drainer.drain_once()
    await drainer.stop()
    assert drainer._task is None and dispatch.await_count == 1
    async with get_session_factory()() as db:
        row = await db.scalar(select(NotificationOutbox))
        assert row.status == "pending" and row.attempts == 1 and row.next_attempt_at is not None
    assert await NotificationOutboxDrainer(max_attempts=2).drain_once() == 0


@pytest.mark.asyncio
async def test_security_alert_joins_login_transaction_and_rolls_back(monkeypatch):
    from app.api.auth import _emit_security_alert

    dispatch = AsyncMock()
    monkeypatch.setattr(NotificationService, "dispatch", dispatch)
    async with get_session_factory()() as db:
        await configure_sink(db)
        await _emit_security_alert(db, "192.0.2.1", "test-user", 15, 7200)
        assert await db.scalar(select(func.count()).select_from(NotificationOutbox)) == 1
        await db.rollback()
        assert await db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_sink_exception_details_never_escape_dispatch_or_dead_letter_queue(tmp_path, monkeypatch):
    def failing_append(*args):
        raise OSError("private-credential-must-not-escape")

    monkeypatch.setattr(NotificationService, "_append_outbox", failing_append)
    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))  # 合法路径才能到达模拟磁盘异常。
    config = {"outbox_enabled": True, "outbox_path": str(tmp_path / "events.jsonl")}
    result = await NotificationService.dispatch(config, "title", "body")
    assert result["outbox"] == {"ok": False, "error": "OSError"}
    monkeypatch.setattr(NotificationService, "dispatch", AsyncMock(side_effect=RuntimeError("private-token")))
    async with get_session_factory()() as db:
        await configure_sink(db)
        enqueue_notification(db, {"webhook_enabled": True}, "title", "body")
        await db.commit()
    await NotificationOutboxDrainer(max_attempts=1).drain_once()
    async with get_session_factory()() as db:
        row = await db.scalar(select(NotificationOutbox))
        assert row.status == "failed" and row.last_error == "Delivery failed: RuntimeError"


@pytest.mark.asyncio
async def test_enqueue_redacts_embedded_proxy_userinfo():
    async with get_session_factory()() as db:
        row = enqueue_notification(db, {}, "title", "Failed http://test-user:test-password@proxy.example:8080")
        payload = json.loads(row.payload)
        assert payload["message"] == "Failed http://***:***@proxy.example:8080"


@pytest.mark.asyncio
async def test_shutdown_database_failure_retains_work_without_masking_shutdown(monkeypatch):
    drainer = NotificationOutboxDrainer()
    drain = AsyncMock(side_effect=RuntimeError("database unavailable"))
    monkeypatch.setattr(drainer, "drain_once", drain)
    await drainer.stop()
    drain.assert_not_awaited()  # 没有在途任务时，停机不再访问数据库领取新任务。


@pytest.mark.asyncio
async def test_jsonl_sink_runs_file_io_off_event_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
    loop_thread = threading.get_ident()
    worker_threads = []
    original = NotificationService._append_outbox

    def append(path, record):
        worker_threads.append(threading.get_ident())
        original(path, record)

    monkeypatch.setattr(NotificationService, "_append_outbox", append)
    path = tmp_path / "events.jsonl"
    result = await NotificationService.dispatch({"outbox_enabled": True, "outbox_path": str(path)}, "title", "body")
    assert result["outbox"]["ok"] is True
    assert worker_threads and worker_threads[0] != loop_thread
    assert json.loads(path.read_text())["text"] == "body"


@pytest.mark.asyncio
async def test_expired_pending_rows_are_failed_without_delivery(monkeypatch):
    dispatch = AsyncMock(return_value={"webhook": {"ok": True}})
    monkeypatch.setattr(NotificationService, "dispatch", dispatch)
    async with get_session_factory()() as db:
        await configure_sink(db)
        row = enqueue_notification(db, {"webhook_enabled": True}, "stale", "body")
        row.created_at = datetime.now(timezone.utc) - timedelta(seconds=3700)
        await db.commit()
        row_id = row.id
    assert await NotificationOutboxDrainer().drain_once() == 0
    async with get_session_factory()() as db:
        row = await db.get(NotificationOutbox, row_id)
        assert row.status == "failed" and row.last_error == "Notification expired before delivery"
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_execution_notification_updates_receipt(monkeypatch):
    dispatch = AsyncMock()
    monkeypatch.setattr(NotificationService, "dispatch", dispatch)
    async with get_session_factory()() as db:
        router, binding, lead, account = await record_execution(db)
        execution = await router._record_execution(db, binding, lead, account,
            "BTCUSDT", "BTC-USDT-SWAP", "BUY", "LONG", "OPEN_LONG", "EXECUTED",
            1, 100, 100, notify_cfg={"webhook_enabled": True})
        row = await db.scalar(select(NotificationOutbox))
        row.created_at = datetime.now(timezone.utc) - timedelta(seconds=3700)
        await db.commit()
        execution_id, row_id = execution.id, row.id
    assert await NotificationOutboxDrainer().drain_once() == 0
    async with get_session_factory()() as db:
        assert (await db.get(NotificationOutbox, row_id)).status == "failed"
        assert (await db.get(ExecutionRecord, execution_id)).notification_state == "failed"
    dispatch.assert_not_awaited()
