"""Read-only follower account detail, equity curve, cashflows, trades, orders, performance, and coalesced sync API."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.accounts import _get_owned_account
from app.api.deps import get_current_user, record_audit
from app.core.database import get_db
from app.core.secret_mask import redact_error_urls, redact_proxy_userinfo
from app.core.time_util import to_utc_iso
from app.models.domain import (
    AccountAlertState,
    AccountCashflowEntry,
    AccountDatasetSyncState,
    AccountEquitySnapshot,
    AccountOpenOrderSnapshot,
    AccountOrderHistory,
    AccountTradeFill,
    CopyBinding,
    ExecutionRecord,
    LeadSource,
    OrderIntent,
    PositionAllocation,
    PositionGuardState,
    SystemConfig,
    TradingAccount,
    User,
)
from app.services.account_detail import (
    compute_bindings_summary,
    compute_data_quality_summary,
    compute_equity_curve,
    compute_performance,
    compute_pnl_breakdown,
    compute_positions_and_protection,
    compute_risk_overview,
    evaluate_account_alerts,
    format_dataset_quality,
    sync_account_detail,
)
from app.services.account_detail.serializers import (
    decode_cursor,
    decimal_to_float,
    decimal_to_str,
    dt_to_ms,
    encode_cursor,
    finite_float,
    parse_range_window,
    sanitize_error_text,
    sanitize_payload,
)

router = APIRouter(prefix="/api/accounts", tags=["account-detail"])

_TRADE_TYPE_RANK = {"fill": 3, "execution": 2, "order": 1}


def _account_meta_out(a: TradingAccount) -> Dict[str, Any]:
    return {
        "id": a.id,
        "ownerUserId": a.owner_user_id,
        "name": a.name,
        "exchange": a.exchange,
        "environment": a.environment,
        "apiKeyMasked": a.api_key_masked,
        "isEnabled": bool(a.is_enabled),
        "marginMode": a.margin_mode,
        "positionMode": a.position_mode,
        "clockOffsetMs": int(getattr(a, "clock_offset_ms", 0) or 0),
        "status": a.status,
        "lastSyncAt": to_utc_iso(a.last_sync_at),
        "lastSyncAtMs": dt_to_ms(a.last_sync_at),
        "lastError": redact_error_urls(redact_proxy_userinfo(a.last_error)) if a.last_error else None,
        "createdAt": to_utc_iso(a.created_at),
        "readOnly": True,
    }


def _safe_json_obj(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    cleaned = sanitize_payload(parsed)
    return cleaned if isinstance(cleaned, dict) else None


async def _validate_account_binding(
    db: AsyncSession, account_id: int, binding_id: Optional[int]
) -> Optional[CopyBinding]:
    if binding_id is None:
        return None
    binding = await db.get(CopyBinding, binding_id)
    if binding is None or binding.account_id != account_id or binding.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Binding not found for account")
    return binding


@router.get("/{account_id}/detail")
async def get_account_detail(
    account_id: int,
    range: str = Query("30d"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = await _get_owned_account(db, account_id, current_user)
    now_ms = int(time.time() * 1000)
    parse_range_window(range, now_ms)

    sync_states = list(
        (await db.scalars(select(AccountDatasetSyncState).where(AccountDatasetSyncState.account_id == account.id))).all()
    )
    open_order_snap = await db.scalar(
        select(AccountOpenOrderSnapshot).where(AccountOpenOrderSnapshot.account_id == account.id)
    )
    data_quality = compute_data_quality_summary(sync_states, open_order_snap, account, now_ms)
    ds_map = data_quality["datasets"]

    bindings = list(
        (
            await db.scalars(
                select(CopyBinding)
                .where(CopyBinding.account_id == account.id, CopyBinding.deleted_at.is_(None))
                .order_by(CopyBinding.id.asc())
            )
        ).all()
    )
    all_bindings = list((await db.scalars(select(CopyBinding).where(CopyBinding.account_id == account.id))).all())
    lead_ids = list({b.lead_id for b in all_bindings})
    leads_by_id: Dict[int, LeadSource] = {}
    if lead_ids:
        leads_by_id = {
            l.id: l for l in (await db.scalars(select(LeadSource).where(LeadSource.id.in_(lead_ids)))).all()
        }
    binding_ids = [b.id for b in bindings]
    guards = (
        list((await db.scalars(select(PositionGuardState).where(PositionGuardState.binding_id.in_(binding_ids)))).all())
        if binding_ids
        else []
    )
    allocations = list(
        (await db.scalars(select(PositionAllocation).where(PositionAllocation.account_id == account.id))).all()
    )
    intents = list((await db.scalars(select(OrderIntent).where(OrderIntent.account_id == account.id))).all())
    executions = list((await db.scalars(select(ExecutionRecord).where(ExecutionRecord.account_id == account.id))).all())
    orders_history = list(
        (
            await db.scalars(
                select(AccountOrderHistory)
                .where(AccountOrderHistory.account_id == account.id)
                .order_by(AccountOrderHistory.updated_at_ms.desc())
                .limit(200)
            )
        ).all()
    )
    recent_fills = list(
        (
            await db.scalars(
                select(AccountTradeFill)
                .where(AccountTradeFill.account_id == account.id)
                .order_by(AccountTradeFill.occurred_at_ms.desc())
                .limit(500)
            )
        ).all()
    )
    latest_usdt_snap = await db.scalar(
        select(AccountEquitySnapshot)
        .where(AccountEquitySnapshot.account_id == account.id, AccountEquitySnapshot.currency == "USDT")
        .order_by(AccountEquitySnapshot.captured_at_ms.desc())
        .limit(1)
    )
    latest_usdc_snap = await db.scalar(
        select(AccountEquitySnapshot)
        .where(AccountEquitySnapshot.account_id == account.id, AccountEquitySnapshot.currency == "USDC")
        .order_by(AccountEquitySnapshot.captured_at_ms.desc())
        .limit(1)
    )

    positions_section: Dict[str, Any]
    try:
        positions_summary = compute_positions_and_protection(
            account,
            allocations,
            bindings,
            leads_by_id,
            guards,
            open_order_snap,
            intents,
            executions,
            orders_history,
            recent_fills,
            sync_states,
            now_ms,
        )
        positions_section = {
            **positions_summary,
            "dataQuality": format_dataset_quality(ds_map.get("open_orders")),
        }
    except Exception as exc:
        positions_summary = {
            "positionsKnown": False,
            "positions": [],
            "orphanAllocations": [],
            "allocatedNotionalUsdt": None,
            "unallocatedNotionalUsdt": None,
            "protectionSummary": {
                "openOrdersKnown": False,
                "openOrdersStale": True,
                "fetchedAtMs": None,
                "openOrdersStatus": "unavailable",
                "unprotectedCount": None,
                "partialProtectedCount": None,
                "totalOpenOrders": None,
                "byCategory": {},
            },
            "openOrders": [],
        }
        positions_section = {
            **positions_summary,
            "dataQuality": {
                "status": "unavailable",
                "fetchedAt": None,
                "coverageStart": None,
                "coverageEnd": None,
                "errorCode": "ANALYTICS_ERROR",
                "errorMessage": sanitize_error_text(str(exc)),
            },
        }

    try:
        risk_section = {
            **compute_risk_overview(
                account, latest_usdt_snap, latest_usdc_snap, positions_summary, sync_states, now_ms
            ),
            "dataQuality": format_dataset_quality(ds_map.get("equity")),
        }
    except Exception as exc:
        risk_section = {
            "currency": "USDT",
            "riskLevel": "unknown",
            "dataQuality": {
                "status": "unavailable",
                "fetchedAt": None,
                "coverageStart": None,
                "coverageEnd": None,
                "errorCode": "ANALYTICS_ERROR",
                "errorMessage": sanitize_error_text(str(exc)),
            },
        }

    pnl_data: Optional[Dict[str, Any]] = None
    try:
        pnl_data = await compute_pnl_breakdown(db, account, range, now_ms, sync_states, all_bindings)
        pnl_section = {
            **pnl_data,
            "dataQuality": format_dataset_quality(ds_map.get("cashflow")),
        }
    except Exception as exc:
        pnl_section = {
            "currency": "USDT",
            "range": range,
            "dataQuality": {
                "status": "unavailable",
                "fetchedAt": None,
                "coverageStart": None,
                "coverageEnd": None,
                "errorCode": "ANALYTICS_ERROR",
                "errorMessage": sanitize_error_text(str(exc)),
            },
        }

    try:
        bindings_section = {
            "items": compute_bindings_summary(bindings, leads_by_id, positions_summary, pnl_data),
            "dataQuality": {"status": "ok", "fetchedAt": now_ms, "coverageStart": None, "coverageEnd": None, "errorCode": None, "errorMessage": None},
        }
    except Exception as exc:
        bindings_section = {
            "items": [],
            "dataQuality": {"status": "unavailable", "fetchedAt": now_ms, "coverageStart": None, "coverageEnd": None, "errorCode": "ANALYTICS_ERROR", "errorMessage": sanitize_error_text(str(exc))},
        }

    try:
        ks_row = await db.get(SystemConfig, "global_kill_switch")
        global_kill = False
        if ks_row and ks_row.value_json:
            try:
                global_kill = bool(json.loads(ks_row.value_json))
            except (ValueError, TypeError):
                global_kill = False
        cutoff_7d = now_ms - 7 * 86_400_000
        recent_forces = list(
            (
                await db.scalars(
                    select(AccountOrderHistory).where(
                        AccountOrderHistory.account_id == account.id,
                        AccountOrderHistory.category == "force_order",
                        AccountOrderHistory.updated_at_ms >= cutoff_7d,
                    )
                )
            ).all()
        )
        recent_liq_cfs = list(
            (
                await db.scalars(
                    select(AccountCashflowEntry).where(
                        AccountCashflowEntry.account_id == account.id,
                        AccountCashflowEntry.category.in_(("LIQUIDATION_FEE", "INSURANCE_CLEAR")),
                        AccountCashflowEntry.occurred_at_ms >= cutoff_7d,
                    )
                )
            ).all()
        )
        unknown_cnt = len(
            list(
                (
                    await db.scalars(
                        select(AccountTradeFill.id).where(
                            AccountTradeFill.account_id == account.id,
                            AccountTradeFill.source_class == "unknown",
                        )
                    )
                ).all()
            )
        )
        evaluated_alerts = evaluate_account_alerts(
            account=account,
            risk_overview=risk_section,
            positions_summary=positions_summary,
            pnl_breakdown=pnl_data,
            data_quality=data_quality,
            bindings=bindings,
            sync_states=sync_states,
            open_order_snap=open_order_snap,
            recent_force_orders=recent_forces,
            recent_liq_cashflows=recent_liq_cfs,
            global_kill_switch=global_kill,
            unknown_attribution_count=unknown_cnt,
            now_ms=now_ms,
        )
        alert_rows = list(
            (await db.scalars(select(AccountAlertState).where(AccountAlertState.account_id == account.id))).all()
        )
        persisted_map = {(r.code, r.scope_key): r for r in alert_rows}
        for al in evaluated_alerts:
            st_row = persisted_map.get((al["code"], al["scopeKey"]))
            if st_row:
                al["firstSeenAtMs"] = st_row.first_seen_at_ms
                al["lastSeenAtMs"] = st_row.last_seen_at_ms
        recent_resolved = [
            {
                "code": r.code,
                "scopeKey": r.scope_key,
                "severity": r.severity,
                "messageKey": r.message_key,
                "firstSeenAtMs": r.first_seen_at_ms,
                "resolvedAtMs": r.resolved_at_ms,
                "params": _safe_json_obj(r.params_json) or {},
            }
            for r in alert_rows
            if r.status == "resolved" and r.resolved_at_ms is not None and r.resolved_at_ms >= cutoff_7d
        ]
        alerts_section = {
            "active": evaluated_alerts,
            "recentResolved": sorted(recent_resolved, key=lambda x: x["resolvedAtMs"] or 0, reverse=True),
            "criticalCount": sum(1 for a in evaluated_alerts if a["severity"] == "critical"),
            "warningCount": sum(1 for a in evaluated_alerts if a["severity"] == "warning"),
            "infoCount": sum(1 for a in evaluated_alerts if a["severity"] == "info"),
            "dataQuality": {"status": "ok", "fetchedAt": now_ms, "coverageStart": None, "coverageEnd": None, "errorCode": None, "errorMessage": None},
        }
    except Exception as exc:
        alerts_section = {
            "active": [],
            "recentResolved": [],
            "criticalCount": 0,
            "warningCount": 0,
            "infoCount": 0,
            "dataQuality": {"status": "unavailable", "fetchedAt": now_ms, "coverageStart": None, "coverageEnd": None, "errorCode": "ANALYTICS_ERROR", "errorMessage": sanitize_error_text(str(exc))},
        }

    open_orders_summary = {
        **positions_section["protectionSummary"],
        "dataQuality": format_dataset_quality(ds_map.get("open_orders")),
    }

    return {
        "account": _account_meta_out(account),
        "risk": risk_section,
        "pnl": pnl_section,
        "positions": {
            "positionsKnown": positions_section["positionsKnown"],
            "items": positions_section["positions"],
            "orphanAllocations": positions_section["orphanAllocations"],
            "allocatedNotionalUsdt": positions_section["allocatedNotionalUsdt"],
            "unallocatedNotionalUsdt": positions_section["unallocatedNotionalUsdt"],
            "dataQuality": positions_section["dataQuality"],
        },
        "openOrdersSummary": open_orders_summary,
        "bindings": bindings_section,
        "alerts": alerts_section,
        "dataQuality": data_quality,
    }


@router.get("/{account_id}/equity")
async def get_account_equity_curve(
    account_id: int,
    range: str = Query("30d"),
    currency: str = Query("USDT"),
    resolution: str = Query("auto"),
    includeEstimated: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = await _get_owned_account(db, account_id, current_user)
    now_ms = int(time.time() * 1000)
    ccy = (currency or "USDT").strip().upper()
    if ccy not in ("USDT", "USDC"):
        raise HTTPException(status_code=422, detail="Unsupported currency")

    sync_states = list(
        (await db.scalars(select(AccountDatasetSyncState).where(AccountDatasetSyncState.account_id == account.id))).all()
    )
    open_order_snap = await db.scalar(
        select(AccountOpenOrderSnapshot).where(AccountOpenOrderSnapshot.account_id == account.id)
    )
    dq_summary = compute_data_quality_summary(sync_states, open_order_snap, account, now_ms)
    curve = await compute_equity_curve(
        db,
        account.id,
        range,
        now_ms,
        resolution=resolution,
        currency=ccy,
        include_estimated=includeEstimated,
    )
    return {
        **curve,
        "dataQuality": format_dataset_quality(dq_summary["datasets"].get("equity")),
        "cashflowQuality": format_dataset_quality(dq_summary["datasets"].get("cashflow")),
    }


@router.get("/{account_id}/cashflows")
async def get_account_cashflows(
    account_id: int,
    from_ms: Optional[int] = Query(None, alias="from", ge=0),
    to_ms: Optional[int] = Query(None, alias="to", ge=0),
    category: Optional[str] = Query(None),
    asset: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    cursor: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = await _get_owned_account(db, account_id, current_user)
    now_ms = int(time.time() * 1000)

    sync_states = list(
        (await db.scalars(select(AccountDatasetSyncState).where(AccountDatasetSyncState.account_id == account.id))).all()
    )
    open_order_snap = await db.scalar(
        select(AccountOpenOrderSnapshot).where(AccountOpenOrderSnapshot.account_id == account.id)
    )
    dq_summary = compute_data_quality_summary(sync_states, open_order_snap, account, now_ms)

    stmt = select(AccountCashflowEntry).where(AccountCashflowEntry.account_id == account.id)
    if from_ms is not None:
        stmt = stmt.where(AccountCashflowEntry.occurred_at_ms >= from_ms)
    if to_ms is not None:
        stmt = stmt.where(AccountCashflowEntry.occurred_at_ms <= to_ms)
    if category:
        cats = [c.strip().upper() for c in category.split(",") if c.strip()]
        if len(cats) == 1:
            stmt = stmt.where(AccountCashflowEntry.category == cats[0])
        elif cats:
            stmt = stmt.where(AccountCashflowEntry.category.in_(cats))
    if asset:
        stmt = stmt.where(AccountCashflowEntry.asset == asset.strip().upper())
    if symbol:
        stmt = stmt.where(AccountCashflowEntry.symbol == symbol.strip().upper())

    decoded = decode_cursor(cursor)
    if decoded is not None:
        try:
            c_ts = int(decoded["occurredAtMs"])
            c_id = int(decoded["id"])
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail="Invalid pagination cursor") from exc
        stmt = stmt.where(
            or_(
                AccountCashflowEntry.occurred_at_ms < c_ts,
                and_(AccountCashflowEntry.occurred_at_ms == c_ts, AccountCashflowEntry.id < c_id),
            )
        )

    rows = list(
        (
            await db.scalars(
                stmt.order_by(AccountCashflowEntry.occurred_at_ms.desc(), AccountCashflowEntry.id.desc()).limit(limit + 1)
            )
        ).all()
    )
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    next_cursor = (
        encode_cursor({"occurredAtMs": page_rows[-1].occurred_at_ms, "id": page_rows[-1].id})
        if has_more and page_rows
        else None
    )

    return {
        "items": [
            {
                "id": r.id,
                "exchange": r.exchange,
                "externalId": r.external_id,
                "asset": r.asset,
                "rawType": r.raw_type,
                "category": r.category,
                "signedAmount": decimal_to_float(r.signed_amount, precision=8),
                "signedAmountExact": decimal_to_str(r.signed_amount),
                "symbol": r.symbol,
                "occurredAtMs": r.occurred_at_ms,
                "source": r.source,
                "metadata": _safe_json_obj(r.metadata_json),
            }
            for r in page_rows
        ],
        "nextCursor": next_cursor,
        "hasMore": has_more,
        "dataQuality": format_dataset_quality(dq_summary["datasets"].get("cashflow")),
    }


@router.get("/{account_id}/trades")
async def get_account_trades(
    account_id: int,
    from_ms: Optional[int] = Query(None, alias="from", ge=0),
    to_ms: Optional[int] = Query(None, alias="to", ge=0),
    symbol: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    binding_id: Optional[int] = Query(None, alias="bindingId"),
    record_type: Optional[str] = Query(None, alias="recordType"),
    cursor: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = await _get_owned_account(db, account_id, current_user)
    await _validate_account_binding(db, account.id, binding_id)
    now_ms = int(time.time() * 1000)

    norm_source = (source or "").strip().lower() or None
    if norm_source == "all":
        norm_source = None
    if norm_source is not None and norm_source not in ("system", "manual", "unknown"):
        raise HTTPException(status_code=422, detail="Invalid source filter")

    norm_type = (record_type or "").strip().lower() or None
    if norm_type == "all":
        norm_type = None
    if norm_type is not None and norm_type not in _TRADE_TYPE_RANK:
        raise HTTPException(status_code=422, detail="Invalid recordType filter")

    decoded = decode_cursor(cursor)
    c_ts: Optional[int] = None
    c_rank: Optional[int] = None
    c_id: Optional[int] = None
    if decoded is not None:
        try:
            c_ts = int(decoded["occurredAtMs"])
            c_rank = int(decoded["typeRank"])
            c_id = int(decoded["id"])
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail="Invalid pagination cursor") from exc

    all_bindings = list((await db.scalars(select(CopyBinding).where(CopyBinding.account_id == account.id))).all())
    binding_names = {b.id: b.name for b in all_bindings}

    sync_states = list(
        (await db.scalars(select(AccountDatasetSyncState).where(AccountDatasetSyncState.account_id == account.id))).all()
    )
    open_order_snap = await db.scalar(
        select(AccountOpenOrderSnapshot).where(AccountOpenOrderSnapshot.account_id == account.id)
    )
    dq_summary = compute_data_quality_summary(sync_states, open_order_snap, account, now_ms)

    def _cursor_cond(ts_col: Any, id_col: Any, rank: int):
        assert c_ts is not None and c_rank is not None and c_id is not None
        if rank < c_rank:
            return ts_col <= c_ts
        if rank > c_rank:
            return ts_col < c_ts
        return or_(ts_col < c_ts, and_(ts_col == c_ts, id_col < c_id))

    merged: List[Tuple[int, int, int, Dict[str, Any]]] = []
    sym_upper = symbol.strip().upper() if symbol else None

    # 1. Exchange Fills (rank 3)
    if norm_type in (None, "fill"):
        fq = select(AccountTradeFill).where(AccountTradeFill.account_id == account.id)
        if from_ms is not None:
            fq = fq.where(AccountTradeFill.occurred_at_ms >= from_ms)
        if to_ms is not None:
            fq = fq.where(AccountTradeFill.occurred_at_ms <= to_ms)
        if sym_upper:
            fq = fq.where(AccountTradeFill.symbol == sym_upper)
        if norm_source:
            fq = fq.where(AccountTradeFill.source_class == norm_source)
        if binding_id is not None:
            fq = fq.where(AccountTradeFill.binding_id == binding_id)
        if decoded is not None:
            fq = fq.where(_cursor_cond(AccountTradeFill.occurred_at_ms, AccountTradeFill.id, 3))
        fill_rows = list(
            (await db.scalars(fq.order_by(AccountTradeFill.occurred_at_ms.desc(), AccountTradeFill.id.desc()).limit(limit + 1))).all()
        )
        for f in fill_rows:
            merged.append(
                (
                    f.occurred_at_ms,
                    3,
                    f.id,
                    {
                        "recordType": "fill",
                        "id": f.id,
                        "occurredAtMs": f.occurred_at_ms,
                        "symbol": f.symbol,
                        "instId": f.inst_id,
                        "side": f.side,
                        "positionSide": f.position_side,
                        "qty": decimal_to_float(f.qty),
                        "price": decimal_to_float(f.price),
                        "quoteQty": decimal_to_float(f.quote_qty),
                        "realizedPnl": decimal_to_float(f.realized_pnl, precision=6),
                        "fee": decimal_to_float(f.commission_amount, precision=8),
                        "feeAsset": f.commission_asset,
                        "maker": f.maker,
                        "status": "FILLED",
                        "sourceClass": f.source_class,
                        "attributionConfidence": f.attribution_confidence,
                        "bindingId": f.binding_id,
                        "bindingName": binding_names.get(f.binding_id) if f.binding_id else None,
                        "executionId": f.execution_id,
                        "intentId": f.intent_id,
                        "exchangeOrderId": f.exchange_order_id,
                        "exchangeFillId": f.exchange_fill_id,
                    },
                )
            )

    # 2. System Executions (rank 2) - never mislabeled as filled when blocked/failed
    if norm_type in (None, "execution") and norm_source in (None, "system"):
        eq = select(ExecutionRecord).where(ExecutionRecord.account_id == account.id)
        if from_ms is not None:
            eq = eq.where(ExecutionRecord.created_at >= datetime.fromtimestamp(from_ms / 1000, tz=timezone.utc))
        if to_ms is not None:
            eq = eq.where(ExecutionRecord.created_at <= datetime.fromtimestamp(to_ms / 1000, tz=timezone.utc))
        if sym_upper:
            eq = eq.where(ExecutionRecord.symbol == sym_upper)
        if binding_id is not None:
            eq = eq.where(ExecutionRecord.binding_id == binding_id)
        exec_rows = list((await db.scalars(eq.order_by(ExecutionRecord.created_at.desc(), ExecutionRecord.id.desc()).limit((limit + 1) * 2))).all())
        for ex in exec_rows:
            ex_ms = dt_to_ms(ex.created_at) or 0
            if c_ts is not None and c_rank is not None and c_id is not None:
                if (ex_ms, 2, ex.id) >= (c_ts, c_rank, c_id):
                    continue
            merged.append(
                (
                    ex_ms,
                    2,
                    ex.id,
                    {
                        "recordType": "execution",
                        "id": ex.id,
                        "occurredAtMs": ex_ms,
                        "symbol": ex.symbol,
                        "instId": ex.inst_id,
                        "side": ex.side,
                        "positionSide": ex.position_side,
                        "action": ex.action,
                        "qty": finite_float(ex.my_qty),
                        "price": finite_float(ex.my_price),
                        "quoteQty": finite_float(ex.my_notional),
                        "slippagePct": finite_float(ex.slippage_pct),
                        "spreadPct": finite_float(ex.spread_pct),
                        "fee": finite_float(ex.fee),
                        "funding": finite_float(ex.funding),
                        "status": ex.status,
                        "details": sanitize_error_text(ex.details, max_len=300),
                        "sourceClass": "system",
                        "attributionConfidence": "exact",
                        "bindingId": ex.binding_id,
                        "bindingName": binding_names.get(ex.binding_id) if ex.binding_id else None,
                        "leadName": ex.lead_name,
                        "executionId": ex.id,
                        "exchangeOrderId": ex.order_id,
                        "exchangeFillId": ex.exchange_fill_id,
                    },
                )
            )

    # 3. Order History (rank 1)
    if norm_type in (None, "order"):
        oq = select(AccountOrderHistory).where(AccountOrderHistory.account_id == account.id)
        if from_ms is not None:
            oq = oq.where(AccountOrderHistory.updated_at_ms >= from_ms)
        if to_ms is not None:
            oq = oq.where(AccountOrderHistory.updated_at_ms <= to_ms)
        if sym_upper:
            oq = oq.where(AccountOrderHistory.symbol == sym_upper)
        if norm_source:
            oq = oq.where(AccountOrderHistory.source_class == norm_source)
        if binding_id is not None:
            oq = oq.where(AccountOrderHistory.binding_id == binding_id)
        if decoded is not None:
            oq = oq.where(_cursor_cond(AccountOrderHistory.updated_at_ms, AccountOrderHistory.id, 1))
        ord_rows = list(
            (await db.scalars(oq.order_by(AccountOrderHistory.updated_at_ms.desc(), AccountOrderHistory.id.desc()).limit(limit + 1))).all()
        )
        for o in ord_rows:
            merged.append(
                (
                    o.updated_at_ms,
                    1,
                    o.id,
                    {
                        "recordType": "order",
                        "id": o.id,
                        "occurredAtMs": o.updated_at_ms,
                        "createdAtMs": o.created_at_ms,
                        "category": o.category,
                        "symbol": o.symbol,
                        "instId": o.inst_id,
                        "side": o.side,
                        "positionSide": o.position_side,
                        "orderType": o.order_type,
                        "status": o.status,
                        "qty": decimal_to_float(o.qty),
                        "filledQty": decimal_to_float(o.filled_qty),
                        "price": decimal_to_float(o.avg_price or o.limit_price or o.trigger_price),
                        "limitPrice": decimal_to_float(o.limit_price),
                        "triggerPrice": decimal_to_float(o.trigger_price),
                        "reduceOnly": o.reduce_only,
                        "closePosition": o.close_position,
                        "sourceClass": o.source_class,
                        "bindingId": o.binding_id,
                        "bindingName": binding_names.get(o.binding_id) if o.binding_id else None,
                        "executionId": o.execution_id,
                        "intentId": o.intent_id,
                        "exchangeOrderId": o.exchange_order_id,
                        "algoId": o.algo_id,
                    },
                )
            )

    merged.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    has_more = len(merged) > limit
    page = merged[:limit]
    next_cursor = (
        encode_cursor({"occurredAtMs": page[-1][0], "typeRank": page[-1][1], "id": page[-1][2]})
        if has_more and page
        else None
    )

    fills_dq = format_dataset_quality(dq_summary["datasets"].get("fills"))
    return {
        "items": [item[3] for item in page],
        "nextCursor": next_cursor,
        "hasMore": has_more,
        "dataQuality": fills_dq,
        "ordersQuality": format_dataset_quality(dq_summary["datasets"].get("orders")),
    }


@router.get("/{account_id}/orders")
async def get_account_orders(
    account_id: int,
    state: str = Query("open"),
    category: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    binding_id: Optional[int] = Query(None, alias="bindingId"),
    cursor: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = await _get_owned_account(db, account_id, current_user)
    await _validate_account_binding(db, account.id, binding_id)
    now_ms = int(time.time() * 1000)

    req_state = (state or "open").strip().lower()
    if req_state not in ("open", "history"):
        raise HTTPException(status_code=422, detail="state must be open or history")

    norm_cat = (category or "").strip().lower() or None
    sym_upper = symbol.strip().upper() if symbol else None
    norm_source = (source or "").strip().lower() or None
    if norm_source == "all":
        norm_source = None
    if norm_source is not None and norm_source not in ("system", "manual", "unknown"):
        raise HTTPException(status_code=422, detail="Invalid source filter")

    sync_states = list(
        (await db.scalars(select(AccountDatasetSyncState).where(AccountDatasetSyncState.account_id == account.id))).all()
    )
    open_order_snap = await db.scalar(
        select(AccountOpenOrderSnapshot).where(AccountOpenOrderSnapshot.account_id == account.id)
    )
    dq_summary = compute_data_quality_summary(sync_states, open_order_snap, account, now_ms)

    if req_state == "open":
        bindings = list((await db.scalars(select(CopyBinding).where(CopyBinding.account_id == account.id))).all())
        leads_by_id: Dict[int, LeadSource] = {}
        guards: List[PositionGuardState] = []
        allocations: List[PositionAllocation] = []
        intents = list((await db.scalars(select(OrderIntent).where(OrderIntent.account_id == account.id))).all())
        executions = list((await db.scalars(select(ExecutionRecord).where(ExecutionRecord.account_id == account.id))).all())
        orders_history = list(
            (
                await db.scalars(
                    select(AccountOrderHistory)
                    .where(AccountOrderHistory.account_id == account.id)
                    .order_by(AccountOrderHistory.updated_at_ms.desc())
                    .limit(200)
                )
            ).all()
        )
        pos_sum = compute_positions_and_protection(
            account,
            allocations,
            bindings,
            leads_by_id,
            guards,
            open_order_snap,
            intents,
            executions,
            orders_history,
            [],
            sync_states,
            now_ms,
        )
        items = pos_sum["openOrders"]
        if norm_cat:
            items = [i for i in items if i["category"].lower() == norm_cat]
        if sym_upper:
            items = [i for i in items if i["symbol"].upper() == sym_upper]
        if norm_source:
            items = [i for i in items if i["sourceClass"] == norm_source]
        if binding_id is not None:
            items = [i for i in items if i["bindingId"] == binding_id]

        oo_dq = format_dataset_quality(dq_summary["datasets"].get("open_orders"))
        if open_order_snap is None:
            oo_dq["status"] = "unavailable"
        return {
            "state": "open",
            "openOrdersKnown": pos_sum["protectionSummary"]["openOrdersKnown"],
            "openOrdersStale": pos_sum["protectionSummary"]["openOrdersStale"],
            "fetchedAtMs": pos_sum["protectionSummary"]["fetchedAtMs"],
            "items": items[:limit],
            "nextCursor": None,
            "hasMore": len(items) > limit,
            "dataQuality": oo_dq,
        }

    # state == "history"
    all_bindings = list((await db.scalars(select(CopyBinding).where(CopyBinding.account_id == account.id))).all())
    binding_names = {b.id: b.name for b in all_bindings}

    stmt = select(AccountOrderHistory).where(AccountOrderHistory.account_id == account.id)
    if norm_cat:
        cats = [c.strip().lower() for c in norm_cat.split(",") if c.strip()]
        if len(cats) == 1:
            stmt = stmt.where(AccountOrderHistory.category == cats[0])
        elif cats:
            stmt = stmt.where(AccountOrderHistory.category.in_(cats))
    if sym_upper:
        stmt = stmt.where(AccountOrderHistory.symbol == sym_upper)
    if norm_source:
        stmt = stmt.where(AccountOrderHistory.source_class == norm_source)
    if binding_id is not None:
        stmt = stmt.where(AccountOrderHistory.binding_id == binding_id)

    decoded = decode_cursor(cursor)
    if decoded is not None:
        try:
            c_ts = int(decoded["updatedAtMs"])
            c_id = int(decoded["id"])
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail="Invalid pagination cursor") from exc
        stmt = stmt.where(
            or_(
                AccountOrderHistory.updated_at_ms < c_ts,
                and_(AccountOrderHistory.updated_at_ms == c_ts, AccountOrderHistory.id < c_id),
            )
        )

    rows = list(
        (
            await db.scalars(
                stmt.order_by(AccountOrderHistory.updated_at_ms.desc(), AccountOrderHistory.id.desc()).limit(limit + 1)
            )
        ).all()
    )
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    next_cursor = (
        encode_cursor({"updatedAtMs": page_rows[-1].updated_at_ms, "id": page_rows[-1].id})
        if has_more and page_rows
        else None
    )

    return {
        "state": "history",
        "items": [
            {
                "id": o.id,
                "orderId": o.exchange_order_id,
                "clientOrderId": o.client_order_id,
                "algoId": o.algo_id,
                "clientAlgoId": o.client_algo_id,
                "category": o.category,
                "symbol": o.symbol,
                "instId": o.inst_id,
                "side": o.side,
                "positionSide": o.position_side,
                "orderType": o.order_type,
                "status": o.status,
                "qty": decimal_to_float(o.qty),
                "filledQty": decimal_to_float(o.filled_qty),
                "avgPrice": decimal_to_float(o.avg_price),
                "limitPrice": decimal_to_float(o.limit_price),
                "triggerPrice": decimal_to_float(o.trigger_price),
                "reduceOnly": o.reduce_only,
                "closePosition": o.close_position,
                "workingType": o.working_type,
                "sourceClass": o.source_class,
                "bindingId": o.binding_id,
                "bindingName": binding_names.get(o.binding_id) if o.binding_id else None,
                "executionId": o.execution_id,
                "intentId": o.intent_id,
                "createdAtMs": o.created_at_ms,
                "updatedAtMs": o.updated_at_ms,
                "metadata": _safe_json_obj(o.metadata_json),
            }
            for o in page_rows
        ],
        "nextCursor": next_cursor,
        "hasMore": has_more,
        "dataQuality": format_dataset_quality(dq_summary["datasets"].get("orders")),
    }


@router.get("/{account_id}/performance")
async def get_account_performance(
    account_id: int,
    range: str = Query("90d"),
    binding_id: Optional[int] = Query(None, alias="bindingId"),
    symbol: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = await _get_owned_account(db, account_id, current_user)
    await _validate_account_binding(db, account.id, binding_id)
    now_ms = int(time.time() * 1000)

    norm_source = (source or "").strip().lower() or None
    if norm_source == "all":
        norm_source = None
    if norm_source is not None and norm_source not in ("system", "manual", "unknown"):
        raise HTTPException(status_code=422, detail="Invalid source filter")

    perf = await compute_performance(
        db,
        account.id,
        range,
        now_ms,
        binding_id=binding_id,
        symbol=symbol.strip().upper() if symbol else None,
        source_class=norm_source,
    )
    sync_states = list(
        (await db.scalars(select(AccountDatasetSyncState).where(AccountDatasetSyncState.account_id == account.id))).all()
    )
    open_order_snap = await db.scalar(
        select(AccountOpenOrderSnapshot).where(AccountOpenOrderSnapshot.account_id == account.id)
    )
    dq_summary = compute_data_quality_summary(sync_states, open_order_snap, account, now_ms)
    return {
        **perf,
        "dataQuality": format_dataset_quality(dq_summary["datasets"].get("fills")),
    }


@router.post("/{account_id}/detail/sync")
async def sync_account_detail_endpoint(
    account_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = await _get_owned_account(db, account_id, current_user)
    res = await sync_account_detail(db, account.id, manual=True)
    summary = {k: v.get("status") for k, v in (res.get("results") or {}).items()}
    await record_audit(
        db,
        "SYNC_ACCOUNT_DETAIL",
        "trading_account",
        str(account.id),
        json.dumps({"results": summary}, separators=(",", ":")),
        user=current_user,
        request=request,
    )
    await db.commit()
    return res
