from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, record_audit
from app.core.database import DEFAULT_SYSTEM_CONFIG, get_db
from app.core.secret_mask import NOTIFICATION_SECRET_FIELDS, mask_sensitive_payload, merge_secrets
from app.models.domain import CopyBinding, LeadSource, SystemConfig, User, UserLeadSubscription, UserPreference
from app.schemas.api import (
    LeadSubscriptionItem,
    LeadSubscriptionsUpdate,
    UserPreferencesResponse,
    UserPreferencesUpdate,
)
from app.services.notifier.service import NotificationService
from app.services.notifier.trade import resolve_display_unit

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


async def _get_user_pref(db: AsyncSession, user_id: int) -> Optional[UserPreference]:
    return await db.get(UserPreference, user_id)


async def _get_system_notifications(db: AsyncSession) -> dict:
    row = await db.scalar(select(SystemConfig).where(SystemConfig.key == "notifications"))
    default = DEFAULT_SYSTEM_CONFIG.get("notifications", {})
    if row and row.value_json:
        try:
            val = json.loads(row.value_json)
            return {**default, **val}
        except Exception:
            pass
    return default


@router.get("/preferences")
async def get_user_preferences(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    pref = await _get_user_pref(db, current_user.id)
    has_custom = False
    notify_dict = None
    if pref and pref.notifications_json:
        try:
            notify_dict = json.loads(pref.notifications_json)
            has_custom = True
        except Exception:
            pass
    if notify_dict is None:
        notify_dict = await _get_system_notifications(db)

    masked_notify = mask_sensitive_payload(notify_dict)

    risk_dict = None
    if pref and pref.risk_defaults_json:
        try:
            risk_dict = json.loads(pref.risk_defaults_json)
        except Exception:
            pass

    return {
        "notifications": masked_notify,
        "riskDefaults": risk_dict,
        "hasCustomNotifications": has_custom,
        "displayUnit": resolve_display_unit(notify_dict.get("display_unit")),
    }


@router.put("/preferences")
async def update_user_preferences(
    payload: UserPreferencesUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    pref = await _get_user_pref(db, current_user.id)
    if not pref:
        pref = UserPreference(user_id=current_user.id)
        db.add(pref)
        await db.flush()

    if payload.notifications is not None:
        incoming = payload.notifications.model_dump(exclude_unset=True)
        existing = {}
        if pref.notifications_json:
            try:
                existing = json.loads(pref.notifications_json)
            except Exception:
                pass
        else:
            existing = await _get_system_notifications(db)
        merged = merge_secrets(existing, incoming, NOTIFICATION_SECRET_FIELDS)
        pref.notifications_json = json.dumps(merged)

    if payload.risk_defaults is not None:
        pref.risk_defaults_json = json.dumps(payload.risk_defaults.model_dump(exclude_unset=True))

    await record_audit(
        db, "UPDATE_USER_PREFERENCES", "user_preference", str(current_user.id),
        "Updated notification / risk preferences", current_user, request
    )
    await db.commit()
    await db.refresh(pref)

    notify_dict = json.loads(pref.notifications_json) if pref.notifications_json else await _get_system_notifications(db)
    risk_dict = json.loads(pref.risk_defaults_json) if pref.risk_defaults_json else None
    return {
        "notifications": mask_sensitive_payload(notify_dict),
        "riskDefaults": risk_dict,
        "hasCustomNotifications": bool(pref.notifications_json),
        "displayUnit": resolve_display_unit(notify_dict.get("display_unit")),
    }


@router.get("/subscriptions")
async def get_user_subscriptions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    leads = list((await db.scalars(select(LeadSource).order_by(LeadSource.id))).all())
    subs = list((await db.scalars(
        select(UserLeadSubscription).where(UserLeadSubscription.user_id == current_user.id)
    )).all())
    sub_map = {s.lead_id: s for s in subs}

    user_bindings = list((await db.scalars(
        select(CopyBinding).where(
            CopyBinding.is_enabled.is_(True),
            (CopyBinding.owner_user_id == current_user.id) if current_user.role != "admin" else True
        )
    )).all())
    binding_counts: Dict[int, int] = {}
    for b in user_bindings:
        binding_counts[b.lead_id] = binding_counts.get(b.lead_id, 0) + 1

    items = []
    for lead in leads:
        sub = sub_map.get(lead.id)
        count = binding_counts.get(lead.id, 0)
        channels = None
        if sub and sub.channels_json:
            try:
                channels = json.loads(sub.channels_json)
            except Exception:
                pass
        items.append({
            "id": sub.id if sub else None,
            "leadId": lead.id,
            "leadName": lead.name,
            "leadPlatform": lead.platform,
            "isEnabled": sub.is_enabled if sub else False,
            "channels": channels,
            "hasBinding": count > 0,
            "bindingCount": count,
        })
    return {"items": items}


@router.put("/subscriptions")
async def update_user_subscriptions(
    payload: LeadSubscriptionsUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    existing_subs = list((await db.scalars(
        select(UserLeadSubscription).where(UserLeadSubscription.user_id == current_user.id)
    )).all())
    sub_map = {s.lead_id: s for s in existing_subs}

    for item in payload.subscriptions:
        lead = await db.get(LeadSource, item.lead_id)
        if not lead:
            continue
        sub = sub_map.get(item.lead_id)
        channels_str = json.dumps(item.channels) if item.channels is not None else None
        if sub:
            sub.is_enabled = item.is_enabled
            if item.channels is not None:
                sub.channels_json = channels_str
        else:
            sub = UserLeadSubscription(
                user_id=current_user.id,
                lead_id=item.lead_id,
                is_enabled=item.is_enabled,
                channels_json=channels_str,
            )
            db.add(sub)

    await record_audit(
        db, "UPDATE_LEAD_SUBSCRIPTIONS", "user_lead_subscription", str(current_user.id),
        f"Updated subscriptions for {len(payload.subscriptions)} leads", current_user, request
    )
    await db.commit()
    return await get_user_subscriptions(db, current_user)


@router.post("/test")
async def test_user_notification(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    pref = await _get_user_pref(db, current_user.id)
    cfg = None
    if pref and pref.notifications_json:
        try:
            cfg = json.loads(pref.notifications_json)
        except Exception:
            pass
    if not cfg:
        cfg = await _get_system_notifications(db)

    unit = resolve_display_unit(cfg.get("display_unit"))
    size_line = "💰 均价 100000"
    if unit != "notional":
        size_line += " ｜ 数量 0.05"
    if unit != "quantity":
        size_line += " ｜ 金额 5,000.00 U"
    res = await NotificationService.dispatch(
        cfg,
        title=f"MirrorQuant - 用户通知测试 ({current_user.username})",
        message=("🟢 带单 开多/加多 ｜ BTCUSDT ｜ 做多\n"
                 f"{size_line}\n"
                 "🧮 1000U 本金 ≈ 开 100.00 U ｜ 建议杠杆 2x\n\n"
                 f"✅ 用户【{current_user.username}】个人通知渠道连接正常（以上为示例数据）"),
        event_type="test",
    )
    return {"ok": True, "results": res}
