"""Pure read-only analytics for follower account risk, equity curves, PnL breakdown, positions, and execution quality."""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time_util import BEIJING_TZ
from app.models.domain import (
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
    TradingAccount,
)
from app.services.adapters.base import safe_decimal
from app.services.account_detail.serializers import (
    decimal_to_float,
    dt_to_ms,
    finite_float,
    parse_range_window,
    sanitize_error_text,
)
from app.services.account_detail.snapshots import _compute_position_metrics
from app.services.positions import with_current_price

DAY_MS = 86_400_000
MAX_CURVE_POINTS = 1000

DATASET_STALE_MS = {
    "equity": 120_000,
    "open_orders": 120_000,
    "cashflow": 1_800_000,
    "fills": 1_800_000,
    "orders": 1_800_000,
    "force_orders": 14_400_000,
    "commission_rates": 14_400_000,
    "daily_snapshots": 2 * DAY_MS,
}

_KNOWN_DATASETS = (
    "equity",
    "open_orders",
    "cashflow",
    "fills",
    "orders",
    "force_orders",
    "commission_rates",
    "daily_snapshots",
)

_GAP_THRESHOLD_MS = {
    "raw": 15 * 60_000,
    "hour": 4 * 3_600_000,
    "day": 3 * DAY_MS,
}

_EXTERNAL_FLOW_CATEGORIES = frozenset({"TRANSFER_IN", "TRANSFER_OUT", "TRANSFER", "BONUS", "OTHER_EXTERNAL_FLOW"})


