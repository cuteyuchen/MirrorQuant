from __future__ import annotations

from app.core.time_util import to_utc_iso

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, get_current_user, owner_filter
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
async def dashboard_board(db: AsyncSession = Depends(get_db), _user: User = Depends(get_current_user)):
    # 管理员只读快照投影，不在 HTTP 请求中触发交易所访问；普通用户只看自己的账户与绑定。
    owner = None if _user.role == "admin" else _user.id
    return await build_board(db, owner_user_id=owner)


@router.get("/intents")
async def list_intents(
    state: str = Query(default="UNKNOWN,SUBMITTING", max_length=120),
    db: AsyncSession = Depends(get_db), _user: User = Depends(get_current_user),
):
    # 状态白名单避免含糊过滤；时间与其余新接口统一为毫秒。
    states = {value.strip().upper() for value in state.split(",") if value.strip()}
    if not states or not states <= {"UNKNOWN", "SUBMITTING", "NEW", "PARTIALLY_FILLED", "RESTING"}:
        raise HTTPException(status_code=422, detail="Only nonterminal intent states are supported")
    stmt = (select(OrderIntent, CopyBinding, TradingAccount)
        .outerjoin(CopyBinding, CopyBinding.id == OrderIntent.binding_id)
        .outerjoin(TradingAccount, TradingAccount.id == OrderIntent.account_id)
        .where(func.upper(OrderIntent.state).in_(states))
        .order_by(OrderIntent.created_at.desc(), OrderIntent.id.desc()))
    if _user.role != "admin":
        stmt = stmt.where(TradingAccount.owner_user_id == _user.id)
    rows = (await db.execute(stmt)).all()
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
    leads = list((await db.scalars(select(LeadSource).where(
        LeadSource.deleted_at.is_(None)).order_by(LeadSource.id))).all())
    # 普通用户只看自己的账户与绑定；带单员本身对所有登录用户可见。
    accounts = list((await db.scalars(select(TradingAccount).where(
        TradingAccount.deleted_at.is_(None),
        owner_filter(_user, TradingAccount.owner_user_id)).order_by(TradingAccount.id))).all())
    bindings = list((await db.scalars(select(CopyBinding).where(
        CopyBinding.deleted_at.is_(None),
        owner_filter(_user, CopyBinding.owner_user_id)).order_by(CopyBinding.id))).all())
    account_ids = {a.id for a in accounts}
    exec_stmt = select(ExecutionRecord).order_by(desc(ExecutionRecord.created_at)).limit(30)
    if _user.role != "admin":
        exec_stmt = exec_stmt.where(ExecutionRecord.account_id.in_(account_ids))
    recent_execs = list((await db.scalars(exec_stmt)).all())
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


_EXEC_SUCCESS_STATUSES = ("EXECUTED", "FILLED", "SUCCESS", "ADOPTED_POSITION")
_EXEC_FAILED_STATUSES = ("FAILED", "REJECTED", "UNFILLED")
_EXEC_SKIPPED_STATUSES = (
    "SKIPPED",
    "SKIPPED_DISABLED",
    "SKIPPED_STALE",
    "SKIPPED_UNSUPPORTED",
    "SKIPPED_CLOSED_CYCLE",
    "SKIPPED_NO_LOCAL_POS",
    "SKIPPED_BELOW_MIN",
)
_EXEC_BLOCKED_STATUSES = (
    "BLOCKED_RISK",
    "BLOCKED_KILL_SWITCH",
    "BLOCKED_SOURCE_ORDERING",
    "BLOCKED_COLLATERAL_BALANCE",
    "BLOCKED_FILTER",
    "BLOCKED_ALLOCATION_MISMATCH",
    "BLOCKED_DRAWDOWN",
    "BLOCKED_SPREAD",
    "BLOCKED_SLIPPAGE",
    "BLOCKED_CATCHUP_DRIFT",
    "BLOCKED_RESTING_LIMIT",
    "BLOCKED_LEVERAGE",
    "BLOCKED_INSUFFICIENT_MARGIN",
    "BLOCKED_ALLOCATION_DEFICIT",
    "BLOCKED_ORPHAN_CLOSE_EVIDENCE",
    "BLOCKED_ORPHAN_DETECTED",
    "BLOCKED_PROTECTION_UNAVAILABLE",
)


