"""Rule-based account anomaly evaluation and lifecycle state persistence."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
)
from app.services.account_detail.serializers import sanitize_error_text
from app.services.engine.equity import risk_state


def _make_alert(
    code: str,
    severity: str,
    message_key: str,
    *,
    scope_key: str = "",
    params: Optional[Dict[str, Any]] = None,
    evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "code": code,
        "scopeKey": scope_key,
        "severity": severity,
        "messageKey": message_key,
        "params": params or {},
        "evidence": evidence or {},
    }


def evaluate_account_alerts(
    *,
    account: TradingAccount,
    risk_overview: Dict[str, Any],
    positions_summary: Dict[str, Any],
    pnl_breakdown: Optional[Dict[str, Any]],
    data_quality: Dict[str, Any],
    bindings: Sequence[CopyBinding],
    sync_states: Sequence[AccountDatasetSyncState],
    open_order_snap: Optional[AccountOpenOrderSnapshot],
    recent_force_orders: Sequence[AccountOrderHistory],
    recent_liq_cashflows: Sequence[AccountCashflowEntry],
    global_kill_switch: bool = False,
    unknown_attribution_count: int = 0,
    attribution_conflicts: Optional[Sequence[Dict[str, Any]]] = None,
    now_ms: int,
) -> List[Dict[str, Any]]:
    alerts: List[Dict[str, Any]] = []

    # 1. Global emergency kill switch
    if global_kill_switch:
        alerts.append(
            _make_alert(
                "GLOBAL_HALT_ACTIVE",
                "critical",
                "accountDetail.alert.globalKillSwitch",
            )
        )

    # 2. Account connectivity / auth errors
    if account.status == "error" or account.last_error:
        alerts.append(
            _make_alert(
                "ACCOUNT_SYNC_ERROR",
                "critical",
                "accountDetail.alert.syncError",
                params={"error": sanitize_error_text(account.last_error) or account.status},
                evidence={"status": account.status},
            )
        )

    # 3. Account sync freshness (>90s warning, >300s critical)
    sync_ms = data_quality.get("accountSyncedAtMs")
    if account.is_enabled:
        if sync_ms is None:
            alerts.append(
                _make_alert(
                    "ACCOUNT_SYNC_STALE",
                    "warning",
                    "accountDetail.alert.neverSynced",
                )
            )
        else:
            age_ms = max(0, now_ms - sync_ms)
            if age_ms > 300_000:
                alerts.append(
                    _make_alert(
                        "ACCOUNT_SYNC_STALE",
                        "critical",
                        "accountDetail.alert.syncStaleCritical",
                        params={"seconds": age_ms // 1000},
                        evidence={"lastSyncMs": sync_ms},
                    )
                )
            elif age_ms > 90_000:
                alerts.append(
                    _make_alert(
                        "ACCOUNT_SYNC_STALE",
                        "warning",
                        "accountDetail.alert.syncStaleWarning",
                        params={"seconds": age_ms // 1000},
                        evidence={"lastSyncMs": sync_ms},
                    )
                )

    # 4. Unverified legacy equity baseline
    eq_state = risk_state(account)
    if eq_state.get("legacy_unverified"):
        alerts.append(
            _make_alert(
                "LEGACY_EQUITY_UNVERIFIED",
                "warning",
                "accountDetail.alert.legacyEquityUnverified",
            )
        )

    # 5. Clock offset drift
    clock_offset = int(getattr(account, "clock_offset_ms", 0) or 0)
    if abs(clock_offset) > 1500:
        alerts.append(
            _make_alert(
                "CLOCK_OFFSET_DRIFT",
                "warning",
                "accountDetail.alert.clockDrift",
                params={"offsetMs": clock_offset},
                evidence={"clockOffsetMs": clock_offset},
            )
        )

    # 6. API trade permission & account position-mode mismatch
    if risk_overview.get("canTrade") is False:
        alerts.append(
            _make_alert(
                "ACCOUNT_CANNOT_TRADE",
                "critical",
                "accountDetail.alert.cannotTrade",
            )
        )
    dual_side = risk_overview.get("dualSidePosition")
    if dual_side is not None:
        expected_dual = (account.position_mode or "hedge").lower() == "hedge"
        if bool(dual_side) != expected_dual:
            alerts.append(
                _make_alert(
                    "POSITION_MODE_MISMATCH",
                    "critical",
                    "accountDetail.alert.positionModeMismatch",
                    params={
                        "configured": account.position_mode,
                        "exchange": "hedge" if dual_side else "one_way",
                    },
                    evidence={"configured": account.position_mode, "dualSidePosition": bool(dual_side)},
                )
            )

    # 7. Missing cashflow causing unknown today PnL
    if sync_ms is not None and risk_overview.get("todayPnl") is None:
        alerts.append(
            _make_alert(
                "TODAY_CASHFLOW_UNKNOWN",
                "warning",
                "accountDetail.alert.todayCashflowUnknown",
            )
        )

    # 8. Margin ratio risk (50% warning, 80% critical)
    margin_ratio = risk_overview.get("marginRatio")
    if margin_ratio is not None:
        if margin_ratio >= 0.80:
            alerts.append(
                _make_alert(
                    "HIGH_MARGIN_RATIO",
                    "critical",
                    "accountDetail.alert.marginRatioCritical",
                    params={"pct": round(margin_ratio * 100, 2)},
                    evidence={"marginRatio": margin_ratio},
                )
            )
        elif margin_ratio >= 0.50:
            alerts.append(
                _make_alert(
                    "HIGH_MARGIN_RATIO",
                    "warning",
                    "accountDetail.alert.marginRatioWarning",
                    params={"pct": round(margin_ratio * 100, 2)},
                    evidence={"marginRatio": margin_ratio},
                )
            )

    # 9. Liquidation proximity (<10% warning, <5% critical) & ADL per position
    for pos in positions_summary.get("positions", []):
        scope = f"{pos['instId']}:{pos['posSide']}"
        liq_dist = pos.get("liqDistancePct")
        if liq_dist is not None:
            if liq_dist < 5.0:
                alerts.append(
                    _make_alert(
                        "LIQUIDATION_PROXIMITY",
                        "critical",
                        "accountDetail.alert.liqProximityCritical",
                        scope_key=scope,
                        params={"symbol": pos["symbol"], "pct": liq_dist},
                        evidence={"instId": pos["instId"], "liqDistancePct": liq_dist},
                    )
                )
            elif liq_dist < 10.0:
                alerts.append(
                    _make_alert(
                        "LIQUIDATION_PROXIMITY",
                        "warning",
                        "accountDetail.alert.liqProximityWarning",
                        scope_key=scope,
                        params={"symbol": pos["symbol"], "pct": liq_dist},
                        evidence={"instId": pos["instId"], "liqDistancePct": liq_dist},
                    )
                )

        adl = pos.get("adlQuantile")
        if adl is not None and adl >= 4:
            alerts.append(
                _make_alert(
                    "HIGH_ADL_QUANTILE",
                    "critical" if adl >= 5 else "warning",
                    "accountDetail.alert.highAdl",
                    scope_key=scope,
                    params={"symbol": pos["symbol"], "quantile": adl},
                    evidence={"instId": pos["instId"], "adlQuantile": adl},
                )
            )

    # 10. Circuit breaker & consecutive execution failures
    for b in bindings:
        if b.circuit_breaker_tripped:
            alerts.append(
                _make_alert(
                    "CIRCUIT_BREAKER_TRIPPED",
                    "critical",
                    "accountDetail.alert.circuitBreakerTripped",
                    scope_key=f"binding:{b.id}",
                    params={"binding": b.name, "reason": sanitize_error_text(b.circuit_breaker_reason) or ""},
                    evidence={"bindingId": b.id},
                )
            )
        if b.consecutive_failures >= 3:
            alerts.append(
                _make_alert(
                    "CONSECUTIVE_EXECUTION_FAILURES",
                    "critical" if b.consecutive_failures >= 5 else "warning",
                    "accountDetail.alert.consecutiveFailures",
                    scope_key=f"binding:{b.id}",
                    params={"binding": b.name, "count": b.consecutive_failures},
                    evidence={"bindingId": b.id, "consecutiveFailures": b.consecutive_failures},
                )
            )

    # 11. Open orders incomplete or stale when positions exist
    positions = positions_summary.get("positions", [])
    prot_sum = positions_summary.get("protectionSummary") or {}
    if positions and not prot_sum.get("openOrdersKnown"):
        alerts.append(
            _make_alert(
                "OPEN_ORDERS_INCOMPLETE",
                "warning",
                "accountDetail.alert.openOrdersIncomplete",
                evidence={"positionCount": len(positions), "status": prot_sum.get("openOrdersStatus")},
            )
        )

    # 12. Position SL protection missing, <95%, over-covered, manual, or orphan risk
    for pos in positions:
        prot = pos.get("protection", {})
        p_status = prot.get("status")
        scope = f"{pos['instId']}:{pos['posSide']}"
        sl_cov = prot.get("slCoveragePct")
        if p_status == "unprotected":
            sev = "critical" if pos.get("requiresProtection") else "warning"
            alerts.append(
                _make_alert(
                    "POSITION_UNPROTECTED",
                    sev,
                    "accountDetail.alert.positionUnprotected",
                    scope_key=scope,
                    params={"symbol": pos["symbol"], "side": pos["posSide"]},
                    evidence={"instId": pos["instId"], "posSide": pos["posSide"], "notionalUsd": pos.get("notionalUsd")},
                )
            )
        elif p_status == "partial" or (sl_cov is not None and 0 < sl_cov < 95.0):
            alerts.append(
                _make_alert(
                    "LOW_SL_COVERAGE",
                    "warning",
                    "accountDetail.alert.positionPartialProtection",
                    scope_key=scope,
                    params={"symbol": pos["symbol"], "pct": sl_cov or 0},
                    evidence={"instId": pos["instId"], "slCoveragePct": sl_cov},
                )
            )

        if prot.get("overCovered"):
            alerts.append(
                _make_alert(
                    "OVER_COVERED_PROTECTION",
                    "warning",
                    "accountDetail.alert.overCoveredProtection",
                    scope_key=scope,
                    params={"symbol": pos["symbol"]},
                    evidence={"instId": pos["instId"], "posSide": pos["posSide"]},
                )
            )

        u_class = pos.get("unallocatedClass")
        if u_class == "orphan_risk":
            alerts.append(
                _make_alert(
                    "ORPHAN_POSITION_RISK",
                    "critical",
                    "accountDetail.alert.orphanPositionRisk",
                    scope_key=scope,
                    params={"symbol": pos["symbol"]},
                    evidence={"instId": pos["instId"], "unallocatedQty": pos.get("unallocatedQty")},
                )
            )
        elif u_class in ("manual", "unallocated") and pos.get("ownership") in ("manual", "mixed"):
            unalloc_notional = None
            qty = pos.get("qty") or 0.0
            if qty > 0 and pos.get("notionalUsd") is not None and pos.get("unallocatedQty") is not None:
                unalloc_notional = abs(pos["notionalUsd"]) * (pos["unallocatedQty"] / qty)
            if unalloc_notional is None or unalloc_notional >= 1.0:
                alerts.append(
                    _make_alert(
                        "MANUAL_EXPOSURE_PRESENT",
                        "warning",
                        "accountDetail.alert.manualExposure",
                        scope_key=scope,
                        params={"symbol": pos["symbol"], "ownership": pos["ownership"]},
                        evidence={
                            "instId": pos["instId"],
                            "ownership": pos["ownership"],
                            "unallocatedQty": pos.get("unallocatedQty"),
                        },
                    )
                )

    # 13. Orphan / over-allocated allocation ledger drift
    orphans = positions_summary.get("orphanAllocations", [])
    over_allocs = [p for p in positions if p.get("ownership") == "drift"]
    if orphans or over_allocs:
        alerts.append(
            _make_alert(
                "ALLOCATION_DRIFT",
                "warning",
                "accountDetail.alert.allocationDrift",
                params={"count": len(orphans) + len(over_allocs)},
                evidence={"orphans": [{"instId": o["instId"], "binding": o["bindingName"]} for o in orphans[:5]]},
            )
        )

    # 14. Recent force liquidation order or liquidation fee within 7d
    cutoff_7d = now_ms - 7 * 86_400_000
    recent_forces = [o for o in recent_force_orders if o.updated_at_ms >= cutoff_7d]
    recent_liq_cfs = [c for c in recent_liq_cashflows if c.occurred_at_ms >= cutoff_7d]
    if recent_forces or recent_liq_cfs:
        alerts.append(
            _make_alert(
                "FORCE_LIQUIDATION_DETECTED",
                "critical",
                "accountDetail.alert.forceLiquidation",
                params={"count": max(len(recent_forces), len(recent_liq_cfs))},
                evidence={
                    "forceOrderCount": len(recent_forces),
                    "liquidationCashflowCount": len(recent_liq_cfs),
                },
            )
        )

    # 15. Dataset sync gaps / unsupported banners / ledger mismatch / attribution ambiguity
    for ds_name, ds_info in (data_quality.get("datasets") or {}).items():
        st = ds_info.get("status")
        if st == "unsupported":
            alerts.append(
                _make_alert(
                    "DATASET_UNSUPPORTED",
                    "info",
                    "accountDetail.alert.datasetUnsupported",
                    scope_key=ds_name,
                    params={"dataset": ds_name, "error": ds_info.get("errorMessage") or ""},
                    evidence={"dataset": ds_name},
                )
            )
        elif st in ("unavailable", "rate_limited", "partial", "stale") and ds_name in (
            "equity",
            "open_orders",
            "cashflow",
            "fills",
            "orders",
        ):
            alerts.append(
                _make_alert(
                    "DATASET_SYNC_GAP",
                    "warning",
                    "accountDetail.alert.datasetGap",
                    scope_key=ds_name,
                    params={"dataset": ds_name, "status": st},
                    evidence={"dataset": ds_name, "status": st, "errorCode": ds_info.get("errorCode")},
                )
            )

    if pnl_breakdown and pnl_breakdown.get("reconciled") is False:
        alerts.append(
            _make_alert(
                "LEDGER_RECONCILIATION_MISMATCH",
                "warning",
                "accountDetail.alert.ledgerMismatch",
                params={"residual": pnl_breakdown.get("reconciliationDelta")},
                evidence={"residual": pnl_breakdown.get("reconciliationDelta"), "range": pnl_breakdown.get("range")},
            )
        )

    if attribution_conflicts or unknown_attribution_count > 0:
        alerts.append(
            _make_alert(
                "ATTRIBUTION_AMBIGUOUS",
                "warning",
                "accountDetail.alert.attributionAmbiguous",
                params={"unknownCount": unknown_attribution_count, "conflictCount": len(attribution_conflicts or [])},
                evidence={"unknownCount": unknown_attribution_count, "conflictCount": len(attribution_conflicts or [])},
            )
        )

    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    alerts.sort(key=lambda a: (severity_rank.get(a["severity"], 9), a["code"], a["scopeKey"]))
    return alerts


async def persist_account_alerts(
    db: AsyncSession,
    account_id: int,
    *,
    now_ms: Optional[int] = None,
    attribution_conflicts: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Evaluate alerts cleanly from current DB state and update AccountAlertState lifecycle rows."""
    from app.services.account_detail.analytics import (
        compute_data_quality_summary,
        compute_pnl_breakdown,
        compute_positions_and_protection,
        compute_risk_overview,
    )

    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    account = await db.get(TradingAccount, account_id)
    if account is None:
        return []

    sync_states = list(
        (await db.scalars(select(AccountDatasetSyncState).where(AccountDatasetSyncState.account_id == account_id))).all()
    )
    open_order_snap = await db.scalar(
        select(AccountOpenOrderSnapshot).where(AccountOpenOrderSnapshot.account_id == account_id)
    )
    bindings = list(
        (
            await db.scalars(
                select(CopyBinding).where(CopyBinding.account_id == account_id, CopyBinding.deleted_at.is_(None))
            )
        ).all()
    )
    lead_ids = [b.lead_id for b in bindings]
    leads_by_id: Dict[int, LeadSource] = {}
    if lead_ids:
        leads_by_id = {
            l.id: l
            for l in (await db.scalars(select(LeadSource).where(LeadSource.id.in_(lead_ids)))).all()
        }
    binding_ids = [b.id for b in bindings]
    guards = (
        list((await db.scalars(select(PositionGuardState).where(PositionGuardState.binding_id.in_(binding_ids)))).all())
        if binding_ids
        else []
    )
    allocations = list(
        (await db.scalars(select(PositionAllocation).where(PositionAllocation.account_id == account_id))).all()
    )
    intents = list((await db.scalars(select(OrderIntent).where(OrderIntent.account_id == account_id))).all())
    executions = list((await db.scalars(select(ExecutionRecord).where(ExecutionRecord.account_id == account_id))).all())
    orders_history = list(
        (
            await db.scalars(
                select(AccountOrderHistory)
                .where(AccountOrderHistory.account_id == account_id)
                .order_by(AccountOrderHistory.updated_at_ms.desc())
                .limit(200)
            )
        ).all()
    )
    fills = list(
        (
            await db.scalars(
                select(AccountTradeFill)
                .where(AccountTradeFill.account_id == account_id)
                .order_by(AccountTradeFill.occurred_at_ms.desc())
                .limit(500)
            )
        ).all()
    )
    latest_usdt_snap = await db.scalar(
        select(AccountEquitySnapshot)
        .where(
            AccountEquitySnapshot.account_id == account_id,
            AccountEquitySnapshot.currency == "USDT",
        )
        .order_by(AccountEquitySnapshot.captured_at_ms.desc())
        .limit(1)
    )
    latest_usdc_snap = await db.scalar(
        select(AccountEquitySnapshot)
        .where(
            AccountEquitySnapshot.account_id == account_id,
            AccountEquitySnapshot.currency == "USDC",
        )
        .order_by(AccountEquitySnapshot.captured_at_ms.desc())
        .limit(1)
    )
    ks_row = await db.get(SystemConfig, "global_kill_switch")
    global_kill = False
    if ks_row and ks_row.value_json:
        try:
            global_kill = bool(json.loads(ks_row.value_json))
        except (ValueError, TypeError):
            global_kill = False

    positions_summary = compute_positions_and_protection(
        account, allocations, bindings, leads_by_id, guards, open_order_snap, intents, executions, orders_history, fills, sync_states, now_ms
    )
    risk_overview = compute_risk_overview(account, latest_usdt_snap, latest_usdc_snap, positions_summary, sync_states, now_ms)
    data_quality = compute_data_quality_summary(sync_states, open_order_snap, account, now_ms)
    pnl_30d = await compute_pnl_breakdown(db, account, "30d", now_ms, sync_states, bindings)

    cutoff_7d = now_ms - 7 * 86_400_000
    recent_forces = list(
        (
            await db.scalars(
                select(AccountOrderHistory).where(
                    AccountOrderHistory.account_id == account_id,
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
                    AccountCashflowEntry.account_id == account_id,
                    AccountCashflowEntry.category.in_(("LIQUIDATION_FEE", "INSURANCE_CLEAR")),
                    AccountCashflowEntry.occurred_at_ms >= cutoff_7d,
                )
            )
        ).all()
    )
    unknown_fills_cnt = len(
        list(
            (
                await db.scalars(
                    select(AccountTradeFill.id).where(
                        AccountTradeFill.account_id == account_id,
                        AccountTradeFill.source_class == "unknown",
                    )
                )
            ).all()
        )
    )

    evaluated = evaluate_account_alerts(
        account=account,
        risk_overview=risk_overview,
        positions_summary=positions_summary,
        pnl_breakdown=pnl_30d,
        data_quality=data_quality,
        bindings=bindings,
        sync_states=sync_states,
        open_order_snap=open_order_snap,
        recent_force_orders=recent_forces,
        recent_liq_cashflows=recent_liq_cfs,
        global_kill_switch=global_kill,
        unknown_attribution_count=unknown_fills_cnt,
        attribution_conflicts=attribution_conflicts,
        now_ms=now_ms,
    )

    existing_rows = {
        (row.code, row.scope_key): row
        for row in (
            await db.scalars(select(AccountAlertState).where(AccountAlertState.account_id == account_id))
        ).all()
    }
    active_keys: set[Tuple[str, str]] = set()
    for al in evaluated:
        key = (al["code"], al["scopeKey"])
        active_keys.add(key)
        p_json = json.dumps(al["params"], ensure_ascii=False, separators=(",", ":"))
        e_json = json.dumps(al["evidence"], ensure_ascii=False, separators=(",", ":"))
        row = existing_rows.get(key)
        if row is None:
            row = AccountAlertState(
                account_id=account_id,
                code=al["code"],
                scope_key=al["scopeKey"],
                severity=al["severity"],
                status="active",
                first_seen_at_ms=now_ms,
                last_seen_at_ms=now_ms,
                resolved_at_ms=None,
                message_key=al["messageKey"],
                params_json=p_json,
                evidence_json=e_json,
            )
            db.add(row)
        else:
            row.severity = al["severity"]
            row.status = "active"
            row.last_seen_at_ms = now_ms
            row.resolved_at_ms = None
            row.message_key = al["messageKey"]
            row.params_json = p_json
            row.evidence_json = e_json
        al["firstSeenAtMs"] = row.first_seen_at_ms
        al["lastSeenAtMs"] = row.last_seen_at_ms

    for key, row in existing_rows.items():
        if key not in active_keys and row.status == "active":
            row.status = "resolved"
            row.resolved_at_ms = now_ms

    await db.flush()
    return evaluated
