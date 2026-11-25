from __future__ import annotations

from app.core.time_util import to_utc_iso

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, get_current_user, record_audit
from app.core.database import get_db
from app.core.secret_mask import mask_proxy_url, merge_proxy_url, proxy_credentials_configured, redact_error_urls
from app.models.domain import LeadSource, ProxyNode, TradingAccount, User
from app.schemas.api import ProxyCreate, ProxyUpdate
from app.services.proxy.manager import ProxyManager
from app.services.adapters.factory import invalidate_proxy_adapters
from app.services.engine.serialization import serialized_execution
from app.services.lead_collector.binance_bapi import BinanceBapiCollector

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
        "lastCheckedAt": to_utc_iso(p.last_checked_at),
        "lastError": redact_error_urls(p.last_error) if p.last_error else None,
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
    current_user: User = Depends(get_current_admin),
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
@serialized_execution  # 代理变更与金融执行串行，防止中途换出口。
async def update_proxy(
    proxy_id: int,
    payload: ProxyUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    node = await db.get(ProxyNode, proxy_id)
    if not node:
        raise HTTPException(status_code=404, detail="Proxy not found")
    data = payload.model_dump(exclude_unset=True)
    previous_url = node.url
    if "url" in data:
        try:
            data["url"] = merge_proxy_url(node.url, data["url"])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    for k, v in data.items():
        setattr(node, k, v)
    await record_audit(db, "UPDATE_PROXY", "proxy", str(node.id), f"Updated proxy {node.name}", current_user, request)
    await db.commit()
    if "url" in data or "is_enabled" in data:
        await invalidate_proxy_adapters(previous_url)
        # 只关闭被修改出口，保留其他带单员的健康长连接。
        await BinanceBapiCollector.close_proxy_clients(previous_url)
    await db.refresh(node)
    return {"item": _proxy_out(node)}


@router.delete("/{proxy_id}")
@serialized_execution
async def delete_proxy(
    proxy_id: int,
    request: Request,
    unassign: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    node = await db.get(ProxyNode, proxy_id)
    if not node:
        raise HTTPException(status_code=404, detail="Proxy not found")
    # 只检查未软删除的活跃账户与带单员，忽略已删除历史行。
    active_accounts = list((await db.scalars(select(TradingAccount).where(
        TradingAccount.proxy_id == proxy_id, TradingAccount.deleted_at.is_(None)))).all())
    active_leads = list((await db.scalars(select(LeadSource).where(
        LeadSource.proxy_id == proxy_id, LeadSource.deleted_at.is_(None)))).all())
    if active_accounts or active_leads:
        if not unassign:
            refs = [*(f"Account:{a.name}" for a in active_accounts), *(f"Lead:{l.name}" for l in active_leads)]
            raise HTTPException(
                status_code=409,
                detail=f"Proxy is assigned; explicitly reassign accounts and leads first ({', '.join(refs[:5])})",
            )
        for acct in active_accounts:
            acct.proxy_id = None
        for lead in active_leads:
            lead.proxy_id = None
    name = node.name
    previous_url = node.url
    await db.delete(node)
    await record_audit(db, "DELETE_PROXY", "proxy", str(proxy_id), f"Deleted proxy {name}", current_user, request)
    await db.commit()
    await invalidate_proxy_adapters(previous_url)
    await BinanceBapiCollector.close_proxy_clients(previous_url)
    return {"ok": True}


@router.post("/{proxy_id}/test")
async def test_proxy_node(
    proxy_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_admin),
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
            "error": redact_error_urls(res.error) if res.error else None,
        },
    }
