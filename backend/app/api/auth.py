from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_current_user_optional, record_audit
from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import create_access_token, hash_password, verify_password
from app.models.domain import User
from app.schemas.api import ChangePasswordRequest, LoginRequest, SetupRequest, UserOut

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _set_auth_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.cookie_name,
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        max_age=settings.access_token_expire_minutes * 60,
        path="/",
    )


def _user_dict(u: User) -> dict:
    return UserOut(
        id=u.id,
        username=u.username,
        role=u.role,
        is_active=u.is_active,
        created_at=u.created_at,
        last_login_at=u.last_login_at,
    ).model_dump()


@router.get("/status")
async def auth_status(
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    count = await db.scalar(select(func.count(User.id))) or 0
    return {
        "initialized": count > 0,
        "authenticated": current_user is not None,
        "user": _user_dict(current_user) if current_user else None,
    }


@router.post("/setup", status_code=status.HTTP_201_CREATED)
async def setup_admin(
    payload: SetupRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    count = await db.scalar(select(func.count(User.id))) or 0
    if count > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="System is already initialized",
        )
    user = User(
        username=payload.username.strip(),
        password_hash=hash_password(payload.password),
        role="admin",
        is_active=True,
        last_login_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.flush()
    await record_audit(db, "SETUP_ADMIN", "user", str(user.id), f"Initialized admin {user.username}", user, request)
    await db.commit()
    await db.refresh(user)

    token = create_access_token(user.id, user.username, user.role)
    _set_auth_cookie(response, token)
    return {"user": _user_dict(user), "token": token}


@router.post("/login")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    user = await db.scalar(select(User).where(User.username == payload.username.strip()))
    if not user or not verify_password(payload.password, user.password_hash):
        await record_audit(db, "LOGIN_FAILED", "user", None, f"Failed login for {payload.username}", None, request)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account disabled")

    user.last_login_at = datetime.now(timezone.utc)
    await record_audit(db, "LOGIN", "user", str(user.id), "Admin logged in", user, request)
    await db.commit()

    token = create_access_token(user.id, user.username, user.role)
    _set_auth_cookie(response, token)
    return {"user": _user_dict(user), "token": token}


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    settings = get_settings()
    response.delete_cookie(key=settings.cookie_name, path="/")
    if current_user:
        await record_audit(db, "LOGOUT", "user", str(current_user.id), "Logged out", current_user, request)
        await db.commit()
    return {"ok": True}


@router.get("/me")
async def me(current_user: User = Depends(get_current_user)):
    return {"user": _user_dict(current_user)}


@router.post("/change-password")
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password incorrect")
    current_user.password_hash = hash_password(payload.new_password)
    await record_audit(db, "CHANGE_PASSWORD", "user", str(current_user.id), "Password rotated", current_user, request)
    await db.commit()
    return {"ok": True}
