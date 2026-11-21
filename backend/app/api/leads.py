from __future__ import annotations

from app.core.time_util import to_utc_iso

import json
import csv
import io
from urllib.parse import quote
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, get_current_user, record_audit
from app.api.exposure import require_binding_flat
from app.core.database import get_db
from app.core.secret_mask import NOTIFICATION_SECRET_FIELDS, mask_sensitive_payload, merge_secrets, redact_error_urls
from app.models.domain import CopyBinding, LeadSource, ProxyNode, User
from app.schemas.api import LeadCreate, LeadUpdate
from app.services.engine.orchestrator import orchestrator
from app.services.engine.serialization import serialized_execution
from app.services.lead_collector.binance_bapi import BinanceBapiCollector
from app.services.notifier.service import NotificationService
from app.services.notifier.trade import resolve_display_unit
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
    notify_cfg = None
    if lead.notification_config_json:
        try:
            raw_cfg = json.loads(lead.notification_config_json)
            notify_cfg = mask_sensitive_payload(raw_cfg)
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
        "slotWatchEnabled": lead.slot_watch_enabled,
        "positionShow": lead.position_show,
        "marginBalance": lead.margin_balance,
        "aumAmount": lead.aum_amount,
        "lastTradeTime": lead.last_trade_time,
        "lastSyncAt": to_utc_iso(lead.last_sync_at),
        "lastError": redact_error_urls(lead.last_error) if lead.last_error else None,
        "status": lead.status,
        "openPositionsCount": open_count,
        "openUnrealizedPnl": round(total_upnl, 2),
        "notificationConfig": notify_cfg,
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

    notify_json = None
    if payload.notification_config is not None:
        notify_json = json.dumps(payload.notification_config.model_dump())

    lead = LeadSource(
        name=payload.name.strip(),
        platform=payload.platform,
        portfolio_id=payload.portfolio_id.strip(),
        description=payload.description,
        poll_interval_sec=payload.poll_interval_sec,
        proxy_id=payload.proxy_id,
        is_enabled=payload.is_enabled,
        is_public_dashboard=payload.is_public_dashboard,
        slot_watch_enabled=payload.slot_watch_enabled,
        notification_config_json=notify_json,
        created_by_user_id=current_user.id,
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
    if "notification_config" in data:
        cfg = data.pop("notification_config")
        if cfg is not None:
            existing = {}
            if lead.notification_config_json:
                try:
                    existing = json.loads(lead.notification_config_json)
                except Exception:
                    pass
            new_cfg = merge_secrets(existing, cfg, NOTIFICATION_SECRET_FIELDS)
            lead.notification_config_json = json.dumps(new_cfg)
        else:
            lead.notification_config_json = None

    previous_proxy_id = lead.proxy_id
    for k, v in data.items():
        setattr(lead, k, v)
    await record_audit(db, "UPDATE_LEAD", "lead", str(lead.id), f"Updated fields: {list(data.keys())}", current_user, request)
    await db.commit()
    if "proxy_id" in data and previous_proxy_id != data["proxy_id"]:
        # 显式换出口后不再保留该出口的旧采集连接。
        previous_proxy = await db.get(ProxyNode, previous_proxy_id) if previous_proxy_id is not None else None
        await BinanceBapiCollector.close_proxy_clients(previous_proxy.url if previous_proxy else None)
    await db.refresh(lead)
    return {"item": _lead_summary(lead)}


@router.post("/{lead_id}/test-notify")
async def test_lead_notification(
    lead_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_admin),
):
    lead = await db.get(LeadSource, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    if not lead.notification_config_json:
        raise HTTPException(status_code=400, detail="Lead has no notification configuration")
    try:
        cfg = json.loads(lead.notification_config_json)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid notification configuration")
    unit = resolve_display_unit(cfg.get("display_unit"))
    size_line = "💰 均价 100000"
    if unit != "notional":
        size_line += " ｜ 数量 0.05"
    if unit != "quantity":
        size_line += " ｜ 金额 5,000.00 U"
    res = await NotificationService.dispatch(
        cfg,
        title=f"MirrorQuant - {lead.name} 测试",
        message=(f"【{lead.name}】\n"
                 f"🟢 带单 开多/加多 ｜ BTCUSDT ｜ 做多\n"
                 f"{size_line}\n"
                 "🧮 1000U 本金 ≈ 开 100.00 U ｜ 建议杠杆 2x\n\n"
                 f"✅ 带单员【{lead.name}】专属通知渠道连接正常（以上为示例数据）"),
        event_type="test",
    )
    return {"ok": True, "results": res}


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
    return mask_sensitive_payload({"lead": _lead_summary(lead), "snapshot": snapshot})


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
    return mask_sensitive_payload({"lead": _lead_summary(lead), **res})


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
        proxy_url = ProxyManager.select_proxy_url(proxies, lead.proxy_id, exchange=orchestrator.pa_analyzer.exchange)
    except ProxyEgressError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if symbol and avg_entry:
        res = await orchestrator.pa_analyzer.analyze_position(
            symbol.upper(), position_side.upper(), avg_entry, proxy_url=proxy_url,
        )
        return mask_sensitive_payload({"items": [res]})

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
    return mask_sensitive_payload({"items": items})


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
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    # 标准 CSV 转义与公式防护同时生效，真正的数值负号不受影响。
    def cell(value):
        if isinstance(value, str) and (value.lstrip("\ufeff \t\r\n").startswith(("=", "+", "-", "@"))
                                       or value.startswith(("\t", "\r", "\n"))):
            return "'" + value
        return value

    if export_type == "cycles":
        writer.writerow(["cycle_id", "symbol", "position_side", "status", "start_time", "end_time", "duration",
                         "entry_count", "exit_count", "scale_in_count", "total_entry_qty", "total_exit_qty",
                         "avg_entry_price", "avg_exit_price", "realized_pnl", "net_remaining"])
        fields = ("cycleId", "symbol", "positionSide", "status", "startTime", "endTime", "durationFormatted",
                  "entryCount", "exitCount", "scaleInCount", "totalEntryQty", "totalExitQty",
                  "avgEntryPrice", "avgExitPrice", "realizedPnl", "netRemainingQty")
        for row in snap.get("tradeCycles") or []:
            writer.writerow([cell(row.get(field)) for field in fields])
        filename = f"{lead.name}_trade_cycles.csv"
    else:
        writer.writerow(["time", "order_time", "symbol", "side", "position_side", "direction", "type", "qty", "avg_price", "pnl"])
        fields = ("time", "orderTime", "symbol", "side", "positionSide", "dir", "type", "qty", "avgPrice", "pnl")
        for row in snap.get("orders") or []:
            writer.writerow([cell(row.get(field)) for field in fields])
        filename = f"{lead.name}_orders.csv"

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=\"lead-{lead.id}-{export_type}.csv\"; "
                 f"filename*=UTF-8''{quote(filename, safe='')}"},
    )
