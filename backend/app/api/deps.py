from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.domain import AuditLog, User


def extract_client_ip(request: Optional[Request]) -> str:
    """Extract real client IP safely behind Nginx/Cloudflare reverse proxy."""
    if not request:
        return "unknown"
    cf_ip = (request.headers.get("CF-Connecting-IP") or "").strip()
    if cf_ip:
        return cf_ip[:64]
    real_ip = (request.headers.get("X-Real-IP") or "").strip()
    if real_ip:
        return real_ip[:64]
    fwd = (request.headers.get("X-Forwarded-For") or "").strip()
    if fwd:
        first_hop = fwd.split(",")[0].strip()
        if first_hop:
            return first_hop[:64]
    if request.client and request.client.host:
        return request.client.host[:64]
    return "unknown"


def extract_token_from_request(request: Request) -> Optional[str]:
    settings = get_settings()
    token = request.cookies.get(settings.cookie_name)
    if token:
        return token
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return None


async def get_current_user_optional(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Optional[User]:
    token = extract_token_from_request(request)
    if not token:
        return None
    payload = decode_access_token(token)
    if not payload or not payload.get("sub"):
        return None
    try:
        user_id = int(payload["sub"])
    except (TypeError, ValueError):
        return None
    user = await db.scalar(select(User).where(User.id == user_id, User.is_active == True))  # noqa: E712
    if user is None or payload.get("ver") != user.session_version:
        return None
    return user


async def get_current_user(
    user: Optional[User] = Depends(get_current_user_optional),
) -> User:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    return user


async def record_audit(
    db: AsyncSession,
    action: str,
    resource_type: str,
    resource_id: Optional[str] = None,
    details: str = "",
    user: Optional[User] = None,
    request: Optional[Request] = None,
) -> None:
    ip = extract_client_ip(request) if request else None
    db.add(
        AuditLog(
            user_id=user.id if user else None,
            username=user.username if user else "system",
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
            ip_address=ip,
        )
    )
