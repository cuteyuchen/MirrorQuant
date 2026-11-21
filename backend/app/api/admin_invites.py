"""Admin invite-code management: create, list, revoke.

Registration is invite-only. An admin creates codes with a use limit and an
optional expiry; each successful registration consumes one use. The code
itself is a random 16-character alphanumeric string.
"""
from __future__ import annotations

import secrets
import string
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, record_audit
from app.core.database import get_db
from app.models.domain import InviteCode, User, utcnow
from app.schemas.api import InviteCodeCreate, InviteCodeOut

router = APIRouter(prefix="/api/admin/invite-codes", tags=["admin-invite-codes"])

_CODE_ALPHABET = string.ascii_letters + string.digits


def _invite_status(code: InviteCode, now: datetime) -> str:
    if code.is_revoked:
        return "revoked"
    if code.expires_at is not None:
        expires = code.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= now:
            return "expired"
    if code.used_count >= code.max_uses:
        return "exhausted"
    return "active"


async def _invite_out(db: AsyncSession, code: InviteCode) -> dict:
    creator = None
    if code.created_by_user_id is not None:
        creator = await db.get(User, code.created_by_user_id)
    return InviteCodeOut(
        id=code.id,
        code=code.code,
        created_by_user_id=code.created_by_user_id,
        created_by_username=creator.username if creator else None,
        max_uses=code.max_uses,
        used_count=code.used_count,
        expires_at=code.expires_at,
        is_revoked=code.is_revoked,
        created_at=code.created_at,
        status=_invite_status(code, datetime.now(timezone.utc)),
    ).model_dump(mode="json", by_alias=True)


def _generate_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(16))


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_invite_code(
    payload: InviteCodeCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    code_text = None
    for _ in range(5):
        candidate = _generate_code()
        clash = await db.scalar(select(InviteCode).where(InviteCode.code == candidate))
        if clash is None:
            code_text = candidate
            break
    if code_text is None:
        raise HTTPException(status_code=503, detail="Could not generate a unique invite code; try again")
    code = InviteCode(
        code=code_text,
        created_by_user_id=current_user.id,
        max_uses=payload.max_uses,
        used_count=0,
        expires_at=payload.expires_at,
        is_revoked=False,
    )
    db.add(code)
    await db.flush()
    await record_audit(db, "CREATE_INVITE_CODE", "invite_code", str(code.id),
                       f"Created invite code (max_uses={code.max_uses})", current_user, request)
    await db.commit()
    await db.refresh(code)
    return {"item": await _invite_out(db, code)}


@router.get("")
async def list_invite_codes(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_admin),
):
    rows = list((await db.scalars(select(InviteCode).order_by(InviteCode.id.desc()))).all())
    return {"items": [await _invite_out(db, code) for code in rows]}


@router.post("/{code_id}/revoke")
async def revoke_invite_code(
    code_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    code = await db.get(InviteCode, code_id)
    if code is None:
        raise HTTPException(status_code=404, detail="Invite code not found")
    if code.is_revoked:
        return {"item": await _invite_out(db, code)}
    code.is_revoked = True
    await record_audit(db, "REVOKE_INVITE_CODE", "invite_code", str(code.id),
                       "Revoked invite code", current_user, request)
    await db.commit()
    await db.refresh(code)
    return {"item": await _invite_out(db, code)}
