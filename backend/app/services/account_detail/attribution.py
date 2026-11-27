"""Attribution of exchange fills/orders to system CopyBindings and ExecutionRecords."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
import json
import time
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.domain import (
    AccountDatasetSyncState,
    AccountOrderHistory,
    AccountTradeFill,
    ExecutionRecord,
    OrderIntent,
)
from app.services.engine.sizer import exact

MANUAL_RECONCILE_GRACE_MS = 120_000
_UNRESOLVED_INTENT_STATES = {"UNKNOWN", "SUBMITTING"}
_TERMINAL_ORDER_STATES = {"FILLED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}
_SUCCESS_EXEC_STATUSES = {"EXECUTED", "FILLED", "SUCCESS"}


def _metadata_dict(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except (ValueError, TypeError):
        return {}


def _is_covered(sync_state: Optional[AccountDatasetSyncState], occurred_at_ms: int) -> bool:
    if sync_state is None or sync_state.status not in ("ok", "partial"):
        return False
    if sync_state.coverage_start_ms is None or sync_state.coverage_end_ms is None:
        return False
    return sync_state.coverage_start_ms <= occurred_at_ms <= sync_state.coverage_end_ms


async def reconcile_attribution(
    db: AsyncSession,
    account_id: int,
    *,
    now_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """Attribute fills and order history rows and reconcile terminal ExecutionRecord fees."""
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)

    intents = list(
        (await db.scalars(select(OrderIntent).where(OrderIntent.account_id == account_id))).all()
    )
    executions = list(
        (await db.scalars(select(ExecutionRecord).where(ExecutionRecord.account_id == account_id))).all()
    )
    orders = list(
        (await db.scalars(select(AccountOrderHistory).where(AccountOrderHistory.account_id == account_id))).all()
    )
    fills = list(
        (await db.scalars(select(AccountTradeFill).where(AccountTradeFill.account_id == account_id))).all()
    )
    sync_states = {
        s.dataset: s
        for s in (
            await db.scalars(
                select(AccountDatasetSyncState).where(AccountDatasetSyncState.account_id == account_id)
            )
        ).all()
    }

    intents_by_id: Dict[str, OrderIntent] = {}
    intents_by_prefix32: Dict[str, List[OrderIntent]] = defaultdict(list)
    intents_by_ex_oid: Dict[str, List[OrderIntent]] = defaultdict(list)
    unresolved_insts: set[str] = set()
    for intent in intents:
        intents_by_id[intent.id] = intent
        intents_by_prefix32[intent.id[:32]].append(intent)
        if intent.exchange_order_id:
            intents_by_ex_oid[intent.exchange_order_id].append(intent)
        if (intent.state or "").upper() in _UNRESOLVED_INTENT_STATES:
            unresolved_insts.add(intent.inst_id.upper())

    execs_by_oid: Dict[str, List[ExecutionRecord]] = defaultdict(list)
    for rec in executions:
        if rec.order_id:
            execs_by_oid[rec.order_id].append(rec)

    orders_by_oid: Dict[str, AccountOrderHistory] = {}
    for ord_row in orders:
        if ord_row.exchange_order_id:
            orders_by_oid[ord_row.exchange_order_id] = ord_row

    # 1. Attribute order history first so clientOrderId links can also help classify fills.
    conflicts: List[Dict[str, Any]] = []

    def _resolve_match(
        ex_oid: Optional[str],
        cl_oid: Optional[str],
        occurred_ms: int,
        inst_id: str,
        symbol: str,
    ) -> Tuple[str, str, Optional[int], Optional[int], Optional[str], Optional[Dict[str, Any]]]:
        matched_intents: List[OrderIntent] = []
        if cl_oid:
            if cl_oid in intents_by_id:
                matched_intents.append(intents_by_id[cl_oid])
            elif cl_oid[:32] in intents_by_prefix32:
                matched_intents.extend(intents_by_prefix32[cl_oid[:32]])
        if ex_oid and ex_oid in intents_by_ex_oid:
            for cand in intents_by_ex_oid[ex_oid]:
                if cand not in matched_intents:
                    matched_intents.append(cand)

        matched_execs = execs_by_oid.get(ex_oid, []) if ex_oid else []

        # Check conflict across intents.
        distinct_intent_bindings = {i.binding_id for i in matched_intents}
        if len(matched_intents) > 1 and (len(distinct_intent_bindings) > 1 or cl_oid is None):
            conflict = {
                "exchangeOrderId": ex_oid,
                "clientOrderId": cl_oid,
                "candidateBindingIds": sorted(distinct_intent_bindings),
                "candidateIntentIds": [i.id for i in matched_intents],
            }
            conflicts.append(conflict)
            return "unknown", "unknown", None, None, None, conflict

        if len(matched_intents) == 1:
            intent = matched_intents[0]
            scoped_execs = [
                e for e in matched_execs
                if e.binding_id == intent.binding_id and (e.status or "").upper() in _SUCCESS_EXEC_STATUSES
            ] or [e for e in matched_execs if e.binding_id == intent.binding_id]
            if len(scoped_execs) > 1:
                scoped_execs.sort(key=lambda e: e.id)
            exec_id = scoped_execs[0].id if scoped_execs else None
            return "system", "exact", intent.binding_id, exec_id, intent.id, None

        # No intent match: check ExecutionRecord by exchange_order_id
        if matched_execs:
            binding_ids = {e.binding_id for e in matched_execs if e.binding_id is not None}
            if len(binding_ids) > 1:
                conflict = {
                    "exchangeOrderId": ex_oid,
                    "clientOrderId": cl_oid,
                    "candidateBindingIds": sorted(binding_ids),
                    "candidateExecutionIds": [e.id for e in matched_execs],
                }
                conflicts.append(conflict)
                return "unknown", "unknown", None, None, None, conflict
            if len(binding_ids) == 1:
                rec = sorted(matched_execs, key=lambda e: e.id)[0]
                return "system", "exact", rec.binding_id, rec.id, None, None

        # Unmatched: check coverage + grace period + no unresolved intent on same instrument.
        fills_cov = _is_covered(sync_states.get("fills"), occurred_ms)
        orders_cov = _is_covered(sync_states.get("orders"), occurred_ms)
        has_unresolved = inst_id.upper() in unresolved_insts or symbol.upper() in unresolved_insts
        if fills_cov and orders_cov and not has_unresolved and (now_ms - occurred_ms) >= MANUAL_RECONCILE_GRACE_MS:
            return "manual", "derived", None, None, None, None
        return "unknown", "unknown", None, None, None, None

    oid_to_client_id: Dict[str, str] = {}
    for ord_row in orders:
        src, _conf, b_id, e_id, i_id, conflict = _resolve_match(
            ord_row.exchange_order_id,
            ord_row.client_order_id,
            ord_row.created_at_ms or ord_row.updated_at_ms,
            ord_row.inst_id,
            ord_row.symbol,
        )
        ord_row.source_class = src
        ord_row.binding_id = b_id
        ord_row.execution_id = e_id
        ord_row.intent_id = i_id
        if ord_row.exchange_order_id and ord_row.client_order_id:
            oid_to_client_id[ord_row.exchange_order_id] = ord_row.client_order_id
        meta = _metadata_dict(ord_row.metadata_json)
        if conflict:
            meta["attributionConflict"] = True
            meta["conflict"] = conflict
        else:
            meta.pop("attributionConflict", None)
            meta.pop("conflict", None)
        ord_row.metadata_json = json.dumps(meta, separators=(",", ":"))

    fills_by_oid: Dict[str, List[AccountTradeFill]] = defaultdict(list)
    for fill in fills:
        cl_id = fill.client_order_id or (oid_to_client_id.get(fill.exchange_order_id) if fill.exchange_order_id else None)
        if cl_id and not fill.client_order_id:
            fill.client_order_id = cl_id
        src, conf, b_id, e_id, i_id, conflict = _resolve_match(
            fill.exchange_order_id,
            cl_id,
            fill.occurred_at_ms,
            fill.inst_id,
            fill.symbol,
        )
        fill.source_class = src
        fill.attribution_confidence = conf
        fill.binding_id = b_id
        fill.execution_id = e_id
        fill.intent_id = i_id
        meta = _metadata_dict(fill.metadata_json)
        if conflict:
            meta["attributionConflict"] = True
            meta["conflict"] = conflict
        else:
            meta.pop("attributionConflict", None)
            meta.pop("conflict", None)
        fill.metadata_json = json.dumps(meta, separators=(",", ":"))
        if fill.exchange_order_id and src == "system":
            fills_by_oid[fill.exchange_order_id].append(fill)

    # Reconcile terminal ExecutionRecord fee & single-fill exchange_fill_id (never touch funding).
    for rec in executions:
        if not rec.order_id or (rec.status or "").upper() not in _SUCCESS_EXEC_STATUSES:
            continue
        order_fills = fills_by_oid.get(rec.order_id, [])
        if not order_fills:
            continue
        ord_hist = orders_by_oid.get(rec.order_id)
        matching_intents = intents_by_ex_oid.get(rec.order_id, [])
        intent_terminal = (
            len(matching_intents) == 1
            and (matching_intents[0].state or "").upper() in _TERMINAL_ORDER_STATES
        )
        order_terminal = (
            ord_hist is not None and (ord_hist.status or "").upper() in _TERMINAL_ORDER_STATES
        )
        fill_qty_sum = sum((f.qty for f in order_fills), Decimal("0"))
        expected_qty = exact(rec.my_qty_exact if rec.my_qty_exact is not None else rec.my_qty)
        qty_complete = expected_qty > 0 and fill_qty_sum >= expected_qty - Decimal("1e-8")
        if not (order_terminal or (intent_terminal and qty_complete)):
            continue
        if ord_hist is not None and ord_hist.filled_qty is not None and ord_hist.filled_qty > 0:
            if fill_qty_sum < ord_hist.filled_qty - Decimal("1e-8"):
                continue

        # Only aggregate fees when all fills are denominated in the USDT settlement currency.
        if all(
            f.commission_amount is not None and (f.commission_asset or "").upper() == "USDT"
            for f in order_fills
        ):
            total_fee = sum((f.commission_amount for f in order_fills if f.commission_amount is not None), Decimal("0"))
            rec.fee = float(total_fee)

        if len(order_fills) == 1:
            rec.exchange_fill_id = order_fills[0].exchange_fill_id

    await db.flush()
    return {"conflicts": conflicts, "fills": len(fills), "orders": len(orders)}