def _parse_json_list(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return [str(x).upper() for x in val] if isinstance(val, list) else []
    except (ValueError, TypeError):
        return []


def _parse_json_dict(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except (ValueError, TypeError):
        return {}


def _parse_positions_json(raw: Optional[str]) -> Tuple[List[Dict[str, Any]], bool]:
    if not raw:
        return [], False
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return [], False
        return [p for p in parsed if isinstance(p, dict)], True
    except (ValueError, TypeError):
        return [], False


def _parse_open_orders(snap: Optional[AccountOpenOrderSnapshot]) -> List[Dict[str, Any]]:
    if snap is None or not snap.orders_json:
        return []
    try:
        parsed = json.loads(snap.orders_json)
        return [o for o in parsed if isinstance(o, dict)] if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


def _normalize_sym_key(value: str) -> str:
    upper = (value or "").upper()
    if upper.endswith(("-SWAP", "-PERP")):
        upper = upper.rsplit("-", 1)[0]
    return upper.replace("-", "").replace("_", "")


def _binding_covers_symbol(binding: CopyBinding, symbol: str, inst_id: str) -> bool:
    if not binding.is_enabled:
        return False
    sym_keys = {_normalize_sym_key(symbol), _normalize_sym_key(inst_id)}
    blacklist = {_normalize_sym_key(s) for s in _parse_json_list(binding.symbol_blacklist_json)}
    if sym_keys & blacklist:
        return False
    whitelist = {_normalize_sym_key(s) for s in _parse_json_list(binding.symbol_whitelist_json)}
    if whitelist and not (sym_keys & whitelist):
        return False
    return True


def format_dataset_quality(ds: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Build standard Section 10.8 partition dataQuality object."""
    if not ds:
        return {
            "status": "unavailable",
            "fetchedAt": None,
            "coverageStart": None,
            "coverageEnd": None,
            "errorCode": None,
            "errorMessage": None,
        }
    return {
        "status": ds.get("status", "unavailable"),
        "fetchedAt": ds.get("lastSuccessAtMs") or ds.get("lastAttemptAtMs"),
        "coverageStart": ds.get("coverageStartMs"),
        "coverageEnd": ds.get("coverageEndMs"),
        "errorCode": ds.get("errorCode"),
        "errorMessage": ds.get("errorMessage"),
    }


def compute_data_quality_summary(
    sync_states: Sequence[AccountDatasetSyncState],
    open_order_snap: Optional[AccountOpenOrderSnapshot],
    account: TradingAccount,
    now_ms: int,
) -> Dict[str, Any]:
    by_dataset = {s.dataset: s for s in sync_states}
    datasets_out: Dict[str, Dict[str, Any]] = {}
    has_degraded = False
    has_unsupported = False

    for name in _KNOWN_DATASETS:
        st = by_dataset.get(name)
        if st is None:
            datasets_out[name] = {
                "dataset": name,
                "status": "unavailable",
                "rawStatus": "unavailable",
                "stale": True,
                "coverageStartMs": None,
                "coverageEndMs": None,
                "lastAttemptAtMs": None,
                "lastSuccessAtMs": None,
                "errorCode": None,
                "errorMessage": None,
                "recordCount": 0,
            }
            if name != "daily_snapshots":
                has_degraded = True
            continue

        stale_limit = DATASET_STALE_MS.get(name, 1_800_000)
        is_stale = (
            st.status not in ("unsupported",)
            and (st.last_success_at_ms is None or (now_ms - st.last_success_at_ms) > stale_limit)
        )
        effective_status = st.status
        if effective_status == "ok" and is_stale:
            effective_status = "stale"
        if effective_status == "unsupported":
            has_unsupported = True
        elif effective_status != "ok":
            has_degraded = True

        datasets_out[name] = {
            "dataset": name,
            "status": effective_status,
            "rawStatus": st.status,
            "stale": is_stale,
            "coverageStartMs": st.coverage_start_ms,
            "coverageEndMs": st.coverage_end_ms,
            "lastAttemptAtMs": st.last_attempt_at_ms,
            "lastSuccessAtMs": st.last_success_at_ms,
            "errorCode": st.last_error_code,
            "errorMessage": sanitize_error_text(st.last_error_message),
            "recordCount": st.record_count,
        }

    account_sync_ms = dt_to_ms(account.last_sync_at)
    account_sync_stale = account_sync_ms is None or (now_ms - account_sync_ms) > 90_000
    if account_sync_stale or account.status == "error":
        has_degraded = True

    open_orders_complete = bool(open_order_snap.complete) if open_order_snap is not None else False
    overall_status = "ok"
    if account.status == "error" or account_sync_ms is None:
        overall_status = "degraded"
    elif has_degraded or has_unsupported:
        overall_status = "partial"

    return {
        "overallStatus": overall_status,
        "accountSyncedAtMs": account_sync_ms,
        "accountSyncStale": account_sync_stale,
        "openOrdersFetchedAtMs": open_order_snap.fetched_at_ms if open_order_snap else None,
        "openOrdersComplete": open_orders_complete,
        "datasets": datasets_out,
    }


def compute_positions_and_protection(
    account: TradingAccount,
    allocations: Sequence[PositionAllocation],
    bindings: Sequence[CopyBinding],
    leads_by_id: Dict[int, LeadSource],
    guards: Sequence[PositionGuardState],
    open_order_snap: Optional[AccountOpenOrderSnapshot],
    intents: Sequence[OrderIntent],
    executions: Sequence[ExecutionRecord],
    orders_history: Sequence[AccountOrderHistory],
    fills: Sequence[AccountTradeFill],
    sync_states: Sequence[AccountDatasetSyncState],
    now_ms: int,
) -> Dict[str, Any]:
    raw_positions, positions_known = _parse_positions_json(account.positions_json)
    bindings_by_id = {b.id: b for b in bindings}
    guards_by_key = {(g.binding_id, g.inst_id.upper(), g.pos_side.lower()): g for g in guards}

    allocs_by_key: Dict[Tuple[str, str], List[PositionAllocation]] = defaultdict(list)
    for alloc in allocations:
        qty = safe_decimal(alloc.owned_qty_exact) or safe_decimal(alloc.owned_qty) or Decimal("0")
        if qty > 0:
            allocs_by_key[(alloc.inst_id.upper(), alloc.pos_side.lower())].append(alloc)

    manual_fill_keys: set[Tuple[str, str]] = set()
    for fill in fills:
        if fill.source_class == "manual":
            manual_fill_keys.add((_normalize_sym_key(fill.symbol), (fill.position_side or "").lower()))

    raw_open_orders = _parse_open_orders(open_order_snap)
    intents_by_id = {i.id: i for i in intents}
    intents_by_prefix32 = {i.id[:32]: i for i in intents}
    intents_by_ex_oid = {i.exchange_order_id: i for i in intents if i.exchange_order_id}
    execs_by_oid = {e.order_id: e for e in executions if e.order_id}
    hist_by_oid = {o.exchange_order_id: o for o in orders_history if o.exchange_order_id}
    hist_by_algo = {o.algo_id: o for o in orders_history if o.algo_id}

    open_orders_out: List[Dict[str, Any]] = []
    open_orders_by_inst: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    category_counts: Dict[str, int] = defaultdict(int)

    for oo in raw_open_orders:
        cat = str(oo.get("category") or "ordinary")
        if cat == "force_order":
            continue  # Force orders never enter open orders view per Section 8.5.
        order_id = str(oo.get("orderId") or "")
        algo_id = str(oo.get("algoId") or "") or None
        client_oid = str(oo.get("clientOrderId") or "") or None
        hist = (hist_by_algo.get(algo_id) if algo_id else None) or hist_by_oid.get(order_id)

        intent = (
            (intents_by_id.get(client_oid) if client_oid else None)
            or (intents_by_prefix32.get(client_oid[:32]) if client_oid else None)
            or intents_by_ex_oid.get(order_id)
        )
        ex_rec = execs_by_oid.get(order_id)
        binding_id = (
            (intent.binding_id if intent else None)
            or (ex_rec.binding_id if ex_rec else None)
            or (hist.binding_id if hist else None)
        )
        binding = bindings_by_id.get(binding_id) if binding_id is not None else None
        source_class = (
            "system"
            if (intent is not None or ex_rec is not None or (hist and hist.source_class == "system"))
            else (hist.source_class if hist else "unknown")
        )
        category_counts[cat] += 1
        enriched_oo = {
            "orderId": order_id,
            "algoId": algo_id,
            "clientOrderId": client_oid,
            "category": cat,
            "symbol": str(oo.get("symbol") or ""),
            "instId": str(oo.get("instId") or oo.get("symbol") or ""),
            "side": str(oo.get("side") or "").lower(),
            "positionSide": str(oo.get("positionSide") or "").lower(),
            "orderType": str(oo.get("orderType") or ""),
            "status": str(oo.get("status") or ""),
            "qty": finite_float(oo.get("qty")),
            "filledQty": finite_float(oo.get("filledQty")),
            "limitPrice": finite_float(oo.get("limitPrice")),
            "triggerPrice": finite_float(oo.get("triggerPrice")),
            "reduceOnly": oo.get("reduceOnly"),
            "closePosition": oo.get("closePosition"),
            "workingType": oo.get("workingType"),
            "createdAtMs": oo.get("createdAtMs"),
            "updatedAtMs": oo.get("updatedAtMs"),
            "sourceClass": source_class,
            "bindingId": binding_id,
            "bindingName": binding.name if binding else None,
        }
        open_orders_out.append(enriched_oo)
        inst_key = enriched_oo["instId"].upper()
        sym_key = enriched_oo["symbol"].upper()
        open_orders_by_inst[inst_key].append(enriched_oo)
        if sym_key and sym_key != inst_key:
            open_orders_by_inst[sym_key].append(enriched_oo)

    oo_sync = next((s for s in sync_states if s.dataset == "open_orders"), None)
    open_orders_stale = (
        oo_sync is None
        or oo_sync.last_success_at_ms is None
        or (now_ms - oo_sync.last_success_at_ms) > DATASET_STALE_MS["open_orders"]
    )
    open_order_snap_valid = (
        open_order_snap is not None
        and bool(open_order_snap.complete)
        and (now_ms - open_order_snap.fetched_at_ms) <= DATASET_STALE_MS["open_orders"]
    )

    positions_out: List[Dict[str, Any]] = []
    matched_alloc_ids: set[int] = set()
    allocated_notional_usdt = Decimal("0")
    unallocated_notional_usdt = Decimal("0")
    unprotected_count = 0
    partial_protected_count = 0

    for raw_pos in raw_positions:
        pos = with_current_price(raw_pos, account.exchange)
        raw_qty = safe_decimal(pos.get("pos")) or Decimal("0")
        if raw_qty == 0:
            continue
        qty_abs = abs(raw_qty)
        inst_id = str(pos.get("instId") or pos.get("symbol") or "")
        symbol = str(pos.get("symbol") or inst_id)
        pos_side = str(pos.get("posSide") or "").lower()
        logical_side = ("long" if raw_qty > 0 else "short") if pos_side in ("", "net", "both") else pos_side

        matching_allocs = list(allocs_by_key.get((inst_id.upper(), pos_side), []))
        if not matching_allocs and logical_side != pos_side:
            matching_allocs = list(allocs_by_key.get((inst_id.upper(), logical_side), []))
        if not matching_allocs and symbol.upper() != inst_id.upper():
            matching_allocs = list(allocs_by_key.get((symbol.upper(), logical_side), []))

        alloc_sum = Decimal("0")
        alloc_details: List[Dict[str, Any]] = []
        has_software_guard = False
        requires_exchange_protection = False
        for alloc in matching_allocs:
            matched_alloc_ids.add(alloc.id)
            a_qty = safe_decimal(alloc.owned_qty_exact) or safe_decimal(alloc.owned_qty) or Decimal("0")
            alloc_sum += a_qty
            b = bindings_by_id.get(alloc.binding_id)
            lead = leads_by_id.get(b.lead_id) if b else None
            guard = guards_by_key.get((alloc.binding_id, alloc.inst_id.upper(), alloc.pos_side.lower()))
            if b:
                if b.tp_sl_mode in ("independent", "hybrid_guard") or b.enable_stop_loss or guard is not None:
                    has_software_guard = True
                if b.enable_stop_loss or b.tp_sl_mode in ("independent", "hybrid_guard"):
                    requires_exchange_protection = True
            alloc_details.append(
                {
                    "allocationId": alloc.id,
                    "bindingId": alloc.binding_id,
                    "bindingName": b.name if b else f"Binding #{alloc.binding_id}",
                    "leadId": b.lead_id if b else None,
                    "leadName": lead.name if lead else None,
                    "ownedQty": decimal_to_float(a_qty),
                    "expectedQty": decimal_to_float(safe_decimal(alloc.expected_qty_exact)),
                    "entryCost": decimal_to_float(safe_decimal(alloc.entry_cost_exact) or safe_decimal(alloc.entry_cost)),
                    "cycleRatio": decimal_to_float(safe_decimal(alloc.cycle_ratio_exact)),
                    "generation": alloc.generation,
                    "openedAtMs": alloc.opened_at_ms or None,
                }
            )

        tol = max(qty_abs * Decimal("0.005"), Decimal("0.000001"))
        unallocated_class = "none"
        if alloc_sum == 0:
            ownership = "manual"
            unallocated_qty = qty_abs
            over_allocated_qty = Decimal("0")
        elif abs(qty_abs - alloc_sum) <= tol:
            ownership = "system"
            unallocated_qty = Decimal("0")
            over_allocated_qty = Decimal("0")
        elif qty_abs > alloc_sum:
            ownership = "mixed"
            unallocated_qty = qty_abs - alloc_sum
            over_allocated_qty = Decimal("0")
        else:
            ownership = "drift"
            unallocated_qty = Decimal("0")
            over_allocated_qty = alloc_sum - qty_abs

        if unallocated_qty > Decimal("0"):
            sym_norm = _normalize_sym_key(symbol)
            has_manual_fill = (
                (sym_norm, logical_side) in manual_fill_keys
                or (sym_norm, "both") in manual_fill_keys
                or (sym_norm, "net") in manual_fill_keys
                or (sym_norm, "") in manual_fill_keys
            )
            in_covered_binding = any(_binding_covers_symbol(b, symbol, inst_id) for b in bindings)
            if has_manual_fill:
                unallocated_class = "manual"
            elif in_covered_binding:
                unallocated_class = "orphan_risk"
            else:
                unallocated_class = "unallocated"

        notional_usd = safe_decimal(pos.get("notionalUsd"))
        cur_px = safe_decimal(pos.get("curPx") or pos.get("markPx"))
        if notional_usd is None and cur_px is not None and cur_px > 0:
            notional_usd = qty_abs * cur_px
        currency = "USDC" if symbol.upper().endswith("USDC") else "USDT"
        if currency == "USDT" and notional_usd is not None:
            ratio = min(Decimal("1"), alloc_sum / qty_abs) if qty_abs > 0 else Decimal("0")
            allocated_notional_usdt += abs(notional_usd) * ratio
            unallocated_notional_usdt += abs(notional_usd) * (Decimal("1") - ratio)

        close_side = "sell" if logical_side == "long" else "buy"
        candidates = open_orders_by_inst.get(inst_id.upper(), []) or open_orders_by_inst.get(symbol.upper(), [])
        seen_oid: set[str] = set()
        related_orders: List[Dict[str, Any]] = []
        sl_qty = Decimal("0")
        tp_qty = Decimal("0")
        sl_qty_unknown = False
        tp_qty_unknown = False
        has_sl_full = False
        has_tp_full = False
        has_sl = False
        has_tp = False
        has_trailing = False
        sl_prices: List[Decimal] = []
        tp_prices: List[Decimal] = []

        for cand in candidates:
            uniq = f"{cand['orderId']}:{cand['algoId']}:{cand['category']}"
            if uniq in seen_oid:
                continue
            seen_oid.add(uniq)
            cand_ps = cand["positionSide"]
            side_match = cand["side"] == close_side or cand_ps == logical_side
            pos_side_match = cand_ps in ("", "both", "net", logical_side)
            if not (side_match and pos_side_match):
                continue
            cat = cand["category"]
            if cat in ("stop_loss", "trailing_stop", "take_profit", "conditional") or cand.get("reduceOnly") or cand.get("closePosition"):
                related_orders.append(cand)
            trig = safe_decimal(cand.get("triggerPrice")) or safe_decimal(cand.get("limitPrice"))
            if cat in ("stop_loss", "trailing_stop"):
                has_sl = True
                if cat == "trailing_stop":
                    has_trailing = True
                if trig is not None and trig > 0:
                    sl_prices.append(trig)
                if cand.get("closePosition"):
                    has_sl_full = True
                else:
                    oq = safe_decimal(cand.get("qty"))
                    fq = safe_decimal(cand.get("filledQty")) or Decimal("0")
                    if oq is None:
                        sl_qty_unknown = True
                    else:
                        sl_qty += max(Decimal("0"), oq - fq)
            elif cat == "take_profit":
                has_tp = True
                if trig is not None and trig > 0:
                    tp_prices.append(trig)
                if cand.get("closePosition"):
                    has_tp_full = True
                else:
                    oq = safe_decimal(cand.get("qty"))
                    fq = safe_decimal(cand.get("filledQty")) or Decimal("0")
                    if oq is None:
                        tp_qty_unknown = True
                    else:
                        tp_qty += max(Decimal("0"), oq - fq)

        sl_coverage_pct: Optional[float] = None
        tp_coverage_pct: Optional[float] = None
        over_covered = False
        if open_order_snap_valid:
            if has_sl_full:
                sl_coverage_pct = 100.0
            elif has_sl and sl_qty_unknown:
                sl_coverage_pct = None
            elif has_sl and qty_abs > 0:
                if sl_qty > qty_abs * Decimal("1.05"):
                    over_covered = True
                sl_coverage_pct = min(100.0, finite_float((sl_qty / qty_abs) * Decimal("100"), precision=2) or 0.0)
            else:
                sl_coverage_pct = 0.0

            if has_tp_full:
                tp_coverage_pct = 100.0
            elif has_tp and tp_qty_unknown:
                tp_coverage_pct = None
            elif has_tp and qty_abs > 0:
                if tp_qty > qty_abs * Decimal("1.05"):
                    over_covered = True
                tp_coverage_pct = min(100.0, finite_float((tp_qty / qty_abs) * Decimal("100"), precision=2) or 0.0)
            else:
                tp_coverage_pct = 0.0

            if has_sl_full or (has_sl and sl_coverage_pct is not None and sl_coverage_pct >= 95.0):
                protection_status = "protected"
            elif has_sl and sl_coverage_pct is None:
                protection_status = "unknown"
            elif has_sl:
                protection_status = "partial"
                partial_protected_count += 1
            elif has_software_guard:
                protection_status = "software_only"
            else:
                protection_status = "unprotected"
                unprotected_count += 1
        else:
            protection_status = "unknown"

        nearest_sl = None
        if sl_prices:
            nearest_sl = decimal_to_float(max(sl_prices) if logical_side == "long" else min(sl_prices))
        nearest_tp = None
        if tp_prices:
            nearest_tp = decimal_to_float(min(tp_prices) if logical_side == "long" else max(tp_prices))

        avg_px = safe_decimal(pos.get("avgPx"))
        liq_px = safe_decimal(pos.get("liquidationPrice"))
        liq_dist_pct: Optional[float] = None
        ref_px = cur_px if (cur_px is not None and cur_px > 0) else avg_px
        if liq_px is not None and liq_px > 0 and ref_px is not None and ref_px > 0:
            raw_dist = ((ref_px - liq_px) / ref_px) * Decimal("100") if logical_side == "long" else ((liq_px - ref_px) / ref_px) * Decimal("100")
            if raw_dist > 0:
                liq_dist_pct = finite_float(raw_dist, precision=2)

        positions_out.append(
            {
                "instId": inst_id,
                "symbol": symbol,
                "currency": currency,
                "posSide": logical_side,
                "rawPosSide": pos_side,
                "mgnMode": str(pos.get("mgnMode") or account.margin_mode or "cross").lower(),
                "qty": decimal_to_float(qty_abs),
                "availQty": finite_float(pos.get("availPos")),
                "avgPx": decimal_to_float(avg_px),
                "curPx": decimal_to_float(cur_px),
                "breakEvenPx": finite_float(pos.get("breakEvenPrice")),
                "liquidationPx": decimal_to_float(liq_px) if (liq_px is not None and liq_px > 0) else None,
                "liqDistancePct": liq_dist_pct,
                "upl": finite_float(pos.get("upl")),
                "uplRatioPct": finite_float((safe_decimal(pos.get("uplRatio")) or Decimal("0")) * Decimal("100"), precision=2)
                if pos.get("uplRatio") is not None
                else None,
                "notionalUsd": decimal_to_float(notional_usd),
                "initialMargin": finite_float(pos.get("initialMargin") or pos.get("marginUsd")),
                "maintenanceMargin": finite_float(pos.get("maintenanceMargin")),
                "marginRatio": finite_float(pos.get("marginRatio")),
                "lever": finite_float(pos.get("lever")),
                "adlQuantile": pos.get("adlQuantile"),
                "ownership": ownership,
                "unallocatedClass": unallocated_class,
                "allocatedQty": decimal_to_float(alloc_sum),
                "unallocatedQty": decimal_to_float(unallocated_qty),
                "overAllocatedQty": decimal_to_float(over_allocated_qty),
                "requiresProtection": requires_exchange_protection,
                "allocations": alloc_details,
                "protection": {
                    "status": protection_status,
                    "hasTakeProfit": has_tp,
                    "hasStopLoss": has_sl,
                    "hasTrailingStop": has_trailing,
                    "hasSoftwareGuard": has_software_guard,
                    "slCoveredQty": decimal_to_float(qty_abs if has_sl_full else sl_qty) if (has_sl and not sl_qty_unknown) else None,
                    "slCoveragePct": sl_coverage_pct,
                    "nearestSlPrice": nearest_sl,
                    "tpCoveredQty": decimal_to_float(qty_abs if has_tp_full else tp_qty) if (has_tp and not tp_qty_unknown) else None,
                    "tpCoveragePct": tp_coverage_pct,
                    "nearestTpPrice": nearest_tp,
                    "overCovered": over_covered,
                    "protectiveOrderCount": len(related_orders),
                    "orders": related_orders,
                },
            }
        )

    orphan_allocations: List[Dict[str, Any]] = []
    for alloc in allocations:
        qty = safe_decimal(alloc.owned_qty_exact) or safe_decimal(alloc.owned_qty) or Decimal("0")
        if qty > 0 and alloc.id not in matched_alloc_ids:
            b = bindings_by_id.get(alloc.binding_id)
            orphan_allocations.append(
                {
                    "allocationId": alloc.id,
                    "bindingId": alloc.binding_id,
                    "bindingName": b.name if b else f"Binding #{alloc.binding_id}",
                    "instId": alloc.inst_id,
                    "posSide": alloc.pos_side,
                    "ownedQty": decimal_to_float(qty),
                    "openedAtMs": alloc.opened_at_ms or None,
                }
            )

    return {
        "positionsKnown": positions_known,
        "positions": positions_out,
        "orphanAllocations": orphan_allocations,
        "allocatedNotionalUsdt": decimal_to_float(allocated_notional_usdt) if positions_known else None,
        "unallocatedNotionalUsdt": decimal_to_float(unallocated_notional_usdt) if positions_known else None,
        "protectionSummary": {
            "openOrdersKnown": open_order_snap_valid,
            "openOrdersStale": open_orders_stale and open_order_snap is not None,
            "fetchedAtMs": open_order_snap.fetched_at_ms if open_order_snap else None,
            "openOrdersStatus": "unavailable" if open_order_snap is None else ("stale" if not open_order_snap_valid else "ok"),
            "unprotectedCount": unprotected_count if open_order_snap_valid else None,
            "partialProtectedCount": partial_protected_count if open_order_snap_valid else None,
            "totalOpenOrders": len(open_orders_out),
            "byCategory": dict(category_counts),
        },
        "openOrders": open_orders_out,
    }


def compute_risk_overview(
    account: TradingAccount,
    latest_usdt_snap: Optional[AccountEquitySnapshot],
    latest_usdc_snap: Optional[AccountEquitySnapshot],
    positions_summary: Dict[str, Any],
    sync_states: Sequence[AccountDatasetSyncState],
    now_ms: int,
) -> Dict[str, Any]:
    from app.services.engine.equity import account_usdt_day_start, account_usdt_hwm, risk_state
    from app.services.engine.cashflow import stored_today_net_flow

    state = risk_state(account)
    assets = state.get("assets") or {}
    usdt_state = assets.get("USDT") if isinstance(assets, dict) else {}
    if not isinstance(usdt_state, dict):
        usdt_state = {}

    eq_sync = next((s for s in sync_states if s.dataset == "equity"), None)
    cap_flags = _parse_json_dict(eq_sync.cursor_json if eq_sync else None)

    has_synced = account.last_sync_at is not None or latest_usdt_snap is not None
    equity: Optional[Decimal] = None
    available: Optional[Decimal] = None
    wallet_balance: Optional[Decimal] = None
    upl: Optional[Decimal] = None
    initial_margin: Optional[Decimal] = None
    maintenance_margin: Optional[Decimal] = None
    margin_ratio: Optional[Decimal] = None
    margin_ratio_source: Optional[str] = cap_flags.get("marginRatioSource")

    if latest_usdt_snap is not None:
        equity = latest_usdt_snap.equity
        available = latest_usdt_snap.available_balance
        wallet_balance = latest_usdt_snap.wallet_balance
        upl = latest_usdt_snap.unrealized_pnl
        initial_margin = latest_usdt_snap.initial_margin
        maintenance_margin = latest_usdt_snap.maintenance_margin
        margin_ratio = latest_usdt_snap.margin_ratio

    account_sync_ms = dt_to_ms(account.last_sync_at) or 0
    if account.last_sync_at is not None and (
        latest_usdt_snap is None or account_sync_ms >= (latest_usdt_snap.captured_at_ms + 5000)
    ):
        if state.get("display_usdt_confirmed") or usdt_state.get("equity") is not None:
            eq_candidate = safe_decimal(usdt_state.get("equity")) or safe_decimal(account.usdt_equity)
            av_candidate = safe_decimal(usdt_state.get("available"))
            if av_candidate is None:
                av_candidate = safe_decimal(account.avail_equity)
            equity = eq_candidate if eq_candidate is not None else equity
            available = av_candidate if av_candidate is not None else available

    raw_positions, positions_known = _parse_positions_json(account.positions_json)
    pos_metrics = _compute_position_metrics(raw_positions if positions_known else None, "USDT")

    total_notional = pos_metrics["total_notional"] if positions_known else (
        latest_usdt_snap.total_notional if latest_usdt_snap else None
    )
    long_notional = pos_metrics["long_notional"] if positions_known else (
        latest_usdt_snap.long_notional if latest_usdt_snap else None
    )
    short_notional = pos_metrics["short_notional"] if positions_known else (
        latest_usdt_snap.short_notional if latest_usdt_snap else None
    )
    net_notional = pos_metrics["net_notional"] if positions_known else (
        latest_usdt_snap.net_notional if latest_usdt_snap else None
    )
    if upl is None and has_synced and positions_known:
        all_usdt = all((p.get("symbol") or "").upper().endswith("USDT") for p in raw_positions)
        if all_usdt:
            upl_sum: Optional[Decimal] = Decimal("0")
            for p in raw_positions:
                u = safe_decimal(p.get("upl"))
                if u is None:
                    upl_sum = None
                    break
                upl_sum = (upl_sum or Decimal("0")) + u
            upl = upl_sum
    if initial_margin is None and positions_known:
        initial_margin = pos_metrics["initial_margin"]
    if maintenance_margin is None and positions_known:
        maintenance_margin = pos_metrics["maintenance_margin"]
    if margin_ratio is None and maintenance_margin is not None and equity is not None and equity > 0:
        margin_ratio = maintenance_margin / equity
        margin_ratio_source = margin_ratio_source or "derived"
    elif margin_ratio is not None:
        margin_ratio_source = margin_ratio_source or "exchange"

    initial_margin_ratio: Optional[Decimal] = None
    if initial_margin is not None and equity is not None and equity > 0:
        initial_margin_ratio = initial_margin / equity

    effective_leverage: Optional[Decimal] = None
    if total_notional is not None and equity is not None and equity > 0:
        effective_leverage = total_notional / equity
    elif latest_usdt_snap is not None and equity is not None and equity > 0:
        effective_leverage = latest_usdt_snap.effective_leverage

    hwm = safe_decimal(account_usdt_hwm(account))
    if hwm is not None and hwm <= 0:
        hwm = None
    if equity is not None and (hwm is None or equity > hwm) and has_synced:
        hwm = equity
    drawdown_pct: Optional[float] = None
    if hwm is not None and hwm > 0 and equity is not None:
        dd = max(Decimal("0"), ((hwm - equity) / hwm) * Decimal("100"))
        drawdown_pct = finite_float(dd, precision=4)

    today_str = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).astimezone(BEIJING_TZ).strftime("%Y-%m-%d")
    day_start = safe_decimal(usdt_state.get("day_start")) if usdt_state.get("date") == today_str else None
    if day_start is None and account.day_start_date == today_str:
        day_start = safe_decimal(account_usdt_day_start(account))
    today_net_flow = safe_decimal(stored_today_net_flow(state, today_str))
    today_pnl: Optional[float] = None
    if equity is not None and day_start is not None and today_net_flow is not None:
        today_pnl = finite_float(equity - day_start - today_net_flow, precision=4)

    max_single_share_pct: Optional[float] = None
    closest_liq_pct: Optional[float] = None
    closest_liq_symbol: Optional[str] = None
    usdt_notionals: List[Decimal] = []
    for p in positions_summary.get("positions", []):
        if p.get("currency") == "USDT" and p.get("notionalUsd") is not None:
            n = safe_decimal(p["notionalUsd"])
            if n is not None:
                usdt_notionals.append(abs(n))
        ld = p.get("liqDistancePct")
        if ld is not None and (closest_liq_pct is None or ld < closest_liq_pct):
            closest_liq_pct = ld
            closest_liq_symbol = p.get("symbol")

    if total_notional is not None and total_notional > 0 and usdt_notionals:
        max_single_share_pct = finite_float((max(usdt_notionals) / total_notional) * Decimal("100"), precision=2)
    elif total_notional == Decimal("0") and positions_known:
        max_single_share_pct = 0.0

    mr_float = decimal_to_float(margin_ratio, precision=6)
    risk_level = "unknown"
    if has_synced:
        if (mr_float is not None and mr_float >= 0.80) or (closest_liq_pct is not None and closest_liq_pct <= 5.0):
            risk_level = "critical"
        elif (mr_float is not None and mr_float >= 0.50) or (closest_liq_pct is not None and closest_liq_pct <= 10.0):
            risk_level = "warning"
        elif mr_float is not None or positions_known:
            risk_level = "normal"

    other_assets: List[Dict[str, Any]] = []
    if isinstance(assets, dict):
        for ccy, info in sorted(assets.items()):
            if ccy == "USDT" or not isinstance(info, dict):
                continue
            eq_val = finite_float(info.get("equity"))
            av_val = finite_float(info.get("available"))
            if (eq_val == 0.0 and av_val == 0.0) and ccy != "USDC":
                continue
            snap_src = latest_usdc_snap if ccy == "USDC" else None
            other_assets.append(
                {
                    "currency": ccy,
                    "equity": finite_float(snap_src.equity) if snap_src else eq_val,
                    "available": finite_float(snap_src.available_balance) if snap_src else av_val,
                    "walletBalance": finite_float(snap_src.wallet_balance) if snap_src else None,
                    "unrealizedPnl": finite_float(snap_src.unrealized_pnl) if snap_src else None,
                }
            )
    if not other_assets and latest_usdc_snap is not None:
        other_assets.append(
            {
                "currency": "USDC",
                "equity": decimal_to_float(latest_usdc_snap.equity),
                "available": decimal_to_float(latest_usdc_snap.available_balance),
                "walletBalance": decimal_to_float(latest_usdc_snap.wallet_balance),
                "unrealizedPnl": decimal_to_float(latest_usdc_snap.unrealized_pnl),
            }
        )

    return {
        "currency": "USDT",
        "riskLevel": risk_level,
        "equity": decimal_to_float(equity, precision=4) if has_synced else None,
        "walletBalance": decimal_to_float(wallet_balance, precision=4),
        "availableBalance": decimal_to_float(available, precision=4) if has_synced else None,
        "unrealizedPnl": decimal_to_float(upl, precision=4),
        "hwmEquity": decimal_to_float(hwm, precision=4),
        "drawdownPct": drawdown_pct,
        "dayStartEquity": decimal_to_float(day_start, precision=4),
        "todayNetFlow": decimal_to_float(today_net_flow, precision=4),
        "todayPnl": today_pnl,
        "totalNotional": decimal_to_float(total_notional, precision=4),
        "longNotional": decimal_to_float(long_notional, precision=4),
        "shortNotional": decimal_to_float(short_notional, precision=4),
        "netNotional": decimal_to_float(net_notional, precision=4),
        "allocatedNotional": positions_summary["allocatedNotionalUsdt"],
        "unallocatedNotional": positions_summary["unallocatedNotionalUsdt"],
        "maxSinglePositionSharePct": max_single_share_pct,
        "initialMargin": decimal_to_float(initial_margin, precision=4),
        "initialMarginRatio": decimal_to_float(initial_margin_ratio, precision=6),
        "initialMarginRatioPct": finite_float(initial_margin_ratio * Decimal("100"), precision=2)
        if initial_margin_ratio is not None
        else None,
        "maintenanceMargin": decimal_to_float(maintenance_margin, precision=4),
        "marginRatio": mr_float,
        "marginRatioPct": finite_float(margin_ratio * Decimal("100"), precision=2) if margin_ratio is not None else None,
        "marginRatioSource": margin_ratio_source,
        "effectiveLeverage": decimal_to_float(effective_leverage, precision=3),
        "closestLiquidationDistancePct": closest_liq_pct,
        "closestLiquidationSymbol": closest_liq_symbol,
        "positionCount": len(positions_summary["positions"]) if positions_known else None,
        "openOrderCount": positions_summary["protectionSummary"]["totalOpenOrders"]
        if positions_summary["protectionSummary"]["openOrdersKnown"]
        else None,
        "canTrade": cap_flags.get("canTrade"),
        "canDeposit": cap_flags.get("canDeposit"),
        "canWithdraw": cap_flags.get("canWithdraw"),
        "multiAssetsMargin": cap_flags.get("multiAssetsMargin"),
        "dualSidePosition": cap_flags.get("dualSidePosition"),
        "otherAssets": other_assets,
        "snapshotAsOfMs": latest_usdt_snap.captured_at_ms if latest_usdt_snap else None,
        "snapshotQuality": latest_usdt_snap.data_quality if latest_usdt_snap else None,
    }


async def compute_pnl_breakdown(
    db: AsyncSession,
    account: TradingAccount,
    range_name: str,
    now_ms: int,
    sync_states: Sequence[AccountDatasetSyncState],
    bindings: Optional[Sequence[CopyBinding]] = None,
) -> Dict[str, Any]:
    start_ms, end_ms = parse_range_window(range_name, now_ms)

    snap_q = select(AccountEquitySnapshot).where(
        AccountEquitySnapshot.account_id == account.id,
        AccountEquitySnapshot.currency == "USDT",
        AccountEquitySnapshot.captured_at_ms <= end_ms,
    )
    if start_ms is not None:
        first_in_win = await db.scalar(
            snap_q.where(AccountEquitySnapshot.captured_at_ms >= start_ms)
            .order_by(AccountEquitySnapshot.captured_at_ms.asc())
            .limit(1)
        )
        prior_snap = await db.scalar(
            select(AccountEquitySnapshot)
            .where(
                AccountEquitySnapshot.account_id == account.id,
                AccountEquitySnapshot.currency == "USDT",
                AccountEquitySnapshot.captured_at_ms < start_ms,
            )
            .order_by(AccountEquitySnapshot.captured_at_ms.desc())
            .limit(1)
        )
        start_snap = (
            prior_snap
            if (prior_snap is not None and (first_in_win is None or (start_ms - prior_snap.captured_at_ms) <= DAY_MS))
            else first_in_win
        )
    else:
        start_snap = await db.scalar(snap_q.order_by(AccountEquitySnapshot.captured_at_ms.asc()).limit(1))
    end_snap = await db.scalar(snap_q.order_by(AccountEquitySnapshot.captured_at_ms.desc()).limit(1))

    effective_start_ms = (
        start_snap.captured_at_ms
        if start_snap is not None
        else (start_ms if start_ms is not None else 0)
    )
    effective_end_ms = end_snap.captured_at_ms if end_snap is not None else end_ms
    cf_q = select(AccountCashflowEntry).where(
        AccountCashflowEntry.account_id == account.id,
        AccountCashflowEntry.occurred_at_ms <= effective_end_ms,
    )
    if start_snap is not None:
        cf_q = cf_q.where(AccountCashflowEntry.occurred_at_ms > start_snap.captured_at_ms)
    elif start_ms is not None:
        cf_q = cf_q.where(AccountCashflowEntry.occurred_at_ms >= start_ms)
    cashflows = list((await db.scalars(cf_q)).all())

    cf_sync = next((s for s in sync_states if s.dataset == "cashflow"), None)
    cf_status = cf_sync.status if cf_sync else "unavailable"
    cf_covers_interval = bool(
        cf_sync is not None
        and cf_sync.status == "ok"
        and cf_sync.coverage_start_ms is not None
        and cf_sync.coverage_end_ms is not None
        and cf_sync.coverage_start_ms <= effective_start_ms
    )

    usdt_totals: Dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    other_flows: Dict[str, Dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: Decimal("0")))
    funding_by_symbol: Dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    deposits = Decimal("0")
    withdrawals = Decimal("0")

    for cf in cashflows:
        asset = (cf.asset or "USDT").upper()
        amt = cf.signed_amount
        if asset == "USDT":
            usdt_totals[cf.category] += amt
            if cf.category == "TRANSFER_IN" or (cf.category == "TRANSFER" and amt > 0):
                deposits += max(Decimal("0"), amt)
            elif cf.category == "TRANSFER_OUT" or (cf.category == "TRANSFER" and amt < 0):
                withdrawals += max(Decimal("0"), -amt)
            if cf.symbol and cf.category in ("FUNDING_FEE", "SPECIAL_FUNDING_FEE"):
                funding_by_symbol[cf.symbol.upper()] += amt
        else:
            other_flows[asset][cf.category] += amt

    net_transfers = deposits - withdrawals
    realized_pnl_ledger = usdt_totals["REALIZED_PNL"]
    funding_fee = usdt_totals["FUNDING_FEE"] + usdt_totals["SPECIAL_FUNDING_FEE"]
    commission = usdt_totals["COMMISSION"]
    commission_rebate = usdt_totals["COMMISSION_REBATE"]
    liquidation_fee = usdt_totals["LIQUIDATION_FEE"]
    insurance_clear = usdt_totals["INSURANCE_CLEAR"]
    bonus = usdt_totals["BONUS"]
    referral_rebate = usdt_totals["REFERRAL_REBATE"]
    other_trading_income = usdt_totals["OTHER_TRADING_INCOME"]
    other_external_flow = usdt_totals["OTHER_EXTERNAL_FLOW"]
    unknown_flow = usdt_totals["UNKNOWN"]
    non_trading_external = bonus + other_external_flow

    start_eq = start_snap.equity if start_snap else None
    end_eq = end_snap.equity if end_snap else None
    total_equity_change = (end_eq - start_eq) if (start_eq is not None and end_eq is not None) else None
    start_upl = start_snap.unrealized_pnl if start_snap else None
    end_upl = end_snap.unrealized_pnl if end_snap else None
    upl_change = (end_upl - start_upl) if (start_upl is not None and end_upl is not None) else None

    cf_data_usable = cf_sync is not None and cf_sync.status in ("ok", "partial")
    trading_net_pnl: Optional[Decimal] = None
    ledger_net_pnl: Optional[Decimal] = None

    if total_equity_change is not None and cf_covers_interval:
        trading_net_pnl = total_equity_change - net_transfers - non_trading_external

    cashflow_trading_only = (
        realized_pnl_ledger
        + funding_fee
        + commission
        + commission_rebate
        + liquidation_fee
        + insurance_clear
        + referral_rebate
        + other_trading_income
        + unknown_flow
    )
    if cf_covers_interval and upl_change is not None:
        ledger_net_pnl = cashflow_trading_only + upl_change

    reconciliation_delta: Optional[Decimal] = None
    reconciled: Optional[bool] = None
    if trading_net_pnl is not None and ledger_net_pnl is not None and end_eq is not None:
        reconciliation_delta = trading_net_pnl - ledger_net_pnl
        tol = max(Decimal("1"), abs(end_eq) * Decimal("0.005"))
        reconciled = abs(reconciliation_delta) <= tol

    # Fills breakdown by attribution source, symbol, and binding
    fills_q = select(AccountTradeFill).where(
        AccountTradeFill.account_id == account.id,
        AccountTradeFill.occurred_at_ms <= end_ms,
    )
    if start_ms is not None:
        fills_q = fills_q.where(AccountTradeFill.occurred_at_ms >= start_ms)
    fills = list((await db.scalars(fills_q)).all())
    fills_sync = next((s for s in sync_states if s.dataset == "fills"), None)
    fills_covers_interval = bool(
        fills_sync is not None
        and fills_sync.status == "ok"
        and fills_sync.coverage_start_ms is not None
        and fills_sync.coverage_start_ms <= effective_start_ms
    )

    attr_buckets: Dict[str, Dict[str, Any]] = {
        cls: {"fillCount": 0, "grossRealizedPnlUsdt": Decimal("0"), "commissionUsdt": Decimal("0"), "netPnlUsdt": Decimal("0")}
        for cls in ("system", "manual", "unknown")
    }
    sym_buckets: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"fillCount": 0, "realizedPnlUsdt": Decimal("0"), "commissionUsdt": Decimal("0"), "fundingFeeUsdt": Decimal("0")}
    )
    bind_buckets: Dict[int, Dict[str, Any]] = defaultdict(
        lambda: {"fillCount": 0, "realizedPnlUsdt": Decimal("0"), "commissionUsdt": Decimal("0"), "netPnlUsdt": Decimal("0")}
    )
    non_usdt_fill_commissions: Dict[str, Decimal] = defaultdict(lambda: Decimal("0"))

    for fill in fills:
        cls = fill.source_class if fill.source_class in attr_buckets else "unknown"
        ab = attr_buckets[cls]
        ab["fillCount"] += 1
        rp = (fill.realized_pnl or Decimal("0")) if fill.symbol.upper().endswith("USDT") else Decimal("0")
        ab["grossRealizedPnlUsdt"] += rp

        sym = fill.symbol.upper()
        sb = sym_buckets[sym]
        sb["fillCount"] += 1
        sb["realizedPnlUsdt"] += rp

        comm_usdt = Decimal("0")
        if fill.commission_amount is not None:
            c_asset = (fill.commission_asset or "USDT").upper()
            if c_asset == "USDT":
                comm_usdt = fill.commission_amount
                ab["commissionUsdt"] += comm_usdt
                sb["commissionUsdt"] += comm_usdt
            else:
                non_usdt_fill_commissions[c_asset] += fill.commission_amount

        ab["netPnlUsdt"] += rp - comm_usdt
        if fill.binding_id is not None:
            bb = bind_buckets[fill.binding_id]
            bb["fillCount"] += 1
            bb["realizedPnlUsdt"] += rp
            bb["commissionUsdt"] += comm_usdt
            bb["netPnlUsdt"] += rp - comm_usdt

    for sym, ff in funding_by_symbol.items():
        sym_buckets[sym]["fundingFeeUsdt"] += ff

    binding_names = {b.id: b.name for b in (bindings or [])}
    by_symbol_out = sorted(
        [
            {
                "symbol": sym,
                "fillCount": sb["fillCount"],
                "realizedPnlUsdt": decimal_to_float(sb["realizedPnlUsdt"], precision=4),
                "commissionUsdt": decimal_to_float(sb["commissionUsdt"], precision=4),
                "fundingFeeUsdt": decimal_to_float(sb["fundingFeeUsdt"], precision=4),
                "netPnlUsdt": decimal_to_float(
                    sb["realizedPnlUsdt"] - sb["commissionUsdt"] + sb["fundingFeeUsdt"], precision=4
                ),
            }
            for sym, sb in sym_buckets.items()
        ],
        key=lambda x: abs(x["netPnlUsdt"] or 0.0),
        reverse=True,
    )
    by_binding_out = [
        {
            "bindingId": bid,
            "bindingName": binding_names.get(bid, f"Binding #{bid}"),
            "fillCount": bb["fillCount"],
            "realizedPnlUsdt": decimal_to_float(bb["realizedPnlUsdt"], precision=4),
            "commissionUsdt": decimal_to_float(bb["commissionUsdt"], precision=4),
            "netPnlUsdt": decimal_to_float(bb["netPnlUsdt"], precision=4),
        }
        for bid, bb in sorted(bind_buckets.items())
    ]

    return {
        "currency": "USDT",
        "range": range_name,
        "startMs": start_snap.captured_at_ms if start_snap else start_ms,
        "endMs": end_snap.captured_at_ms if end_snap else end_ms,
        "cashflowStatus": cf_status,
        "cashflowCoverageComplete": cf_covers_interval,
        "cashflowCoverageStartMs": cf_sync.coverage_start_ms if cf_sync else None,
        "fillsStatus": fills_sync.status if fills_sync else "unavailable",
        "fillsCoverageComplete": fills_covers_interval,
        "startEquity": decimal_to_float(start_eq, precision=4),
        "endEquity": decimal_to_float(end_eq, precision=4),
        "totalEquityChange": decimal_to_float(total_equity_change, precision=4),
        "unrealizedPnlChange": decimal_to_float(upl_change, precision=4),
        "deposits": decimal_to_float(deposits, precision=4) if cf_data_usable else None,
        "withdrawals": decimal_to_float(withdrawals, precision=4) if cf_data_usable else None,
        "netTransfers": decimal_to_float(net_transfers, precision=4) if cf_data_usable else None,
        "nonTradingExternal": decimal_to_float(non_trading_external, precision=4) if cf_data_usable else None,
        "tradingNetPnl": decimal_to_float(trading_net_pnl, precision=4),
        "tradingPnl": decimal_to_float(trading_net_pnl, precision=4),
        "ledgerNetPnl": decimal_to_float(ledger_net_pnl, precision=4),
        "ledger": {
            "realizedPnl": decimal_to_float(realized_pnl_ledger, precision=4) if cf_data_usable else None,
            "fundingFee": decimal_to_float(funding_fee, precision=4) if cf_data_usable else None,
            "commission": decimal_to_float(commission, precision=4) if cf_data_usable else None,
            "commissionRebate": decimal_to_float(commission_rebate, precision=4) if cf_data_usable else None,
            "liquidationFee": decimal_to_float(liquidation_fee, precision=4) if cf_data_usable else None,
            "insuranceClear": decimal_to_float(insurance_clear, precision=4) if cf_data_usable else None,
            "bonus": decimal_to_float(bonus, precision=4) if cf_data_usable else None,
            "referralRebate": decimal_to_float(referral_rebate, precision=4) if cf_data_usable else None,
            "otherIncome": decimal_to_float(other_trading_income, precision=4) if cf_data_usable else None,
            "otherExternalFlow": decimal_to_float(other_external_flow, precision=4) if cf_data_usable else None,
            "unknownFlow": decimal_to_float(unknown_flow, precision=4) if cf_data_usable else None,
        },
        "reconciled": reconciled,
        "reconciliationDelta": decimal_to_float(reconciliation_delta, precision=4),
        "reconciliationResidual": decimal_to_float(reconciliation_delta, precision=4),
        "attribution": {
            cls: {
                "fillCount": b["fillCount"],
                "grossRealizedPnlUsdt": decimal_to_float(b["grossRealizedPnlUsdt"], precision=4) if fills_sync else None,
                "commissionUsdt": decimal_to_float(b["commissionUsdt"], precision=4) if fills_sync else None,
                "netPnlUsdt": decimal_to_float(b["netPnlUsdt"], precision=4) if fills_sync else None,
            }
            for cls, b in attr_buckets.items()
        },
        "bySymbol": by_symbol_out,
        "byBinding": by_binding_out,
        "nonUsdtFillCommissions": {k: decimal_to_float(v, precision=6) for k, v in non_usdt_fill_commissions.items()},
        "otherAssetCashflows": [
            {"asset": asset, "categories": {k: decimal_to_float(v, precision=6) for k, v in cats.items()}}
            for asset, cats in sorted(other_flows.items())
        ],
    }


def _downsample_preserving_extremes(rows: List[AccountEquitySnapshot], max_points: int = MAX_CURVE_POINTS) -> List[AccountEquitySnapshot]:
    if len(rows) <= max_points:
        return rows
    bucket_count = max(1, max_points // 4)
    step = len(rows) / bucket_count
    selected_indices: set[int] = set()
    for i in range(bucket_count):
        lo = int(i * step)
        hi = min(len(rows), int((i + 1) * step))
        if hi <= lo:
            continue
        selected_indices.add(lo)
        selected_indices.add(hi - 1)
        min_idx = min(range(lo, hi), key=lambda idx: rows[idx].equity)
        max_idx = max(range(lo, hi), key=lambda idx: rows[idx].equity)
        selected_indices.add(min_idx)
        selected_indices.add(max_idx)
    return [rows[idx] for idx in sorted(selected_indices)][:max_points]


async def compute_equity_curve(
    db: AsyncSession,
    account_id: int,
    range_name: str,
    now_ms: int,
    *,
    resolution: str = "auto",
    currency: str = "USDT",
    include_estimated: bool = False,
) -> Dict[str, Any]:
    start_ms, end_ms = parse_range_window(range_name, now_ms)
    span_ms = (end_ms - start_ms) if start_ms is not None else None
    req_res = resolution.strip().lower()
    if req_res == "auto":
        if span_ms is not None and span_ms <= 2 * DAY_MS:
            chosen_res = "raw"
        elif span_ms is not None and span_ms <= 45 * DAY_MS:
            chosen_res = "hour"
        else:
            chosen_res = "day"
    elif req_res in ("raw", "hour", "day"):
        chosen_res = req_res
    else:
        chosen_res = "hour"

    async def _query_res(res_name: str) -> List[AccountEquitySnapshot]:
        stmt = select(AccountEquitySnapshot).where(
            AccountEquitySnapshot.account_id == account_id,
            AccountEquitySnapshot.currency == currency.upper(),
            AccountEquitySnapshot.resolution == res_name,
            AccountEquitySnapshot.bucket_start_ms <= end_ms,
        )
        if not include_estimated:
            stmt = stmt.where(AccountEquitySnapshot.source != "estimated_backfill")
        if start_ms is not None:
            stmt = stmt.where(AccountEquitySnapshot.bucket_start_ms >= start_ms)
        return list((await db.scalars(stmt.order_by(AccountEquitySnapshot.bucket_start_ms.asc()))).all())

    rows = await _query_res(chosen_res)
    actual_res = chosen_res
    if not rows and req_res == "auto":
        for fallback_res in ("hour", "raw", "day"):
            if fallback_res == chosen_res:
                continue
            candidate = await _query_res(fallback_res)
            if candidate:
                rows = candidate
                actual_res = fallback_res
                break

    rows = _downsample_preserving_extremes(rows, MAX_CURVE_POINTS)

    # Fetch external flows & cashflow sync coverage for cumulative trading PnL and TWR chaining
    cf_sync = await db.scalar(
        select(AccountDatasetSyncState).where(
            AccountDatasetSyncState.account_id == account_id,
            AccountDatasetSyncState.dataset == "cashflow",
        )
    )
    ext_flows = list(
        (
            await db.scalars(
                select(AccountCashflowEntry)
                .where(
                    AccountCashflowEntry.account_id == account_id,
                    AccountCashflowEntry.asset == currency.upper(),
                    AccountCashflowEntry.category.in_(tuple(_EXTERNAL_FLOW_CATEGORIES)),
                    AccountCashflowEntry.occurred_at_ms <= end_ms,
                )
                .order_by(AccountCashflowEntry.occurred_at_ms.asc())
            )
        ).all()
    )
    if start_ms is not None:
        ext_flows_in_range = [f for f in ext_flows if f.occurred_at_ms >= start_ms]
    else:
        ext_flows_in_range = ext_flows

    first_pt_ts = rows[0].captured_at_ms if rows else None
    cashflow_covers_curve = bool(
        first_pt_ts is not None
        and cf_sync is not None
        and cf_sync.status == "ok"
        and cf_sync.coverage_start_ms is not None
        and cf_sync.coverage_start_ms <= first_pt_ts
    )

    flow_times = [f.occurred_at_ms for f in ext_flows_in_range]
    flow_prefix_sums: List[Decimal] = [Decimal("0")]
    for f in ext_flows_in_range:
        flow_prefix_sums.append(flow_prefix_sums[-1] + f.signed_amount)

    def _ext_sum_between(t_lo: int, t_hi: int) -> Decimal:
        hi_idx = bisect_right(flow_times, t_hi)
        lo_idx = bisect_right(flow_times, t_lo)
        return flow_prefix_sums[hi_idx] - flow_prefix_sums[lo_idx]

    gap_thresh = _GAP_THRESHOLD_MS.get(actual_res, 4 * 3_600_000)
    points: List[Dict[str, Any]] = []
    prev_bucket_ms: Optional[int] = None
    prev_captured_ms: Optional[int] = None
    baseline_eq = rows[0].equity if rows else None
    baseline_ts = rows[0].captured_at_ms if rows else None

    twr_idx: Optional[Decimal] = Decimal("1") if (rows and cashflow_covers_curve and rows[0].equity > 0) else None
    peak_twr: Optional[Decimal] = twr_idx
    max_dd_pct: Optional[Decimal] = Decimal("0") if twr_idx is not None else None
    current_dd_pct: Optional[Decimal] = None
    prev_eq: Optional[Decimal] = None

    for idx, row in enumerate(rows):
        gap_before = prev_bucket_ms is not None and (row.bucket_start_ms - prev_bucket_ms) > gap_thresh

        cum_trading_pnl: Optional[Decimal] = None
        if cashflow_covers_curve and baseline_eq is not None and baseline_ts is not None:
            cum_ext = _ext_sum_between(baseline_ts, row.captured_at_ms)
            cum_trading_pnl = row.equity - baseline_eq - cum_ext

        point_dd_pct: Optional[Decimal] = None
        if idx > 0 and twr_idx is not None and prev_eq is not None and prev_captured_ms is not None:
            step_ext = _ext_sum_between(prev_captured_ms, row.captured_at_ms)
            if prev_eq > 0 and (row.equity - step_ext) > 0:
                twr_idx = twr_idx * ((row.equity - step_ext) / prev_eq)
            elif prev_eq > 0 and step_ext == 0 and row.equity == 0:
                twr_idx = Decimal("0")
            else:
                # Invalid negative adjusted base due to clock skew or sub-zero equity
                twr_idx = None

        if twr_idx is not None:
            if peak_twr is None or twr_idx > peak_twr:
                peak_twr = twr_idx
            if peak_twr > 0:
                point_dd_pct = max(Decimal("0"), ((peak_twr - twr_idx) / peak_twr) * Decimal("100"))
                if max_dd_pct is None or point_dd_pct > max_dd_pct:
                    max_dd_pct = point_dd_pct
            current_dd_pct = point_dd_pct

        prev_bucket_ms = row.bucket_start_ms
        prev_captured_ms = row.captured_at_ms
        prev_eq = row.equity

        points.append(
            {
                "tsMs": row.captured_at_ms,
                "bucketStartMs": row.bucket_start_ms,
                "resolution": row.resolution,
                "source": row.source,
                "dataQuality": row.data_quality,
                "gapBefore": gap_before,
                "equity": decimal_to_float(row.equity, precision=4),
                "equityMin": decimal_to_float(row.equity_min, precision=4),
                "equityMax": decimal_to_float(row.equity_max, precision=4),
                "cumulativeTradingPnl": decimal_to_float(cum_trading_pnl, precision=4),
                "twrIndex": decimal_to_float(twr_idx, precision=6),
                "twrPct": decimal_to_float((twr_idx - Decimal("1")) * Decimal("100"), precision=4)
                if twr_idx is not None
                else None,
                "drawdownPct": decimal_to_float(point_dd_pct, precision=4),
                "walletBalance": decimal_to_float(row.wallet_balance, precision=4),
                "availableBalance": decimal_to_float(row.available_balance, precision=4),
                "unrealizedPnl": decimal_to_float(row.unrealized_pnl, precision=4),
                "totalNotional": decimal_to_float(row.total_notional, precision=4),
                "marginRatio": decimal_to_float(row.margin_ratio, precision=6),
                "effectiveLeverage": decimal_to_float(row.effective_leverage, precision=3),
            }
        )

    transfers_only = [f for f in ext_flows_in_range if f.category in ("TRANSFER_IN", "TRANSFER_OUT", "TRANSFER")]
    peak_eq = max((r.equity for r in rows), default=None)

    return {
        "currency": currency.upper(),
        "range": range_name,
        "resolution": actual_res,
        "cashflowCovered": cashflow_covers_curve,
        "cashflowCoverageStartMs": cf_sync.coverage_start_ms if cf_sync else None,
        "points": points,
        "transfers": [
            {
                "id": t.id,
                "tsMs": t.occurred_at_ms,
                "category": t.category,
                "signedAmount": decimal_to_float(t.signed_amount, precision=4),
                "asset": t.asset,
            }
            for t in transfers_only
        ],
        "currentDrawdownPct": decimal_to_float(current_dd_pct, precision=4),
        "maxDrawdownPct": decimal_to_float(max_dd_pct, precision=4),
        "summary": {
            "pointCount": len(points),
            "startEquity": points[0]["equity"] if points else None,
            "endEquity": points[-1]["equity"] if points else None,
            "peakEquity": decimal_to_float(peak_eq, precision=4),
            "twrPct": points[-1]["twrPct"] if points else None,
            "currentDrawdownPct": decimal_to_float(current_dd_pct, precision=4),
            "maxDrawdownPct": decimal_to_float(max_dd_pct, precision=4),
        },
    }


def _reconstruct_trade_cycles(
    fills: Sequence[AccountTradeFill],
    coverage_start_ms: Optional[int],
) -> Dict[str, Any]:
    """Reconstruct round-trip position cycles from chronological fills per (symbol, side)."""
    by_sym_side: Dict[Tuple[str, str], List[AccountTradeFill]] = defaultdict(list)
    for f in fills:
        ps = (f.position_side or "both").lower()
        if ps in ("", "net"):
            ps = "both"
        by_sym_side[(f.symbol.upper(), ps)].append(f)

    closed_pnls: List[Decimal] = []
    holding_durations: List[int] = []
    incomplete_cycles = 0

    for (sym, ps), group in by_sym_side.items():
        group.sort(key=lambda r: (r.occurred_at_ms, r.id))
        running_qty = Decimal("0")
        in_cycle = False
        cycle_incomplete = False
        cycle_open_ms = 0
        cycle_pnl = Decimal("0")
        has_rp = False

        for f in group:
            qty = abs(f.qty)
            rp = f.realized_pnl or Decimal("0")
            comm_usdt = (
                f.commission_amount
                if (f.commission_amount is not None and (f.commission_asset or "USDT").upper() == "USDT")
                else Decimal("0")
            )
            if ps in ("long", "short"):
                is_open = (ps == "long" and f.side.lower() == "buy") or (ps == "short" and f.side.lower() == "sell")
                delta = qty if is_open else -qty
            else:
                delta = qty if f.side.lower() == "buy" else -qty

            if not in_cycle:
                if ps in ("long", "short") and delta < 0:
                    # Closing fill before any observed opening fill -> started prior to coverage window
                    incomplete_cycles += 1
                    continue
                if delta == 0:
                    continue
                in_cycle = True
                cycle_open_ms = f.occurred_at_ms
                cycle_pnl = rp - comm_usdt
                has_rp = f.realized_pnl is not None
                running_qty = delta
                if ps == "both" and coverage_start_ms is not None and (f.occurred_at_ms - coverage_start_ms) < 60_000 and rp != 0:
                    cycle_incomplete = True
            else:
                new_qty = running_qty + delta
                cycle_pnl += rp - comm_usdt
                if f.realized_pnl is not None:
                    has_rp = True
                # Detect sign cross (one-way flip) or return to zero
                crossed_zero = (running_qty > 0 and new_qty <= 0) or (running_qty < 0 and new_qty >= 0)
                running_qty = new_qty
                if abs(running_qty) <= Decimal("0.00000001") or crossed_zero:
                    if cycle_incomplete:
                        incomplete_cycles += 1
                    elif sym.endswith("USDT") and has_rp:
                        closed_pnls.append(cycle_pnl)
                        holding_durations.append(max(0, f.occurred_at_ms - cycle_open_ms))
                    if abs(running_qty) <= Decimal("0.00000001"):
                        in_cycle = False
                        cycle_incomplete = False
                        cycle_pnl = Decimal("0")
                        has_rp = False
                        running_qty = Decimal("0")
                    else:
                        cycle_open_ms = f.occurred_at_ms
                        cycle_pnl = Decimal("0")
                        cycle_incomplete = False
                        has_rp = False

    win_pnls = [p for p in closed_pnls if p > 0]
    loss_pnls = [p for p in closed_pnls if p < 0]
    win_count = len(win_pnls)
    loss_count = len(loss_pnls)
    breakeven_count = len([p for p in closed_pnls if p == 0])
    decided = win_count + loss_count
    gross_win = sum(win_pnls, Decimal("0"))
    gross_loss = sum((abs(p) for p in loss_pnls), Decimal("0"))
    avg_win = (gross_win / Decimal(win_count)) if win_count > 0 else None
    avg_loss = (gross_loss / Decimal(loss_count)) if loss_count > 0 else None
    expectancy = (sum(closed_pnls, Decimal("0")) / Decimal(len(closed_pnls))) if closed_pnls else None

    return {
        "closedCycleCount": len(closed_pnls),
        "incompleteCycleCount": incomplete_cycles,
        "winCount": win_count,
        "lossCount": loss_count,
        "breakevenCount": breakeven_count,
        "winRatePct": finite_float((win_count / decided) * 100, precision=2) if decided > 0 else None,
        "payoffRatio": decimal_to_float(avg_win / avg_loss, precision=3) if (avg_win is not None and avg_loss is not None and avg_loss > 0) else None,
        "profitFactor": decimal_to_float(gross_win / gross_loss, precision=3) if gross_loss > 0 else None,
        "expectancyUsdt": decimal_to_float(expectancy, precision=4),
        "avgWinUsdt": decimal_to_float(avg_win, precision=4),
        "avgLossUsdt": decimal_to_float(avg_loss, precision=4),
        "largestWinUsdt": decimal_to_float(max(win_pnls), precision=4) if win_pnls else None,
        "largestLossUsdt": decimal_to_float(min(loss_pnls), precision=4) if loss_pnls else None,
        "avgHoldingDurationMs": int(sum(holding_durations) / len(holding_durations)) if holding_durations else None,
    }


def _percentile(sorted_vals: List[float], p: float) -> Optional[float]:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


async def compute_performance(
    db: AsyncSession,
    account_id: int,
    range_name: str,
    now_ms: int,
    *,
    binding_id: Optional[int] = None,
    symbol: Optional[str] = None,
    source_class: Optional[str] = None,
) -> Dict[str, Any]:
    start_ms, end_ms = parse_range_window(range_name, now_ms)

    fills_q = select(AccountTradeFill).where(
        AccountTradeFill.account_id == account_id,
        AccountTradeFill.occurred_at_ms <= end_ms,
    )
    if start_ms is not None:
        fills_q = fills_q.where(AccountTradeFill.occurred_at_ms >= start_ms)
    if binding_id is not None:
        fills_q = fills_q.where(AccountTradeFill.binding_id == binding_id)
    if symbol:
        fills_q = fills_q.where(AccountTradeFill.symbol == symbol.upper())
    if source_class and source_class in ("system", "manual", "unknown"):
        fills_q = fills_q.where(AccountTradeFill.source_class == source_class)

    fills = list((await db.scalars(fills_q.order_by(AccountTradeFill.occurred_at_ms.desc()))).all())

    fills_sync = await db.scalar(
        select(AccountDatasetSyncState).where(
            AccountDatasetSyncState.account_id == account_id,
            AccountDatasetSyncState.dataset == "fills",
        )
    )
    orders_sync = await db.scalar(
        select(AccountDatasetSyncState).where(
            AccountDatasetSyncState.account_id == account_id,
            AccountDatasetSyncState.dataset == "orders",
        )
    )
    rates_sync = await db.scalar(
        select(AccountDatasetSyncState).where(
            AccountDatasetSyncState.account_id == account_id,
            AccountDatasetSyncState.dataset == "commission_rates",
        )
    )

    volume_usd = Decimal("0")
    gross_rp_usdt = Decimal("0")
    commission_usdt = Decimal("0")
    non_usdt_commissions: Dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    maker_count = 0
    taker_count = 0

    by_symbol: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "symbol": "",
            "fillCount": 0,
            "volumeUsd": Decimal("0"),
            "realizedPnlUsdt": Decimal("0"),
            "commissionUsdt": Decimal("0"),
            "winFills": 0,
            "lossFills": 0,
            "makerCount": 0,
            "takerCount": 0,
        }
    )

    for f in fills:
        sym = f.symbol.upper()
        sb = by_symbol[sym]
        sb["symbol"] = sym
        sb["fillCount"] += 1

        q_amt = f.quote_qty if f.quote_qty is not None else (f.qty * f.price)
        volume_usd += abs(q_amt)
        sb["volumeUsd"] += abs(q_amt)

        if f.maker is True:
            maker_count += 1
            sb["makerCount"] += 1
        elif f.maker is False:
            taker_count += 1
            sb["takerCount"] += 1

        is_usdt_sym = sym.endswith("USDT")
        rp = f.realized_pnl
        if rp is not None and is_usdt_sym:
            gross_rp_usdt += rp
            sb["realizedPnlUsdt"] += rp
            if rp > 0:
                sb["winFills"] += 1
            elif rp < 0:
                sb["lossFills"] += 1

        if f.commission_amount is not None:
            c_asset = (f.commission_asset or "USDT").upper()
            if c_asset == "USDT":
                commission_usdt += f.commission_amount
                sb["commissionUsdt"] += f.commission_amount
            else:
                non_usdt_commissions[c_asset] += f.commission_amount

    known_maker_fills = maker_count + taker_count
    cycles = _reconstruct_trade_cycles(fills, fills_sync.coverage_start_ms if fills_sync else None)

    exec_q = select(ExecutionRecord).where(ExecutionRecord.account_id == account_id)
    if start_ms is not None:
        exec_q = exec_q.where(ExecutionRecord.created_at >= datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc))
    if binding_id is not None:
        exec_q = exec_q.where(ExecutionRecord.binding_id == binding_id)
    if symbol:
        exec_q = exec_q.where(ExecutionRecord.symbol == symbol.upper())
    executions = list((await db.scalars(exec_q)).all())

    intents_by_oid: Dict[str, OrderIntent] = {}
    fills_by_oid: Dict[str, int] = {}
    if executions:
        intents = list((await db.scalars(select(OrderIntent).where(OrderIntent.account_id == account_id))).all())
        for intent in intents:
            if intent.exchange_order_id:
                intents_by_oid[intent.exchange_order_id] = intent
        for fill in fills:
            if fill.exchange_order_id:
                prev_ms = fills_by_oid.get(fill.exchange_order_id)
                if prev_ms is None or fill.occurred_at_ms < prev_ms:
                    fills_by_oid[fill.exchange_order_id] = fill.occurred_at_ms

    status_counts: Dict[str, int] = defaultdict(int)
    slippage_vals: List[float] = []
    spread_vals: List[float] = []
    latency_vals: List[float] = []
    exact_latency_count = 0
    estimated_latency_count = 0
    executed_count = 0
    failed_count = 0
    blocked_count = 0
    skipped_count = 0

    for ex in executions:
        st = (ex.status or "").upper()
        status_counts[st] += 1
        if st in ("EXECUTED", "FILLED", "SUCCESS"):
            executed_count += 1
            if ex.slippage_pct is not None:
                sf = finite_float(ex.slippage_pct)
                if sf is not None:
                    slippage_vals.append(sf)
            intent = intents_by_oid.get(ex.order_id or "")
            if intent is not None and intent.source_time_ms:
                fill_ms = fills_by_oid.get(ex.order_id or "")
                if fill_ms is not None:
                    if fill_ms >= intent.source_time_ms:
                        latency_vals.append(float(fill_ms - intent.source_time_ms))
                        exact_latency_count += 1
                elif ex.created_at is not None:
                    est_ms = (dt_to_ms(ex.created_at) or 0) - intent.source_time_ms
                    if est_ms >= 0:
                        latency_vals.append(float(est_ms))
                        estimated_latency_count += 1
        elif "FAIL" in st or "ERROR" in st:
            failed_count += 1
        elif "BLOCK" in st or "LIMIT" in st:
            blocked_count += 1
        elif "SKIP" in st:
            skipped_count += 1
        if ex.spread_pct is not None:
            spf = finite_float(ex.spread_pct)
            if spf is not None:
                spread_vals.append(spf)

    slippage_sorted = sorted(slippage_vals)
    latency_sorted = sorted(latency_vals)

    commission_rates: List[Dict[str, Any]] = []
    if rates_sync and rates_sync.cursor_json:
        try:
            parsed = json.loads(rates_sync.cursor_json)
            if isinstance(parsed, list):
                commission_rates = [
                    {
                        "symbol": str(item.get("symbol") or ""),
                        "instId": str(item.get("instId") or ""),
                        "makerRate": finite_float(item.get("makerRate")),
                        "takerRate": finite_float(item.get("takerRate")),
                    }
                    for item in parsed
                    if isinstance(item, dict)
                ]
        except (ValueError, TypeError):
            commission_rates = []

    by_symbol_out = sorted(
        [
            {
                "symbol": sb["symbol"],
                "fillCount": sb["fillCount"],
                "volumeUsd": decimal_to_float(sb["volumeUsd"], precision=4),
                "realizedPnlUsdt": decimal_to_float(sb["realizedPnlUsdt"], precision=4),
                "commissionUsdt": decimal_to_float(sb["commissionUsdt"], precision=4),
                "netPnlUsdt": decimal_to_float(sb["realizedPnlUsdt"] - sb["commissionUsdt"], precision=4),
                "winCount": sb["winFills"],
                "lossCount": sb["lossFills"],
                "winRatePct": finite_float((sb["winFills"] / (sb["winFills"] + sb["lossFills"])) * 100, precision=2)
                if (sb["winFills"] + sb["lossFills"]) > 0
                else None,
                "makerRatioPct": finite_float((sb["makerCount"] / (sb["makerCount"] + sb["takerCount"])) * 100, precision=2)
                if (sb["makerCount"] + sb["takerCount"]) > 0
                else None,
            }
            for sb in by_symbol.values()
        ],
        key=lambda x: abs(x["volumeUsd"] or 0.0),
        reverse=True,
    )

    fills_status = fills_sync.status if fills_sync else "unavailable"
    return {
        "range": range_name,
        "fillsStatus": fills_status,
        "fillsCoverageStartMs": fills_sync.coverage_start_ms if fills_sync else None,
        "fillsCoverageEndMs": fills_sync.coverage_end_ms if fills_sync else None,
        "ordersStatus": orders_sync.status if orders_sync else "unavailable",
        "tradeSummary": {
            "fillCount": len(fills),
            "volumeUsd": decimal_to_float(volume_usd, precision=4) if fills_sync else None,
            "grossRealizedPnlUsdt": decimal_to_float(gross_rp_usdt, precision=4) if fills_sync else None,
            "commissionUsdt": decimal_to_float(commission_usdt, precision=4) if fills_sync else None,
            "nonUsdtCommissions": {k: decimal_to_float(v, precision=6) for k, v in non_usdt_commissions.items()},
            "netRealizedPnlUsdt": decimal_to_float(gross_rp_usdt - commission_usdt, precision=4) if fills_sync else None,
            "makerCount": maker_count,
            "takerCount": taker_count,
            "makerRatioPct": finite_float((maker_count / known_maker_fills) * 100, precision=2)
            if known_maker_fills > 0
            else None,
            **cycles,
        },
        "executionQuality": {
            "totalExecutions": len(executions),
            "executedCount": executed_count,
            "failedCount": failed_count,
            "blockedCount": blocked_count,
            "skippedCount": skipped_count,
            "successRatePct": finite_float((executed_count / len(executions)) * 100, precision=2)
            if executions
            else None,
            "avgSlippagePct": finite_float(sum(slippage_vals) / len(slippage_vals), precision=4)
            if slippage_vals
            else None,
            "medianSlippagePct": finite_float(_percentile(slippage_sorted, 0.5), precision=4),
            "p95SlippagePct": finite_float(_percentile(slippage_sorted, 0.95), precision=4),
            "maxSlippagePct": finite_float(max(slippage_vals, key=lambda x: abs(x)), precision=4)
            if slippage_vals
            else None,
            "avgSpreadPct": finite_float(sum(spread_vals) / len(spread_vals), precision=4)
            if spread_vals
            else None,
            "spreadSampleCount": len(spread_vals),
            "avgLatencyMs": int(sum(latency_vals) / len(latency_vals)) if latency_vals else None,
            "p95LatencyMs": finite_float(_percentile(latency_sorted, 0.95), precision=1),
            "exactLatencyCount": exact_latency_count,
            "estimatedLatencyCount": estimated_latency_count,
            "statusCounts": dict(status_counts),
        },
        "commissionRates": commission_rates,
        "bySymbol": by_symbol_out,
    }


def compute_bindings_summary(
    bindings: Sequence[CopyBinding],
    leads_by_id: Dict[int, LeadSource],
    positions_summary: Dict[str, Any],
    pnl_breakdown: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    by_binding_notional: Dict[int, float] = defaultdict(float)
    by_binding_pos_count: Dict[int, int] = defaultdict(int)
    for pos in positions_summary["positions"]:
        qty = pos.get("qty") or 0.0
        notional = pos.get("notionalUsd") or 0.0
        for alloc in pos.get("allocations", []):
            bid = alloc["bindingId"]
            by_binding_pos_count[bid] += 1
            if qty > 0:
                by_binding_notional[bid] += notional * min(1.0, (alloc.get("ownedQty") or 0.0) / qty)

    pnl_by_binding = {
        row["bindingId"]: row for row in (pnl_breakdown.get("byBinding", []) if pnl_breakdown else [])
    }

    out: List[Dict[str, Any]] = []
    for b in bindings:
        lead = leads_by_id.get(b.lead_id)
        pb = pnl_by_binding.get(b.id)
        out.append(
            {
                "id": b.id,
                "name": b.name,
                "leadId": b.lead_id,
                "leadName": lead.name if lead else f"Lead #{b.lead_id}",
                "isEnabled": bool(b.is_enabled),
                "riskScale": finite_float(b.risk_scale),
                "orderMode": b.order_mode,
                "tpSlMode": b.tp_sl_mode,
                "circuitBreakerTripped": bool(b.circuit_breaker_tripped),
                "circuitBreakerReason": sanitize_error_text(b.circuit_breaker_reason),
                "consecutiveFailures": b.consecutive_failures,
                "allocatedPositionsCount": by_binding_pos_count.get(b.id, 0),
                "allocatedNotionalUsd": finite_float(by_binding_notional.get(b.id, 0.0), precision=2),
                "periodNetPnlUsdt": pb.get("netPnlUsdt") if pb else None,
                "periodFillCount": pb.get("fillCount") if pb else None,
            }
        )
    return out
