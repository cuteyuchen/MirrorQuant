from __future__ import annotations

from app.core.time_util import to_utc_iso

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, get_current_user
from app.core.database import get_db
from app.core.secret_mask import mask_sensitive_payload, redact_error_urls
from app.services.lead_collector.freshness import lead_freshness
from app.services.positions import with_current_price
from app.services.engine.equity import account_equity_display, account_usdt_hwm
from app.services.dashboard_board import build_board, milliseconds, number
from app.models.domain import (
    CopyBinding,
    ExecutionRecord,
    LeadSource,
    OrderIntent,
    SystemConfig,
    TradingAccount,
    User,
)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/board")
async def dashboard_board(db: AsyncSession = Depends(get_db), _user: User = Depends(get_current_admin)):
    # 管理员只读快照投影，不在 HTTP 请求中触发交易所访问。
    return await build_board(db)


@router.get("/intents")
async def list_intents(
    state: str = Query(default="UNKNOWN,SUBMITTING", max_length=120),
    db: AsyncSession = Depends(get_db), _user: User = Depends(get_current_admin),
):
    # 状态白名单避免含糊过滤；时间与其余新接口统一为毫秒。
    states = {value.strip().upper() for value in state.split(",") if value.strip()}
    if not states or not states <= {"UNKNOWN", "SUBMITTING", "NEW", "PARTIALLY_FILLED", "RESTING"}:
        raise HTTPException(status_code=422, detail="Only nonterminal intent states are supported")
    rows = (await db.execute(select(OrderIntent, CopyBinding, TradingAccount)
        .outerjoin(CopyBinding, CopyBinding.id == OrderIntent.binding_id)
        .outerjoin(TradingAccount, TradingAccount.id == OrderIntent.account_id)
        .where(func.upper(OrderIntent.state).in_(states))
        .order_by(OrderIntent.created_at.desc(), OrderIntent.id.desc()))).all()
    return {"items": [{
        "id": intent.id, "bindingId": intent.binding_id, "bindingName": binding.name if binding else None,
        "accountId": intent.account_id, "accountName": account.name if account else None,
        "instId": intent.inst_id, "posSide": intent.pos_side, "state": intent.state,
        "requestedQty": number(intent.requested_qty), "filledQty": number(intent.filled_qty),
        "exchangeOrderId": intent.exchange_order_id, "reduceOnly": intent.reduce_only,
        "createdAt": milliseconds(intent.created_at), "sourceEventId": intent.source_event_id,
    } for intent, binding, account in rows]}


