from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, record_audit
from app.core.database import get_db
from app.core.secret_mask import mask_proxy_url, merge_proxy_url, proxy_credentials_configured, redact_proxy_userinfo
from app.models.domain import ProxyNode, User
from app.schemas.api import ProxyCreate, ProxyUpdate
from app.services.proxy.manager import ProxyManager

router = APIRouter(prefix="/api/proxies", tags=["proxies"])


def _proxy_out(p: ProxyNode) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "url": mask_proxy_url(p.url),
        "url_configured": proxy_credentials_configured(p.url),
        "region": p.region,
        "priority": p.priority,
        "isEnabled": p.is_enabled,
        "isHealthy": p.is_healthy,
        "latencyMs": p.latency_ms,
        "lastCheckedAt": p.last_checked_at.isoformat() if p.last_checked_at else None,
        "lastError": redact_proxy_userinfo(p.last_error) if p.last_error else None,
    }


@router.get("")
async def list_proxies(db: AsyncSession = Depends(get_db), _user: User = Depends(get_current_user)):
    rows = list((await db.scalars(select(ProxyNode).order_by(ProxyNode.priority, ProxyNode.id))).all())
    return {"items": [_proxy_out(p) for p in rows]}


@router.get("/{proxy_id}")
async def get_proxy(proxy_id: int, db: AsyncSession = Depends(get_db), _user: User = Depends(get_current_user)):
    node = await db.get(ProxyNode, proxy_id)
    if not node:
        raise HTTPException(status_code=404, detail="Proxy not found")
    return {"item": _proxy_out(node)}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_proxy(
    payload: ProxyCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    node = ProxyNode(
        name=payload.name.strip(),
        url=payload.url.strip(),
        region=payload.region.strip().upper(),
        priority=payload.priority,
        is_enabled=payload.is_enabled,
    )
    db.add(node)
    await db.flush()
    await record_audit(db, "CREATE_PROXY", "proxy", str(node.id), f"Added proxy {node.name}", current_user, request)
    await db.commit()
    await db.refresh(node)
    return {"item": _proxy_out(node)}


@router.put("/{proxy_id}")
async def update_proxy(
    proxy_id: int,
    payload: ProxyUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    node = await db.get(ProxyNode, proxy_id)
    if not node:
        raise HTTPException(status_code=404, detail="Proxy not found")
    data = payload.model_dump(exclude_unset=True)
    if "url" in data:
        try:
            data["url"] = merge_proxy_url(node.url, data["url"])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    for k, v in data.items():
        setattr(node, k, v)
    await record_audit(db, "UPDATE_PROXY", "proxy", str(node.id), f"Updated proxy {node.name}", current_user, request)
    await db.commit()
    await db.refresh(node)
    return {"item": _proxy_out(node)}


@router.delete("/{proxy_id}")
async def delete_proxy(
    proxy_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    node = await db.get(ProxyNode, proxy_id)
    if not node:
        raise HTTPException(status_code=404, detail="Proxy not found")
    name = node.name
    await db.delete(node)
    await record_audit(db, "DELETE_PROXY", "proxy", str(proxy_id), f"Deleted proxy {name}", current_user, request)
    await db.commit()
    return {"ok": True}


@router.post("/{proxy_id}/test")
async def test_proxy_node(
    proxy_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    node = await db.get(ProxyNode, proxy_id)
    if not node:
        raise HTTPException(status_code=404, detail="Proxy not found")
    res = await ProxyManager.test_proxy(node.url)
    node.is_healthy = res.healthy
    node.latency_ms = res.latency_ms
    node.last_checked_at = datetime.now(timezone.utc)
    node.last_error = res.error
    await db.commit()
    await db.refresh(node)
    return {
        "item": _proxy_out(node),
        "test": {
            "healthy": res.healthy,
            "latencyMs": res.latency_ms,
            "error": redact_proxy_userinfo(res.error) if res.error else None,
        },
    }
