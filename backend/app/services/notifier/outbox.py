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
from app.models.domain import NotificationOutbox, SystemConfig
from app.services.notifier.service import NotificationService

logger = logging.getLogger("mirrorquant.notification_outbox")
CHANNELS = ("outbox", "webhook", "telegram", "wecom", "dingtalk")
DELIVERY_TIMEOUT_SEC = 45.0
CLAIM_LEASE_SEC = 60.0


def enqueue_notification(db, config: Dict[str, Any], title: str, message: str,
                         event_type: str = "info", execution_id: Optional[int] = None) -> NotificationOutbox:
    """Join the caller's transaction; never commit or start external I/O here."""
    row = NotificationOutbox(payload=json.dumps({
        "title": redact_proxy_userinfo(title),
        "message": redact_proxy_userinfo(message),
        "event_type": event_type,
        "execution_id": execution_id,
        "channels": [channel for channel in CHANNELS if config.get(f"{channel}_enabled")],
        "delivered_channels": [],
    }, ensure_ascii=False), status="pending", attempts=0)
    db.add(row)
    return row


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
        """Finish in-flight delivery, then drain pending rows up to their retry limit."""
        self._stopping.set()
        if self._task is not None:
            await self._task
            self._task = None
        # Backoff is unnecessary on shutdown; active claims in other workers are
        # still respected. Undeliverable rows finish in the durable failed queue.
        try:
            while await self.drain_once(ignore_backoff=True):
                pass
        except Exception as exc:
            # A database outage cannot be repaired during shutdown. Rows remain
            # durable for the next worker; preserve the original shutdown result.
            logger.error("Shutdown notification drain deferred: %s", type(exc).__name__)

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
                await db.execute(update(NotificationOutbox).where(
                    NotificationOutbox.status == "pending", available,
                    NotificationOutbox.attempts >= self.max_attempts,
                ).values(status="failed", last_error="Delivery retry limit exhausted", claimed_until=None))
                ids = list((await db.scalars(select(NotificationOutbox.id).where(
                    *conditions, NotificationOutbox.attempts < self.max_attempts,
                ).order_by(NotificationOutbox.id).limit(100))).all())
                await db.commit()

            processed = 0
            for row_id in ids:
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
                    await db.execute(update(NotificationOutbox).where(
                        NotificationOutbox.id == row_id, NotificationOutbox.status == "pending",
                        NotificationOutbox.attempts == attempt,
                    ).values(
                        status="sent" if error is None else ("failed" if attempt >= self.max_attempts else "pending"),
                        last_error=error, claimed_until=None,
                        payload=json.dumps(payload, ensure_ascii=False) if payload is not None else payload_text,
                        next_attempt_at=datetime.now(timezone.utc) + timedelta(seconds=min(60, 2 ** attempt)) if error else None,
                    ))
                    await db.commit()
            return processed


notification_outbox = NotificationOutboxDrainer()
