from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, get_current_user, record_audit
from app.api.exposure import require_binding_flat
from app.core.database import get_db
from app.core.secret_mask import redact_proxy_userinfo
from app.models.domain import CopyBinding, LeadSource, ProxyNode, User
from app.schemas.api import LeadCreate, LeadUpdate
from app.services.engine.orchestrator import orchestrator
from app.services.engine.serialization import serialized_execution
from app.services.proxy.manager import ProxyEgressError, ProxyManager

router = APIRouter(prefix="/api/leads", tags=["leads"])


def _lead_summary(lead: LeadSource) -> dict:
    snap = None
    open_count = 0
    total_upnl = 0.0
    if lead.snapshot_json:
        try:
            snap = json.loads(lead.snapshot_json)
            open_pos = snap.get("openPositions") or []
            open_count = len(open_pos)
            total_upnl = float((snap.get("openPositionsSummary") or {}).get("totalUnrealizedPnl") or 0.0)
        except Exception:
            pass
    return {
        "id": lead.id,
        "name": lead.name,
        "platform": lead.platform,
        "portfolioId": lead.portfolio_id,
        "avatarUrl": lead.avatar_url,
        "description": lead.description,
        "pollIntervalSec": lead.poll_interval_sec,
        "proxyId": lead.proxy_id,
        "isEnabled": lead.is_enabled,
        "isPublicDashboard": lead.is_public_dashboard,
        "positionShow": lead.position_show,
        "marginBalance": lead.margin_balance,
        "aumAmount": lead.aum_amount,
        "lastTradeTime": lead.last_trade_time,
        "lastSyncAt": lead.last_sync_at.isoformat() if lead.last_sync_at else None,
        "lastError": redact_proxy_userinfo(lead.last_error) if lead.last_error else None,
        "status": lead.status,
        "openPositionsCount": open_count,
        "openUnrealizedPnl": round(total_upnl, 2),
    }


