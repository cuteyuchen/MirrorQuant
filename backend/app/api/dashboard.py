from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.core.secret_mask import redact_proxy_userinfo
from app.models.domain import (
    CopyBinding,
    ExecutionRecord,
    LeadSource,
    SystemConfig,
    TradingAccount,
    User,
)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/overview")
async def dashboard_overview(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    leads = list((await db.scalars(select(LeadSource).order_by(LeadSource.id))).all())
    accounts = list((await db.scalars(select(TradingAccount).order_by(TradingAccount.id))).all())
    bindings = list((await db.scalars(select(CopyBinding).order_by(CopyBinding.id))).all())
    recent_execs = list(
        (await db.scalars(select(ExecutionRecord).order_by(desc(ExecutionRecord.created_at)).limit(30))).all()
    )
    ks_row = await db.scalar(select(SystemConfig).where(SystemConfig.key == "global_kill_switch"))
    global_kill = json.loads(ks_row.value_json) if ks_row else False

    total_equity = sum(a.usdt_equity for a in accounts if a.is_enabled)
    total_avail = sum(a.avail_equity for a in accounts if a.is_enabled)
    total_notional = sum(a.total_notional for a in accounts if a.is_enabled)
    total_upl = sum(a.unrealized_pnl for a in accounts if a.is_enabled)
    total_hwm = sum(a.hwm_equity for a in accounts if a.is_enabled)

    lead_positions = []
    for lead in leads:
        if not lead.snapshot_json:
            continue
        try:
            snap = json.loads(lead.snapshot_json)
            for p in snap.get("openPositions", []):
                lead_positions.append({"leadId": lead.id, "leadName": lead.name, **p})
        except Exception:
            pass

    follower_positions = []
    for acct in accounts:
        if not acct.positions_json:
            continue
        try:
            for p in json.loads(acct.positions_json):
                follower_positions.append(
                    {
                        "accountId": acct.id,
                        "accountName": acct.name,
                        "exchange": acct.exchange,
                        "environment": acct.environment,
                        **p,
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
        "leadPositions": lead_positions,
        "followerPositions": follower_positions,
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
                "orderId": r.order_id,
                "details": redact_proxy_userinfo(r.details),
                "createdAt": r.created_at.isoformat(),
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
    _user: User = Depends(get_current_user),
):
    stmt = select(ExecutionRecord).order_by(desc(ExecutionRecord.created_at))
    if binding_id is not None:
        stmt = stmt.where(ExecutionRecord.binding_id == binding_id)
    if status_filter:
        stmt = stmt.where(ExecutionRecord.status == status_filter)
    rows = list((await db.scalars(stmt.offset(offset).limit(limit))).all())
    return {
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
                "orderId": r.order_id,
                "details": redact_proxy_userinfo(r.details),
                "createdAt": r.created_at.isoformat(),
            }
            for r in rows
        ]
    }


@router.get("/public")
async def public_lead_feed(db: AsyncSession = Depends(get_db)):
    """Public unauthenticated endpoint strictly exposing opted-in lead snapshots (never private accounts)."""
    cfg_row = await db.scalar(select(SystemConfig).where(SystemConfig.key == "public_monitoring_enabled"))
    enabled = json.loads(cfg_row.value_json) if cfg_row else True
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
                "snapshot": snap,
            }
        )
    return {"enabled": True, "leads": out}
