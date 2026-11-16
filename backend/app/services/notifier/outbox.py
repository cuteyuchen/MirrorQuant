"""Transactional notifications with bounded, at-least-once delivery.

Failed rows are retained as the dead-letter queue (status='failed'). Payloads
contain no sink credentials: destinations are resolved from current notification
settings at delivery time. Successfully delivered channels are not retried.
A crash after delivery but before acknowledgement can still duplicate a message.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy import or_, select, update

from app.core.config import get_settings
from app.core.database import DEFAULT_SYSTEM_CONFIG, get_session_factory
from app.core.secret_mask import redact_proxy_userinfo
from app.models.domain import ExecutionRecord, NotificationOutbox, SystemConfig
from app.services.notifier.service import NotificationService

logger = logging.getLogger("mirrorquant.notification_outbox")
CHANNELS = ("outbox", "webhook", "telegram", "wecom", "dingtalk")
DELIVERY_TIMEOUT_SEC = 45.0
CLAIM_LEASE_SEC = 60.0


def enqueue_notification(db, config: Dict[str, Any], title: str, message: str,
                         event_type: str = "info", execution_id: Optional[int] = None,
                         event_key: Optional[str] = None) -> NotificationOutbox:
    """Join the caller's transaction; never commit or start external I/O here."""
    row = NotificationOutbox(payload=json.dumps({
        "title": redact_proxy_userinfo(title),
        "message": redact_proxy_userinfo(message),
        "event_type": event_type,
        "event_key": redact_proxy_userinfo(event_key) if event_key is not None else None,
        "execution_id": execution_id,
        "channels": [channel for channel in CHANNELS if config.get(f"{channel}_enabled")],
        "delivered_channels": [],
    }, ensure_ascii=False), status="pending", attempts=0)
    db.add(row)
    return row


async def _acknowledge_execution(db, payload, status: str) -> None:
    # 只同步有完整关联证据的执行投影，损坏或无关通知不能覆盖回执。
    if (isinstance(payload, dict) and type(payload.get("execution_id")) is int
            and payload.get("event_key") == f"execution:{payload['execution_id']}"):
        await db.execute(update(ExecutionRecord).where(
            ExecutionRecord.id == payload["execution_id"],
            ExecutionRecord.notification_state == "pending",
        ).values(notification_state=status))