@router.get("")
async def list_leads(db: AsyncSession = Depends(get_db), _user: User = Depends(get_current_user)):
    leads = list((await db.scalars(select(LeadSource).order_by(LeadSource.id))).all())
    return {"items": [_lead_summary(l) for l in leads]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_lead(
    payload: LeadCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    existing = await db.scalar(
        select(LeadSource).where(
            LeadSource.platform == payload.platform,
            LeadSource.portfolio_id == payload.portfolio_id.strip(),
        )
    )
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Portfolio ID already registered")

    lead = LeadSource(
        name=payload.name.strip(),
        platform=payload.platform,
        portfolio_id=payload.portfolio_id.strip(),
        description=payload.description,
        poll_interval_sec=payload.poll_interval_sec,
        proxy_id=payload.proxy_id,
        is_enabled=payload.is_enabled,
        is_public_dashboard=payload.is_public_dashboard,
    )
    db.add(lead)
    await db.flush()
    await record_audit(
        db, "CREATE_LEAD", "lead", str(lead.id), f"Added lead {lead.name} ({lead.portfolio_id})", current_user, request
    )
    await db.commit()
    await db.refresh(lead)
    return {"item": _lead_summary(lead)}


@router.put("/{lead_id}")
@serialized_execution
async def update_lead(
    lead_id: int,
    payload: LeadUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    lead = await db.get(LeadSource, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(lead, k, v)
    await record_audit(db, "UPDATE_LEAD", "lead", str(lead.id), f"Updated fields: {list(data.keys())}", current_user, request)
    await db.commit()
    await db.refresh(lead)
    return {"item": _lead_summary(lead)}


@router.delete("/{lead_id}")
@serialized_execution
async def delete_lead(
    lead_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    lead = await db.get(LeadSource, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    if lead.is_enabled:
        raise HTTPException(status_code=409, detail="Disable the lead and drain exposure before deletion")
    bindings = list((await db.scalars(select(CopyBinding).where(CopyBinding.lead_id == lead_id))).all())
    for binding in bindings:
        await require_binding_flat(db, binding, deleting=True)
    name = lead.name
    await db.delete(lead)
    await record_audit(db, "DELETE_LEAD", "lead", str(lead_id), f"Deleted lead {name}", current_user, request)
    await db.commit()
    return {"ok": True}


@router.get("/{lead_id}/snapshot")
async def get_lead_snapshot(
    lead_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    lead = await db.get(LeadSource, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    snapshot = json.loads(lead.snapshot_json) if lead.snapshot_json else None
    return {"lead": _lead_summary(lead), "snapshot": snapshot}


@router.post("/{lead_id}/sync")
async def sync_lead_now(
    lead_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    lead = await db.get(LeadSource, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    res = await orchestrator.sync_lead(db, lead)
    await record_audit(db, "SYNC_LEAD", "lead", str(lead.id), f"Manual sync ok={res.get('ok')}", current_user, request)
    await db.commit()
    await db.refresh(lead)
    return {"lead": _lead_summary(lead), **res}


@router.get("/{lead_id}/analysis")
async def analyze_lead_symbol(
    lead_id: int,
    symbol: Optional[str] = None,
    position_side: str = "LONG",
    avg_entry: Optional[float] = None,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    lead = await db.get(LeadSource, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    proxies = list((await db.scalars(select(ProxyNode).order_by(ProxyNode.priority))).all())
    try:
        proxy_url = ProxyManager.select_proxy_url(proxies, lead.proxy_id)
    except ProxyEgressError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if symbol and avg_entry:
        res = await orchestrator.pa_analyzer.analyze_position(
            symbol.upper(), position_side.upper(), avg_entry, proxy_url=proxy_url,
        )
        return {"items": [res]}

    snap = json.loads(lead.snapshot_json) if lead.snapshot_json else {}
    items = []
    for p in snap.get("openPositions") or []:
        items.append(
            await orchestrator.pa_analyzer.analyze_position(
                p["symbol"],
                p["positionSide"],
                p.get("avgEntry"),
                current_price=p.get("currentPrice"),
                entry_orders=p.get("entryOrders"),
                proxy_url=proxy_url,
            )
        )
    return {"items": items}


@router.get("/{lead_id}/export-csv")
async def export_lead_csv(
    lead_id: int,
    export_type: str = Query(default="orders", pattern="^(orders|cycles)$"),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    lead = await db.get(LeadSource, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    snap = json.loads(lead.snapshot_json) if lead.snapshot_json else {}
    if export_type == "cycles":
        cycles = snap.get("tradeCycles") or []
        lines = [
            "cycle_id,symbol,position_side,status,start_time,end_time,duration,entry_count,exit_count,scale_in_count,total_entry_qty,total_exit_qty,avg_entry_price,avg_exit_price,realized_pnl,net_remaining"
        ]
        for c in cycles:
            lines.append(
                f'"{c.get("cycleId")}","{c.get("symbol")}","{c.get("positionSide")}","{c.get("status")}",'
                f'{c.get("startTime")},{c.get("endTime")},"{c.get("durationFormatted")}",'
                f'{c.get("entryCount")},{c.get("exitCount")},{c.get("scaleInCount")},'
                f'{c.get("totalEntryQty")},{c.get("totalExitQty")},{c.get("avgEntryPrice")},'
                f'{c.get("avgExitPrice")},{c.get("realizedPnl")},{c.get("netRemainingQty", 0.0)}'
            )
        csv_text = "\n".join(lines)
        filename = f"{lead.name}_trade_cycles.csv"
    else:
        orders = snap.get("orders") or []
        lines = ["time,order_time,symbol,side,position_side,direction,type,qty,avg_price,pnl"]
        for o in orders:
            lines.append(
                f'{o.get("time")},{o.get("orderTime")},"{o.get("symbol")}","{o.get("side")}","{o.get("positionSide")}",'
                f'"{o.get("dir")}","{o.get("type")}",{o.get("qty")},{o.get("avgPrice")},{o.get("pnl")}'
            )
        csv_text = "\n".join(lines)
        filename = f"{lead.name}_orders.csv"

    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
