from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, record_audit
from app.core.config import get_settings
from app.core.database import DEFAULT_SYSTEM_CONFIG, get_db
from app.models.domain import AuditLog, SystemConfig, User
from app.schemas.api import SettingsUpdate
from app.services.notifier.service import NotificationService

router = APIRouter(prefix="/api/settings", tags=["settings"])


async def _load_cfg(db: AsyncSession, key: str):
    row = await db.scalar(select(SystemConfig).where(SystemConfig.key == key))
    if row:
        return json.loads(row.value_json)
    return DEFAULT_SYSTEM_CONFIG.get(key)


async def _save_cfg(db: AsyncSession, key: str, value) -> None:
    row = await db.scalar(select(SystemConfig).where(SystemConfig.key == key))
    raw = json.dumps(value, ensure_ascii=False)
    if row is None:
        db.add(SystemConfig(key=key, value_json=raw))
    else:
        row.value_json = raw


@router.get("")
async def get_settings_view(db: AsyncSession = Depends(get_db), _user: User = Depends(get_current_user)):
    settings = get_settings()
    return {
        "global_kill_switch": await _load_cfg(db, "global_kill_switch"),
        "public_monitoring_enabled": await _load_cfg(db, "public_monitoring_enabled"),
        "notifications": await _load_cfg(db, "notifications"),
        "risk_defaults": await _load_cfg(db, "risk_defaults"),
        "security": {
            "encryptionAlgorithm": "AES-256-GCM",
            "passwordHashing": "Argon2id",
            "authMode": "JWT (HttpOnly Cookie)",
            "masterKeyConfigured": bool(settings.resolved_master_key()),
            "masterKeySource": "environment" if settings.master_key else "auto-generated (.master_key)",
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

    await record_audit(
        db, "UPDATE_SETTINGS", "system", "settings", f"Updated keys: {updated}", current_user, request
    )
    await db.commit()
    return await get_settings_view(db=db, _user=current_user)


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