class NotificationOutboxDrainer:
    def __init__(self, max_attempts: Optional[int] = None, poll_interval: Optional[float] = None):
        settings = get_settings()
        self.max_attempts = max_attempts if max_attempts is not None else settings.notification_outbox_max_attempts
        self.poll_interval = poll_interval if poll_interval is not None else settings.notification_outbox_poll_sec
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = asyncio.Event()
        self._lock = asyncio.Lock()
        self._task = asyncio.create_task(self._run(), name="mirrorquant-notification-outbox")

    async def stop(self) -> None:
        """停止领取新任务，保留退避队列供下一次启动恢复。"""
        self._stopping.set()
        if self._task is not None:
            try:
                # 超时取消仍保留有租约的持久化任务，不提前耗尽重试次数。
                await asyncio.wait_for(self._task, timeout=DELIVERY_TIMEOUT_SEC + 5)
            except asyncio.TimeoutError:
                logger.warning("Shutdown deferred claimed notifications until lease expiry")
            finally:
                self._task = None

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.drain_once()
            except Exception as exc:
                logger.error("Notification outbox drain failed: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass

    async def drain_once(self, *, ignore_backoff: bool = False) -> int:
        async with self._lock:
            now = datetime.now(timezone.utc)
            available = or_(NotificationOutbox.claimed_until.is_(None), NotificationOutbox.claimed_until <= now)
            conditions = [NotificationOutbox.status == "pending", available]
            if not ignore_backoff:
                conditions.append(or_(NotificationOutbox.next_attempt_at.is_(None), NotificationOutbox.next_attempt_at <= now))
            async with get_session_factory()() as db:
                exhausted = await db.execute(update(NotificationOutbox).where(
                    NotificationOutbox.status == "pending", available,
                    NotificationOutbox.attempts >= self.max_attempts,
                ).values(status="failed", last_error="Delivery retry limit exhausted", claimed_until=None)
                    .returning(NotificationOutbox.payload))
                # 重启后已耗尽的租约也必须留下终态回执，不再次调用通知渠道。
                for payload_text in exhausted.scalars().all():
                    try:
                        payload = json.loads(payload_text)
                    except (ValueError, TypeError):
                        continue
                    await _acknowledge_execution(db, payload, "failed")
                ids = list((await db.scalars(select(NotificationOutbox.id).where(
                    *conditions, NotificationOutbox.attempts < self.max_attempts,
                ).order_by(NotificationOutbox.id).limit(100))).all())
                await db.commit()

            processed = 0
            for row_id in ids:
                if self._stopping.is_set():
                    break  # 停机只等待当前一条通知，不遍历整批。
                async with get_session_factory()() as db:
                    # Atomic claims serialize multiple workers without holding a
                    # database write transaction open during sink I/O.
                    claim = await db.execute(update(NotificationOutbox).where(
                        NotificationOutbox.id == row_id, *conditions,
                        NotificationOutbox.attempts < self.max_attempts,
                    ).values(attempts=NotificationOutbox.attempts + 1,
                             claimed_until=datetime.now(timezone.utc) + timedelta(seconds=CLAIM_LEASE_SEC)))
                    if not claim.rowcount:
                        await db.rollback()
                        continue
                    row = await db.get(NotificationOutbox, row_id)
                    payload_text, attempt = row.payload, row.attempts
                    config_row = await db.get(SystemConfig, "notifications")
                    config_text = config_row.value_json if config_row else "{}"
                    await db.commit()
                processed += 1
                error = None
                payload = None
                try:
                    payload = json.loads(payload_text)
                    config = {**DEFAULT_SYSTEM_CONFIG["notifications"], **json.loads(config_text)}
                    remaining = set(payload["channels"]) - set(payload.get("delivered_channels", []))
                    for channel in CHANNELS:
                        config[f"{channel}_enabled"] = channel in remaining and bool(config.get(f"{channel}_enabled"))
                    results = await asyncio.wait_for(NotificationService.dispatch(
                        config, payload["title"], payload["message"], event_type=payload["event_type"],
                    ), timeout=DELIVERY_TIMEOUT_SEC) if remaining else {}
                    delivered = {channel for channel in remaining if results.get(channel, {}).get("ok") is True}
                    payload["delivered_channels"] = sorted(set(payload.get("delivered_channels", [])) | delivered)
                    missing = remaining - delivered
                    if missing:
                        # Sink exceptions can embed tokens or credential-bearing
                        # URLs. Persist only the channel names, never raw errors.
                        error = "Delivery failed for channels: " + ", ".join(sorted(missing))
                except Exception as exc:
                    error = "Delivery failed: " + type(exc).__name__
                async with get_session_factory()() as db:
                    final_status = "sent" if error is None else ("failed" if attempt >= self.max_attempts else "pending")
                    acknowledged = await db.execute(update(NotificationOutbox).where(
                        NotificationOutbox.id == row_id, NotificationOutbox.status == "pending",
                        NotificationOutbox.attempts == attempt,
                    ).values(
                        status=final_status,
                        last_error=error, claimed_until=None,
                        payload=json.dumps(payload, ensure_ascii=False) if payload is not None else payload_text,
                        # 10/20/40/80/160/300 秒退避，保留成功渠道，不重复通知。
                        next_attempt_at=datetime.now(timezone.utc) + timedelta(seconds=min(300, 10 * 2 ** (attempt - 1))) if error else None,
                    ))
                    # 通知回执和执行投影一起落盘，防止终态记录一直被当作 pending。
                    if acknowledged.rowcount:
                        await _acknowledge_execution(db, payload, final_status)
                    await db.commit()
            return processed


notification_outbox = NotificationOutboxDrainer()