def _exec_status_group_clause(status_group: str):
    if status_group == "success":
        return ExecutionRecord.status.in_(_EXEC_SUCCESS_STATUSES)
    if status_group == "failed":
        return ExecutionRecord.status.in_(_EXEC_FAILED_STATUSES)
    if status_group == "skipped":
        return or_(ExecutionRecord.status.in_(_EXEC_SKIPPED_STATUSES), ExecutionRecord.status.like("SKIPPED%"))
    if status_group == "blocked":
        return or_(ExecutionRecord.status.in_(_EXEC_BLOCKED_STATUSES), ExecutionRecord.status.like("BLOCKED%"))
    if status_group == "actionable":
        return not_(or_(ExecutionRecord.status.in_(_EXEC_SKIPPED_STATUSES), ExecutionRecord.status.like("SKIPPED%")))
    return None


@router.get("/executions")
async def list_executions(
    binding_id: Optional[int] = None,
    status_filter: Optional[str] = Query(default=None, alias="status"),
    status_group: Optional[str] = Query(default=None, pattern="^(all|actionable|success|failed|skipped|blocked)$"),
    include_counts: bool = Query(default=True),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    base_filters = []
    if _user.role != "admin":
        own_account_ids = list((await db.scalars(select(TradingAccount.id).where(
            TradingAccount.owner_user_id == _user.id, TradingAccount.deleted_at.is_(None)))).all())
        base_filters.append(ExecutionRecord.account_id.in_(own_account_ids))
        if binding_id is not None:
            binding = await db.get(CopyBinding, binding_id)
            if not binding or binding.owner_user_id != _user.id:
                raise HTTPException(status_code=403, detail="Not your resource")
    if binding_id is not None:
        base_filters.append(ExecutionRecord.binding_id == binding_id)

    stmt = (
        select(ExecutionRecord, CopyBinding, TradingAccount)
        .outerjoin(CopyBinding, CopyBinding.id == ExecutionRecord.binding_id)
        .outerjoin(TradingAccount, TradingAccount.id == ExecutionRecord.account_id)
        .where(*base_filters)
        .order_by(desc(ExecutionRecord.created_at), desc(ExecutionRecord.id))
    )
    if status_filter:
        stmt = stmt.where(ExecutionRecord.status == status_filter)
    elif status_group and status_group != "all":
        clause = _exec_status_group_clause(status_group)
        if clause is not None:
            stmt = stmt.where(clause)

    rows = (await db.execute(stmt.offset(offset).limit(limit))).all()
    count_stmt = select(func.count(ExecutionRecord.id)).where(*base_filters)
    if status_filter:
        count_stmt = count_stmt.where(ExecutionRecord.status == status_filter)
    elif status_group and status_group != "all":
        clause = _exec_status_group_clause(status_group)
        if clause is not None:
            count_stmt = count_stmt.where(clause)
    total = int(await db.scalar(count_stmt) or 0)

    counts_out = None
    if include_counts:
        group_rows = (await db.execute(
            select(ExecutionRecord.status, func.count(ExecutionRecord.id))
            .where(*base_filters)
            .group_by(ExecutionRecord.status)
        )).all()
        counts_map = {"all": 0, "actionable": 0, "success": 0, "failed": 0, "skipped": 0, "blocked": 0}
        for st_raw, cnt_raw in group_rows:
            cnt = int(cnt_raw or 0)
            st = str(st_raw or "").upper()
            counts_map["all"] += cnt
            is_skipped = st in _EXEC_SKIPPED_STATUSES or st.startswith("SKIPPED")
            if not is_skipped:
                counts_map["actionable"] += cnt
            if st in _EXEC_SUCCESS_STATUSES:
                counts_map["success"] += cnt
            elif st in _EXEC_FAILED_STATUSES:
                counts_map["failed"] += cnt
            elif is_skipped:
                counts_map["skipped"] += cnt
            elif st in _EXEC_BLOCKED_STATUSES or st.startswith("BLOCKED"):
                counts_map["blocked"] += cnt
        counts_out = counts_map

    return {
        "total": total,
        "counts": counts_out,
        "items": [
            {
                "id": r.id,
                "bindingId": r.binding_id,
                "bindingName": b.name if b is not None else None,
                "bindingDeleted": bool(b is not None and b.deleted_at is not None),
                "leadId": r.lead_id,
                "accountId": r.account_id,
                "leadName": r.lead_name,
                "accountName": r.account_name,
                "environment": a.environment if a is not None else None,
                "accountDeleted": bool(a is not None and a.deleted_at is not None),
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
            for r, b, a in rows
        ],
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
                    LeadSource.deleted_at.is_(None),
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
