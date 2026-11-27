"""低频清理过期记录，未决资金与人工审计依赖的证据必须保留。"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import String, and_, cast, delete, exists, func, or_, select

from app.core.config import get_settings
from app.models.domain import AccountAlertState, AuditLog, BindingDelivery, ExecutionRecord, LoginAttemptGuard, NotificationOutbox, OrderIntent
from app.services.account_detail.snapshots import downsample_equity_snapshots
from app.services.engine.serialization import serialized_execution


@serialized_execution
async def cleanup_retained_records(db, *, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=get_settings().retention_days)
    # 只有已审计终态才允许清理其执行证据，未决意图的关联记录全部保留。
    related_intent = (
        OrderIntent.account_id == ExecutionRecord.account_id,
        OrderIntent.binding_id == ExecutionRecord.binding_id,
        or_(OrderIntent.exchange_order_id == ExecutionRecord.order_id,
            OrderIntent.source_event_id == ExecutionRecord.delivery_event_id,
            OrderIntent.delivery_event_id == ExecutionRecord.delivery_event_id),
    )
    terminal = func.lower(OrderIntent.state).in_(("filled", "canceled", "rejected", "expired"))
    unverified_intent = exists(select(OrderIntent.id).where(
        *related_intent, or_(OrderIntent.reporting_complete.is_(False), ~terminal),
    ))
    verified_intent = exists(select(OrderIntent.id).where(
        *related_intent, OrderIntent.reporting_complete.is_(True), terminal,
    ))
    pending_notification = exists(select(NotificationOutbox.id).where(
        NotificationOutbox.execution_id == ExecutionRecord.id,
        NotificationOutbox.status == "pending",
    ))
    terminal_notification = exists(select(NotificationOutbox.id).where(
        NotificationOutbox.execution_id == ExecutionRecord.id,
        NotificationOutbox.event_key == "execution:" + cast(ExecutionRecord.id, String),
        NotificationOutbox.status.in_(("sent", "failed")),
    ))
    # 尚未消费的来源投递或待人工复核记录不能因保留期失去交易证据。
    pending_delivery = exists(select(BindingDelivery.binding_id).where(
        BindingDelivery.binding_id == ExecutionRecord.binding_id,
        or_(BindingDelivery.source_event_id == ExecutionRecord.delivery_event_id,
            and_(ExecutionRecord.delivery_event_id.is_(None),
                 BindingDelivery.exit_progress == "REVIEW_REQUIRED")),
        or_(BindingDelivery.state != "APPLIED", BindingDelivery.exit_progress == "REVIEW_REQUIRED"),
    ))
    protected_execution = or_(
        unverified_intent, pending_delivery, ExecutionRecord.notification_state.in_(("unknown", "unsent")),
        and_(ExecutionRecord.notification_state == "pending", ~terminal_notification),
        and_(ExecutionRecord.status.in_(("UNKNOWN", "PARTIAL")), ~verified_intent),
    )
    keep_notification = exists(select(ExecutionRecord.id).where(
        ExecutionRecord.id == NotificationOutbox.execution_id, protected_execution,
    ))
    conditions = (
        # 先删除可清理的执行记录，避免先删终态回执后使旧 pending 投影失去证据。
        (ExecutionRecord, and_(ExecutionRecord.created_at < cutoff, ~protected_execution, ~pending_notification)),
        (NotificationOutbox, and_(
            NotificationOutbox.created_at < cutoff, NotificationOutbox.status.in_(("sent", "failed")),
            or_(NotificationOutbox.claimed_until.is_(None), NotificationOutbox.claimed_until <= now),
            ~keep_notification,
        )),
        (AuditLog, AuditLog.created_at < cutoff),
        (LoginAttemptGuard, and_(
            LoginAttemptGuard.updated_at < cutoff,
            or_(LoginAttemptGuard.last_fail_at.is_(None), LoginAttemptGuard.last_fail_at < cutoff),
            or_(LoginAttemptGuard.locked_until.is_(None), LoginAttemptGuard.locked_until <= now),
        )),
    )
    counts = {}
    for model, condition in conditions:
        result = await db.execute(delete(model).where(condition).execution_options(synchronize_session=False))
        counts[model.__tablename__] = result.rowcount

    # Purge only resolved alerts past 180d retention; active alerts are never deleted.
    alert_cutoff_ms = int((now - timedelta(days=180)).timestamp() * 1000)
    alert_res = await db.execute(
        delete(AccountAlertState)
        .where(
            AccountAlertState.status == "resolved",
            AccountAlertState.resolved_at_ms.is_not(None),
            AccountAlertState.resolved_at_ms < alert_cutoff_ms,
        )
        .execution_options(synchronize_session=False)
    )
    if alert_res.rowcount:
        counts[AccountAlertState.__tablename__] = alert_res.rowcount

    # Idempotently downsample raw/hour equity snapshots and prune expired raw/hour buckets only after aggregation.
    await downsample_equity_snapshots(db, now_ms=int(now.timestamp() * 1000))
    await db.commit()
    return counts
