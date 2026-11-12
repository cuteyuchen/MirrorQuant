from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, record_audit
from app.core.config import get_settings
from app.core.database import DEFAULT_SYSTEM_CONFIG, get_db
from app.models.domain import AuditLog, CopyBinding, LoginAttemptGuard, SystemConfig, User
from app.schemas.api import SettingsUpdate
from app.services.notifier.service import NotificationService

router = APIRouter(prefix="/api/settings", tags=["settings"])


async def _load_cfg(db: AsyncSession, key: str):
    row = await db.scalar(select(SystemConfig).where(SystemConfig.key == key))
    default = DEFAULT_SYSTEM_CONFIG.get(key)
    if row:
        val = json.loads(row.value_json)
        if isinstance(default, dict) and isinstance(val, dict):
            return {**default, **val}
        return val
    return default


async def _save_cfg(db: AsyncSession, key: str, value) -> None:
    row = await db.scalar(select(SystemConfig).where(SystemConfig.key == key))
    raw = json.dumps(value, ensure_ascii=False)
    if row is None:
        db.add(SystemConfig(key=key, value_json=raw))
    else:
        row.value_json = raw


@router.get("")
async def get_settings_view(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    settings = get_settings()
    return {
        "global_kill_switch": await _load_cfg(db, "global_kill_switch"),
        "public_monitoring_enabled": await _load_cfg(db, "public_monitoring_enabled"),
        "notifications": await _load_cfg(db, "notifications"),
        "risk_defaults": await _load_cfg(db, "risk_defaults"),
        "resilience": await _load_cfg(db, "resilience"),
        "security": {
            "encryptionAlgorithm": "AES-256-GCM",
            "passwordHashing": "Argon2id",
            "authMode": "JWT (HttpOnly Cookie)",
            "masterKeyConfigured": bool(settings.resolved_master_key()),
            "masterKeySource": "environment" if settings.master_key else "auto-generated (.master_key)",
            "totpEnabled": bool(getattr(current_user, "totp_enabled", False)),
        },
    }


@router.put("")
async def update_settings_view(
    payload: SettingsUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    updated = []
    if payload.global_kill_switch is not None:
        await _save_cfg(db, "global_kill_switch", payload.global_kill_switch)
        updated.append("global_kill_switch")
    if payload.public_monitoring_enabled is not None:
        await _save_cfg(db, "public_monitoring_enabled", payload.public_monitoring_enabled)
        updated.append("public_monitoring_enabled")
    if payload.notifications is not None:
        await _save_cfg(db, "notifications", payload.notifications)
        updated.append("notifications")
    if payload.risk_defaults is not None:
        await _save_cfg(db, "risk_defaults", payload.risk_defaults)
        updated.append("risk_defaults")
    if payload.resilience is not None:
        await _save_cfg(db, "resilience", payload.resilience)
        updated.append("resilience")

    await record_audit(
        db, "UPDATE_SETTINGS", "system", "settings", f"Updated keys: {updated}", current_user, request
    )
    await db.commit()
    return await get_settings_view(db=db, current_user=current_user)


@router.post("/apply-defaults-to-bindings")
async def apply_defaults_to_bindings(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    defaults = await _load_cfg(db, "risk_defaults") or {}
    bindings = list((await db.scalars(select(CopyBinding))).all())
    field_map = {
        "max_total_leverage": float,
        "max_single_leverage": float,
        "max_instrument_leverage": int,
        "max_drawdown_pct": float,
        "daily_max_loss_pct": float,
        "max_slippage_pct": float,
        "max_spread_pct": float,
        "ioc_offset_pct": float,
        "order_mode": str,
        "tp_sl_mode": str,
        "enable_stop_loss": bool,
        "stop_loss_pct": float,
        "enable_take_profit": bool,
        "take_profit_pct": float,
        "enable_trailing_stop": bool,
        "trailing_callback_pct": float,
        "hard_stop_loss_pct": float,
    }
    for b in bindings:
        for key, cast_fn in field_map.items():
            if key in defaults and defaults[key] is not None:
                setattr(b, key, cast_fn(defaults[key]))

    await record_audit(
        db,
        "APPLY_RISK_DEFAULTS",
        "binding",
        "all",
        f"Applied global defaults to {len(bindings)} binding(s)",
        current_user,
        request,
    )
    await db.commit()
    return {"ok": True, "updatedCount": len(bindings)}


@router.post("/test-notify")
async def test_notification(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    cfg = await _load_cfg(db, "notifications") or {}
    res = await NotificationService.dispatch(
        cfg,
        title="MirrorQuant Test Alert",
        message="Notification channel connectivity verified successfully.",
        event_type="test",
    )
    return {"ok": True, "results": res}


@router.get("/security-guards")
async def list_security_guards(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    now = datetime.now(timezone.utc)
    rows = list(
        (await db.scalars(select(LoginAttemptGuard).order_by(desc(LoginAttemptGuard.updated_at)).limit(100))).all()
    )
    items = []
    for g in rows:
        lu = g.locked_until
        if lu and lu.tzinfo is None:
            lu = lu.replace(tzinfo=timezone.utc)
        rem = max(0, int((lu - now).total_seconds())) if (lu and lu > now) else 0
        items.append(
            {
                "id": g.id,
                "guardKey": g.guard_key,
                "keyType": g.key_type,
                "keyValue": g.key_value,
                "failCount": g.fail_count,
                "lockedUntil": lu.isoformat() if lu else None,
                "lockRemainingSec": rem,
                "lastIp": g.last_ip,
                "lastUsername": g.last_username,
                "lastFailAt": g.last_fail_at.isoformat() if g.last_fail_at else None,
            }
        )
    return {"items": items}


@router.post("/security-guards/{guard_id}/unlock")
async def unlock_security_guard(
    guard_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    guard = await db.get(LoginAttemptGuard, guard_id)
    if not guard:
        raise HTTPException(status_code=404, detail="Security guard entry not found")
    guard.fail_count = 0
    guard.locked_until = None
    guard.alert_sent_level = 0
    await record_audit(
        db,
        "UNLOCK_LOGIN_GUARD",
        "security",
        str(guard.id),
        f"Unlocked {guard.guard_key}",
        current_user,
        request,
    )
    await db.commit()
    return {"ok": True}


@router.get("/audit-logs")
async def list_audit_logs(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    rows = list((await db.scalars(select(AuditLog).order_by(desc(AuditLog.created_at)).offset(offset).limit(limit))).all())
    return {
        "items": [
            {
                "id": r.id,
                "username": r.username,
                "action": r.action,
                "resourceType": r.resource_type,
                "resourceId": r.resource_id,
                "details": r.details,
                "ipAddress": r.ip_address,
                "createdAt": r.created_at.isoformat(),
            }
            for r in rows
        ]
    }
