from __future__ import annotations

from ipaddress import ip_address, ip_network
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.domain import AuditLog, User


def extract_client_ip(request: Optional[Request]) -> str:
    """Honor forwarded addresses only across explicitly trusted proxy hops."""
    if not request:
        return "unknown"
    peer = request.client.host if request.client and request.client.host else "unknown"
    try:
        address = ip_address(peer)
    except ValueError:
        return peer[:64]
    trusted = [ip_network(cidr) for cidr in get_settings().trusted_proxy_cidrs]
    if not any(address in network for network in trusted):
        return str(address)
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        try:
            hops = [ip_address(hop.strip()) for hop in forwarded.split(",")]
        except ValueError:
            return str(address)
        # Walk from the socket peer toward the client. A client-supplied prefix
        # must never override the first untrusted hop appended by our proxy.
        for hop in reversed(hops):
            if not any(address in network for network in trusted):
                break
            address = hop
        return str(address)
    for header in ("X-Real-IP", "CF-Connecting-IP"):
        value = request.headers.get(header)
        if value:
            try:
                return str(ip_address(value.strip()))
            except ValueError:
                return str(address)
    return str(address)


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


async def get_current_admin(
    user: User = Depends(get_current_user),
) -> User:
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator role required",
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