@router.get("/overview")
async def dashboard_overview(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    leads = list((await db.scalars(select(LeadSource).order_by(LeadSource.id))).all())
    accounts = list((await db.scalars(select(TradingAccount).where(
        TradingAccount.deleted_at.is_(None)).order_by(TradingAccount.id))).all())
    bindings = list((await db.scalars(select(CopyBinding).where(
        CopyBinding.deleted_at.is_(None)).order_by(CopyBinding.id))).all())
    recent_execs = list(
        (await db.scalars(select(ExecutionRecord).order_by(desc(ExecutionRecord.created_at)).limit(30))).all()
    )
    ks_row = await db.scalar(select(SystemConfig).where(SystemConfig.key == "global_kill_switch"))
    global_kill = json.loads(ks_row.value_json) if ks_row else False

    total_equity = sum(account_equity_display(a)["usdtEquity"] for a in accounts if a.is_enabled)
    total_avail = sum(account_equity_display(a)["availEquity"] for a in accounts if a.is_enabled)
    total_notional = sum(a.total_notional for a in accounts if a.is_enabled)
    total_upl = sum(a.unrealized_pnl for a in accounts if a.is_enabled)
    total_hwm = sum(account_usdt_hwm(a) for a in accounts if a.is_enabled)

    lead_positions = []
    leads_status = []
    for lead in leads:
        snap = {}
        try:
            snap = json.loads(lead.snapshot_json) if lead.snapshot_json else {}
            if not isinstance(snap, dict):
                snap = {}
            for p in snap.get("openPositions", []):
                if isinstance(p, dict):
                    lead_positions.append({**p, "leadId": lead.id, "leadName": lead.name})
        except Exception:
            snap = {}
        leads_status.append({
            "id": lead.id,
            "name": lead.name,
            "sourceType": lead.platform,
            "isEnabled": lead.is_enabled,
            "status": lead.status,
            "lastSyncAt": to_utc_iso(lead.last_sync_at),
            "lastError": redact_error_urls(lead.last_error) if lead.last_error else None,
            "openPositionsCount": sum(1 for p in lead_positions if p["leadId"] == lead.id),
            "positionsKnown": isinstance(snap.get("openPositions"), list),
            "snapshotIncomplete": bool((snap.get("openPositionsMeta") or {}).get("partial")
                                       or (snap.get("openPositionsMeta") or {}).get("error")
                                       or (snap.get("meta") or {}).get("detailOk") is False),
            "marginBalance": lead.margin_balance,
            "boundAccountIds": [b.account_id for b in bindings if b.lead_id == lead.id and b.is_enabled],
            **lead_freshness(lead, snap),
        })

    follower_positions = []
    for acct in accounts:
        if not acct.positions_json:
            continue
        try:
            for p in json.loads(acct.positions_json):
                if not isinstance(p, dict):
                    continue
                follower_positions.append(
                    {
                        **with_current_price(p, acct.exchange),
                        "accountId": acct.id,
                        "accountName": acct.name,
                        "exchange": acct.exchange,
                        "environment": acct.environment,
                        "accountStatus": acct.status,
                        "lastSyncAt": to_utc_iso(acct.last_sync_at),
                    }
                )
        except Exception:
            pass

    return {
        "kpis": {
            "totalEquity": round(total_equity, 2),
            "availEquity": round(total_avail, 2),
            "totalNotional": round(total_notional, 2),
            "totalUnrealizedPnl": round(total_upl, 2),
            "actualLeverage": round(total_notional / total_equity, 2) if total_equity > 0 else 0.0,
            "drawdownPct": round(max(0.0, (total_hwm - total_equity) / total_hwm * 100.0), 2) if total_hwm > 0 else 0.0,
            "activeLeads": sum(1 for l in leads if l.is_enabled),
            "totalLeads": len(leads),
            "activeAccounts": sum(1 for a in accounts if a.is_enabled),
            "totalAccounts": len(accounts),
            "activeBindings": sum(1 for b in bindings if b.is_enabled),
            "globalKillSwitch": bool(global_kill),
        },
        "leadPositions": mask_sensitive_payload(lead_positions),
        "leadsStatus": mask_sensitive_payload(leads_status),
        "accountsStatus": [
            {"id": a.id, "isEnabled": a.is_enabled, "status": a.status,
             "lastSyncAt": to_utc_iso(a.last_sync_at)}
            for a in accounts
        ],
        "bindingsStatus": [
            {"leadId": b.lead_id, "accountId": b.account_id, "isEnabled": b.is_enabled,
             "circuitBreakerTripped": b.circuit_breaker_tripped,
             "symbolMap": json.loads(b.symbol_map_json or "{}")}
            for b in bindings
        ],
        "followerPositions": mask_sensitive_payload(follower_positions),
        "recentExecutions": [
            {
                "id": r.id,
                "bindingId": r.binding_id,
                "leadName": r.lead_name,
                "accountName": r.account_name,
                "symbol": r.symbol,
                "instId": r.inst_id,
                "side": r.side,
                "positionSide": r.position_side,
                "action": r.action,
                "status": r.status,
                "leadQty": r.lead_qty,
                "leadPrice": r.lead_price,
                "leadNotional": r.lead_notional,
                "myQty": r.my_qty,
                "myPrice": r.my_price,
                "myNotional": r.my_notional,
                "slippagePct": r.slippage_pct,
                "spreadPct": r.spread_pct,
                "orderId": r.order_id,
                "details": redact_error_urls(r.details),
                "createdAt": to_utc_iso(r.created_at),
            }
            for r in recent_execs
        ],
    }


@router.get("/executions")
async def list_executions(
    binding_id: Optional[int] = None,
    status_filter: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_admin),
):
    stmt = select(ExecutionRecord).order_by(desc(ExecutionRecord.created_at), desc(ExecutionRecord.id))
    if binding_id is not None:
        stmt = stmt.where(ExecutionRecord.binding_id == binding_id)
    if status_filter:
        stmt = stmt.where(ExecutionRecord.status == status_filter)
    rows = list((await db.scalars(stmt.offset(offset).limit(limit))).all())
    # 总数使用相同过滤条件，不受分页影响。
    total = await db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    return {
        "total": total,
        "items": [
            {
                "id": r.id,
                "bindingId": r.binding_id,
                "leadId": r.lead_id,
                "accountId": r.account_id,
                "leadName": r.lead_name,
                "accountName": r.account_name,
                "symbol": r.symbol,
                "instId": r.inst_id,
                "side": r.side,
                "positionSide": r.position_side,
                "action": r.action,
                "status": r.status,
                "leadQty": r.lead_qty,
                "leadPrice": r.lead_price,
                "leadNotional": r.lead_notional,
                "myQty": r.my_qty,
                "myPrice": r.my_price,
                "myNotional": r.my_notional,
                "slippagePct": r.slippage_pct,
                "spreadPct": r.spread_pct,
                "orderId": r.order_id,
                "details": redact_error_urls(r.details),
                "createdAt": to_utc_iso(r.created_at),
            }
            for r in rows
        ]
    }


@router.get("/public")
async def public_lead_feed(db: AsyncSession = Depends(get_db)):
    """Public unauthenticated endpoint strictly exposing opted-in lead snapshots (never private accounts)."""
    cfg_row = await db.scalar(select(SystemConfig).where(SystemConfig.key == "public_monitoring_enabled"))
    # 缺失、损坏或非布尔配置都按未授权公开处理。
    try:
        enabled = json.loads(cfg_row.value_json) is True if cfg_row else False
    except (TypeError, ValueError):
        enabled = False
    if not enabled:
        return {"enabled": False, "leads": []}

    leads = list(
        (
            await db.scalars(
                select(LeadSource).where(
                    LeadSource.is_enabled == True,  # noqa: E712
                    LeadSource.is_public_dashboard == True,  # noqa: E712
                )
            )
        ).all()
    )
    out = []
    for lead in leads:
        snap = json.loads(lead.snapshot_json) if lead.snapshot_json else None
        out.append(
            {
                "id": lead.id,
                "name": lead.name,
                "platform": lead.platform,
                "portfolioId": lead.portfolio_id,
                "marginBalance": lead.margin_balance,
                "aumAmount": lead.aum_amount,
                **lead_freshness(lead, snap or {}),
                "snapshot": mask_sensitive_payload(snap),
            }
        )
    return {"enabled": True, "leads": out}
