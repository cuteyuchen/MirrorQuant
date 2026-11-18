"""Transactional notifications with bounded, at-least-once delivery.

Failed rows are retained as the dead-letter queue (status='failed'). Payloads
contain no sink credentials: destinations are resolved from current notification
settings at delivery time. Successfully delivered channels are not retried.
A crash after delivery but before acknowledgement can still duplicate a message.
Rows older than the configured TTL are expired without delivery, so a restart
never flushes stale notifications that accumulated while delivery was down.
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
from app.services.notifier.trade import merged_trade_message

logger = logging.getLogger("mirrorquant.notification_outbox")
CHANNELS = ("outbox", "webhook", "telegram", "wecom", "dingtalk")
DELIVERY_TIMEOUT_SEC = 45.0
CLAIM_LEASE_SEC = 60.0
# 批次封口后 drainer 才按事件分组发送；未封口的行超过此时限视为批次已死，照常分组发出。
MERGE_SEAL_GRACE_SEC = 120.0
MERGE_TOPUP_LIMIT = 200


def enqueue_notification(db, config: Dict[str, Any], title: str, message: str,
                         event_type: str = "info", execution_id: Optional[int] = None,
                         event_key: Optional[str] = None, merge_key: Optional[str] = None,
                         lead_section: Optional[str] = None,
                         follower_section: Optional[str] = None) -> NotificationOutbox:
    """Join the caller's transaction; never commit or start external I/O here.

    A mergeable row carries its lead-event key plus the rendered lead and
    follower sections; the drainer groups sealed rows by merge_key and sends
    one message per lead event. The row itself stays one durable record per
    execution outcome, so fill reporting atomicity is unchanged.
    """
    payload = {
        "title": redact_proxy_userinfo(title),
        "message": redact_proxy_userinfo(message),
        "event_type": event_type,
        "event_key": redact_proxy_userinfo(event_key) if event_key is not None else None,
        "execution_id": execution_id,
        "channels": [channel for channel in CHANNELS if config.get(f"{channel}_enabled")],
        "delivered_channels": [],
    }
    if merge_key is not None:
        payload["merge_key"] = merge_key
        payload["lead_section"] = lead_section
        payload["follower_section"] = follower_section
        payload["sealed"] = False
    row = NotificationOutbox(payload=json.dumps(payload, ensure_ascii=False), status="pending", attempts=0)
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


def _payload_sections(payload: Dict[str, Any]) -> Optional[tuple]:
    lead, follower = payload.get("lead_section"), payload.get("follower_section")
    if isinstance(lead, str) and lead and isinstance(follower, str) and follower:
        return lead, follower
    return None


def _row_is_mergeable(payload: Dict[str, Any]) -> bool:
    return isinstance(payload.get("merge_key"), str) and bool(payload.get("merge_key"))


def _row_created_utc(created: datetime) -> datetime:
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)  # 数据库存的是不带时区的 UTC 时间
    return created


class NotificationOutboxDrainer:
    def __init__(self, max_attempts: Optional[int] = None, poll_interval: Optional[float] = None,
                 ttl_sec: Optional[float] = None):
        settings = get_settings()
        self.max_attempts = max_attempts if max_attempts is not None else settings.notification_outbox_max_attempts
        self.poll_interval = poll_interval if poll_interval is not None else settings.notification_outbox_poll_sec
        self.ttl_sec = ttl_sec if ttl_sec is not None else settings.notification_ttl_sec
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

    async def _claim_row(self, row_id, conditions):
        async with get_session_factory()() as db:
            claim = await db.execute(update(NotificationOutbox).where(
                NotificationOutbox.id == row_id, *conditions,
                NotificationOutbox.attempts < self.max_attempts,
            ).values(attempts=NotificationOutbox.attempts + 1,
                     claimed_until=datetime.now(timezone.utc) + timedelta(seconds=CLAIM_LEASE_SEC)))
            if not claim.rowcount:
                await db.rollback()
                return None
            row = await db.get(NotificationOutbox, row_id)
            entry = {"row_id": row_id, "attempt": row.attempts, "payload_text": row.payload,
                     "created_at": row.created_at}
            config_row = await db.get(SystemConfig, "notifications")
            entry["config_text"] = config_row.value_json if config_row else "{}"
            await db.commit()
            return entry

    async def _release_claim(self, entry) -> None:
        """Give back an unsealed row untouched: no attempt consumed, no error recorded."""
        async with get_session_factory()() as db:
            await db.execute(update(NotificationOutbox).where(
                NotificationOutbox.id == entry["row_id"], NotificationOutbox.status == "pending",
                NotificationOutbox.attempts == entry["attempt"],
            ).values(attempts=entry["attempt"] - 1, claimed_until=None))
            await db.commit()

    async def _finalize_row(self, entry, error, payload) -> None:
        async with get_session_factory()() as db:
            final_status = "sent" if error is None else ("failed" if entry["attempt"] >= self.max_attempts else "pending")
            acknowledged = await db.execute(update(NotificationOutbox).where(
                NotificationOutbox.id == entry["row_id"], NotificationOutbox.status == "pending",
                NotificationOutbox.attempts == entry["attempt"],
            ).values(
                status=final_status,
                last_error=error, claimed_until=None,
                payload=json.dumps(payload, ensure_ascii=False) if payload is not None else entry["payload_text"],
                # 10/20/40/80/160/300 秒退避，保留成功渠道，不重复通知。
                next_attempt_at=datetime.now(timezone.utc) + timedelta(seconds=min(300, 10 * 2 ** (entry["attempt"] - 1))) if error else None,
            ))
            # 通知回执和执行投影一起落盘，防止终态记录一直被当作 pending。
            if acknowledged.rowcount:
                await _acknowledge_execution(db, payload, final_status)
            await db.commit()

    async def _deliver(self, entries, message: str, title: str, event_type: str) -> int:
        """Send one dispatch shared by entries (a single row, or one merged group)."""
        first = entries[0]
        try:
            parsed = []
            remaining_union = set()
            for entry in entries:
                payload = entry.get("payload")
                if payload is None:
                    raise ValueError("Invalid notification payload")
                parsed.append(payload)
                remaining_union |= set(payload["channels"]) - set(payload.get("delivered_channels", []))
            config = {**DEFAULT_SYSTEM_CONFIG["notifications"], **json.loads(first["config_text"])}
            for channel in CHANNELS:
                config[f"{channel}_enabled"] = channel in remaining_union and bool(config.get(f"{channel}_enabled"))
            results = await asyncio.wait_for(NotificationService.dispatch(
                config, title, message, event_type=event_type,
            ), timeout=DELIVERY_TIMEOUT_SEC) if remaining_union else {}
            delivered = {channel for channel in remaining_union if results.get(channel, {}).get("ok") is True}
            for entry, payload in zip(entries, parsed):
                remaining = set(payload["channels"]) - set(payload.get("delivered_channels", []))
                payload["delivered_channels"] = sorted(set(payload.get("delivered_channels", [])) | (delivered & remaining))
                missing = remaining - delivered
                error = None
                if missing:
                    # Sink exceptions can embed tokens or credential-bearing
                    # URLs. Persist only the channel names, never raw errors.
                    error = "Delivery failed for channels: " + ", ".join(sorted(missing))
                await self._finalize_row(entry, error, payload)
        except Exception as exc:
            error = "Delivery failed: " + type(exc).__name__
            for entry in entries:
                await self._finalize_row(entry, error, entry.get("payload"))
        return len(entries)

    def _group_message(self, entries) -> str:
        sections = [_payload_sections(entry["payload"]) for entry in entries]
        return merged_trade_message(sections[0][0], [pair[1] for pair in sections])

    async def drain_once(self, *, ignore_backoff: bool = False) -> int:
        async with self._lock:
            now = datetime.now(timezone.utc)
            available = or_(NotificationOutbox.claimed_until.is_(None), NotificationOutbox.claimed_until <= now)
            conditions = [NotificationOutbox.status == "pending", available]
            if not ignore_backoff:
                conditions.append(or_(NotificationOutbox.next_attempt_at.is_(None), NotificationOutbox.next_attempt_at <= now))
            async with get_session_factory()() as db:
                # 过期消息直接作废：先于耗尽判断，让旧行的终态原因说清是过期而非投递失败。
                expired = await db.execute(update(NotificationOutbox).where(
                    NotificationOutbox.status == "pending", available,
                    NotificationOutbox.created_at < now - timedelta(seconds=self.ttl_sec),
                ).values(status="failed", last_error="Notification expired before delivery", claimed_until=None)
                    .returning(NotificationOutbox.payload))
                for payload_text in expired.scalars().all():
                    try:
                        payload = json.loads(payload_text)
                    except (ValueError, TypeError):
                        continue
                    await _acknowledge_execution(db, payload, "failed")
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

            claimed = []
            for row_id in ids:
                if self._stopping.is_set():
                    break  # 停机只等待当前一条通知，不遍历整批。
                entry = await self._claim_row(row_id, conditions)
                if entry is not None:
                    claimed.append(entry)

            # 同一事件的分组若被领取窗口切开，把同键的其余行也领进来，避免拆成两条消息。
            merge_keys = set()
            for entry in claimed:
                try:
                    payload = json.loads(entry["payload_text"])
                except (ValueError, TypeError):
                    continue
                if _row_is_mergeable(payload):
                    merge_keys.add(payload["merge_key"])
            if merge_keys:
                claimed_ids = {entry["row_id"] for entry in claimed}
                async with get_session_factory()() as db:
                    rows = (await db.scalars(select(NotificationOutbox).where(
                        *conditions, NotificationOutbox.attempts < self.max_attempts,
                        NotificationOutbox.id.not_in(claimed_ids),
                    ).order_by(NotificationOutbox.id).limit(MERGE_TOPUP_LIMIT))).all()
                    candidates = [(row.id, row.payload) for row in rows]
                    await db.commit()
                for candidate_id, candidate_payload in candidates:
                    try:
                        payload = json.loads(candidate_payload)
                    except (ValueError, TypeError):
                        continue
                    if payload.get("merge_key") not in merge_keys:
                        continue
                    entry = await self._claim_row(candidate_id, conditions)
                    if entry is not None:
                        claimed.append(entry)
                claimed.sort(key=lambda entry: entry["row_id"])

            now = datetime.now(timezone.utc)
            singles = []
            groups = {}
            for entry in claimed:
                try:
                    payload = json.loads(entry["payload_text"])
                except (ValueError, TypeError):
                    singles.append(entry)
                    continue
                entry["payload"] = payload
                if not _row_is_mergeable(payload):
                    singles.append(entry)
                    continue
                sealed = payload.get("sealed") is True
                aged = now - _row_created_utc(entry["created_at"]) >= timedelta(seconds=MERGE_SEAL_GRACE_SEC)
                if sealed or aged:
                    groups.setdefault(payload["merge_key"], []).append(entry)
                else:
                    # 批次还没处理完：不发、不计次，下次轮询再看。
                    await self._release_claim(entry)
            for key, members in list(groups.items()):
                if any(_payload_sections(member["payload"]) is None for member in members):
                    # 分段缺失无法拼合，退回单条发送，不丢通知。
                    singles.extend(members)
                    del groups[key]

            processed = 0
            for entry in singles:
                if self._stopping.is_set():
                    break
                payload = entry.get("payload")
                if payload is None:
                    processed += await self._deliver([entry], "", "", "info")
                else:
                    processed += await self._deliver(
                        [entry], payload["message"], payload["title"], payload["event_type"])
            for members in groups.values():
                if self._stopping.is_set():
                    break
                first_payload = members[0]["payload"]
                processed += await self._deliver(
                    members, self._group_message(members), first_payload["title"], first_payload["event_type"])
            return processed


notification_outbox = NotificationOutboxDrainer()
