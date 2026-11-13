from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import hashlib
from datetime import datetime, timezone
from decimal import Decimal, ROUND_UP
from typing import Any, Callable, Coroutine, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import DEFAULT_SYSTEM_CONFIG
from app.core.security import CredentialVault
from app.core.secret_mask import redact_proxy_userinfo
from app.schemas.api import ResilienceSettings, RiskDefaults
from app.models.domain import (
    BindingDebt,
    BindingDelivery,
    BindingSourceProgress,
    OrderIntent,
    PositionAllocation,
    CopyBinding,
    ExecutionRecord,
    LeadSource,
    PositionGuardState,
    ProxyNode,
    SystemConfig,
    TradingAccount,
)
from app.services.adapters.base import (
    BaseExchangeAdapter,
    ExchangePosition,
    OrderResult,
)
from app.services.adapters.factory import create_exchange_adapter
from app.services.engine.risk import RiskEngine
from app.services.engine.sizer import PositionSizer, exact, format_decimal, quantize_down
from app.services.engine.serialization import serialized_execution
from app.services.lead_collector.binance_bapi import (
    direction_label,
    expand_slim_order,
    group_orders_into_batches,
    is_open_leg,
    order_dedup_key,
    is_authoritative_snapshot,
    source_fill_kind,
    validate_source_fill,
)
from app.services.notifier.outbox import enqueue_notification
from app.services.proxy.manager import ProxyManager

logger = logging.getLogger("mirrorquant.router")

BroadcastFn = Optional[Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]]


class UnknownOrderOutcome(RuntimeError):
    """A mutation may have been accepted; reconciliation must precede further work."""


TERMINAL_STATES = {"filled", "canceled", "rejected", "expired"}


class CopyRouter:
    """Routes lead order batches & closures across N:M CopyBindings with risk, TP/SL & resilience."""

    def __init__(self, vault: CredentialVault, broadcast_fn: BroadcastFn = None):
        self.vault = vault
        self.broadcast_fn = broadcast_fn
        self._orphan_alert_ts: Dict[str, float] = {}

    async def _emit(self, event: Dict[str, Any]) -> None:
        if self.broadcast_fn:
            try:
                await asyncio.wait_for(self.broadcast_fn(event), timeout=1.0)
            except Exception:
                pass

    @staticmethod
    async def _get_config(db: AsyncSession, key: str, default: Any) -> Any:
        row = await db.scalar(select(SystemConfig).where(SystemConfig.key == key).execution_options(populate_existing=True))
        if row:
            val = json.loads(row.value_json)
            if isinstance(default, dict) and isinstance(val, dict):
                val = {**default, **val}
            schema = {"resilience": ResilienceSettings, "risk_defaults": RiskDefaults}.get(key)
            if schema is not None:
                return schema.model_validate(val).model_dump()
            if key == "global_kill_switch" and not isinstance(val, bool):
                raise ValueError("Invalid persisted kill switch")
            return val
        return default

    async def _get_debt(
        self, db: AsyncSession, binding_id: int, symbol: str, position_side: str
    ) -> BindingDebt:
        debt = await db.scalar(
            select(BindingDebt).where(
                BindingDebt.binding_id == binding_id,
                BindingDebt.symbol == symbol,
                BindingDebt.position_side == position_side,
            )
        )
        if debt is None:
            debt = BindingDebt(
                binding_id=binding_id,
                symbol=symbol,
                position_side=position_side,
                pending_usd=0.0,
            )
            db.add(debt)
            await db.flush()
        return debt

    async def _upsert_guard(
        self,
        db: AsyncSession,
        binding_id: int,
        inst_id: str,
        pos_side: str,
        entry_px: float,
    ) -> PositionGuardState:
        guard = await db.scalar(
            select(PositionGuardState).where(
                PositionGuardState.binding_id == binding_id,
                PositionGuardState.inst_id == inst_id,
                PositionGuardState.pos_side == pos_side,
            )
        )
        if guard is None:
            guard = PositionGuardState(
                binding_id=binding_id,
                inst_id=inst_id,
                pos_side=pos_side,
                entry_px=entry_px,
                peak_px=entry_px,
                trough_px=entry_px,
            )
            db.add(guard)
        else:
            if entry_px > 0:
                guard.entry_px = entry_px
                guard.peak_px = max(guard.peak_px or entry_px, entry_px)
                guard.trough_px = min(guard.trough_px or entry_px, entry_px)
        await db.flush()
        return guard

    async def _clear_guard(
        self, db: AsyncSession, binding_id: int, inst_id: str, pos_side: str
    ) -> None:
        guard = await db.scalar(
            select(PositionGuardState).where(
                PositionGuardState.binding_id == binding_id,
                PositionGuardState.inst_id == inst_id,
                PositionGuardState.pos_side == pos_side,
            )
        )
        if guard is not None:
            await db.delete(guard)

    @staticmethod
    def _refresh_day_start(account: TradingAccount, equity: float) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if account.day_start_date != today or (account.day_start_equity or 0.0) <= 0:
            account.day_start_date = today
            account.day_start_equity = equity

    @staticmethod
    async def _place_order_idempotent(
        adapter: BaseExchangeAdapter,
        inst_id: str,
        side: str,
        pos_side: str,
        ord_type: str,
        sz: str,
        px: Optional[str],
        cl_ord_id: str,
        reduce_only: bool,
        td_mode: str,
    ) -> OrderResult:
        """Submit once; an ambiguous result authorizes lookup, never another POST."""
        try:
            return await adapter.place_order(
                inst_id=inst_id, side=side, pos_side=pos_side, ord_type=ord_type,
                sz=sz, px=px, cl_ord_id=cl_ord_id, reduce_only=reduce_only, td_mode=td_mode,
            )
        except Exception as first_exc:
            try:
                existing = await adapter.get_order(inst_id, cl_ord_id=cl_ord_id)
                if existing and existing.get("ordId"):
                    return OrderResult(
                        ord_id=str(existing["ordId"]), cl_ord_id=cl_ord_id,
                        fill_sz=float(existing.get("accFillSz") or 0),
                        avg_px=float(existing.get("avgPx") or 0),
                        state=str(existing.get("state") or "submitted"), raw=existing,
                    )
            except Exception:
                logger.exception("Order lookup failed for %s", cl_ord_id)
            logger.critical("Order placement outcome ambiguous for %s: %s", cl_ord_id, first_exc)
            raise UnknownOrderOutcome(f"Order placement outcome ambiguous for {cl_ord_id}") from first_exc

    async def _get_allocation(
        self, db: AsyncSession, binding_id: int, account_id: int, inst_id: str, pos_side: str,
    ) -> PositionAllocation:
        allocation = await db.scalar(select(PositionAllocation).where(
            PositionAllocation.binding_id == binding_id, PositionAllocation.inst_id == inst_id,
            PositionAllocation.pos_side == pos_side,
        ))
        if allocation is None:
            allocation = PositionAllocation(binding_id=binding_id, account_id=account_id,
                inst_id=inst_id, pos_side=pos_side, owned_qty=0.0, entry_cost=0.0,
                generation=0, opened_at_ms=0)
            db.add(allocation)
            await db.flush()
        return allocation

    @staticmethod
    def _owned_qty(allocation):
        return exact(allocation.owned_qty_exact if allocation.owned_qty_exact is not None else allocation.owned_qty)

    @staticmethod
    def _entry_cost(allocation):
        return exact(allocation.entry_cost_exact if allocation.entry_cost_exact is not None else allocation.entry_cost)

    @staticmethod
    def _pending_debt(debt):
        return exact(debt.pending_usd_exact if debt.pending_usd_exact is not None else debt.pending_usd)

    @staticmethod
    def _set_debt(debt, value):
        debt.pending_usd_exact = str(exact(value))
        debt.pending_usd = float(value)

    @staticmethod
    def _state(value: Any) -> str:
        raw = str(value or "unknown").lower().replace("-", "_")
        return {"cancelled": "canceled", "partiallyfilledcanceled": "canceled",
                "partially_filled_canceled": "canceled", "deactivated": "canceled",
                "full_fill": "filled", "live": "new", "partiallyfilled": "partially_filled"}.get(raw, raw)

    @staticmethod
    def _confirmed_fill(info: Optional[Dict[str, Any]], res: OrderResult) -> float:
        raw = info.get("accFillSz") if info is not None else None
        value = exact(raw if raw is not None else (res.fill_sz if res.fill_sz is not None else 0))
        if value < 0:
            raise ValueError("Invalid negative cumulative fill")
        return float(value)

    async def _apply_intent_info(self, db, intent, info, spec, source_time_ms=0):
        cumulative = exact(info.get("accFillSz") if info.get("accFillSz") is not None else intent.filled_qty)
        prior = exact(intent.filled_qty)
        if cumulative < prior or cumulative > exact(intent.requested_qty):
            raise UnknownOrderOutcome(f"Invalid cumulative fill for {intent.id}")
        delta = cumulative - prior
        avg_px = exact(info.get("avgPx") or intent.avg_px)
        if delta > 0 and avg_px <= 0:
            raise UnknownOrderOutcome(f"Missing fill price for {intent.id}")
        allocation = await self._get_allocation(db, intent.binding_id, intent.account_id, intent.inst_id, intent.pos_side)
        debt = await self._get_debt(db, intent.binding_id, intent.inst_id, intent.pos_side.upper())
        if delta > 0:
            owned = self._owned_qty(allocation)
            if intent.reduce_only:
                if delta > owned:
                    raise UnknownOrderOutcome("Confirmed close exceeds binding allocation")
                remaining = max(Decimal("0"), owned - delta)
                allocation.entry_cost_exact = str(self._entry_cost(allocation) * remaining / owned if owned else Decimal("0"))
                allocation.entry_cost = float(allocation.entry_cost_exact)
                allocation.owned_qty_exact = str(remaining)
                allocation.owned_qty = float(remaining)
                if remaining > 0 and owned > 0:
                    self._set_debt(debt, self._pending_debt(debt) * remaining / owned)
                if remaining == 0:
                    self._set_debt(debt, 0)
                    await self._clear_guard(db, intent.binding_id, intent.inst_id, intent.pos_side)
            else:
                if owned == 0:
                    allocation.generation += 1
                    allocation.opened_at_ms = source_time_ms or intent.source_time_ms or int(time.time() * 1000)
                    await self._clear_guard(db, intent.binding_id, intent.inst_id, intent.pos_side)
                # Average fill prices are cumulative: subtract previous cost, not delta * average.
                new_cost = cumulative * avg_px - prior * exact(intent.avg_px)
                if new_cost <= 0:
                    raise UnknownOrderOutcome(f"Invalid incremental fill cost for {intent.id}")
                allocation.entry_cost_exact = str(self._entry_cost(allocation) + new_cost)
                allocation.entry_cost = float(allocation.entry_cost_exact)
                allocation.owned_qty_exact = str(owned + delta)
                allocation.owned_qty = float(allocation.owned_qty_exact)
                self._set_debt(debt, max(Decimal("0"), exact(intent.target_usd) - cumulative * avg_px * spec.ct_val))
                await self._upsert_guard(db, intent.binding_id, intent.inst_id, intent.pos_side,
                    float(self._entry_cost(allocation) / self._owned_qty(allocation)))
        intent.filled_qty = str(cumulative)
        intent.avg_px = str(avg_px)
        intent.exchange_order_id = str(info.get("ordId") or intent.exchange_order_id or "") or None
        state = self._state(info.get("state"))
        intent.state = state.upper() if state in TERMINAL_STATES else "UNKNOWN"
        if state in TERMINAL_STATES:
            delivery = await db.get(BindingDelivery, (intent.binding_id, intent.source_event_id))
            if delivery is not None:
                delivery.state = "APPLIED"
        await db.commit()  # Confirmed fill and its allocation/debt commit together.
        return float(delta), float(avg_px), state

    async def _submit_confirmed(self, db, binding, account, adapter, spec,
                                source_event_id, target_usd=0, source_time_ms=0, observed_at_ms=0, **order):
        material = f"{account.id}:{binding.id}:{source_event_id}"
        cid = "mq" + hashlib.sha256(material.encode()).hexdigest()[:30]
        order["cl_ord_id"] = cid
        intent = await db.get(OrderIntent, cid)
        if intent is None:
            if not order["reduce_only"]:
                if observed_at_ms and not 0 <= int(time.time() * 1000) - int(observed_at_ms) <= 30000:
                    raise RuntimeError("Lead snapshot expired before submission")
                # Reload policy immediately before the durable submission boundary.
                await db.refresh(binding)
                await db.refresh(account)
                if (not binding.is_enabled or not account.is_enabled or
                        await self._get_config(db, "global_kill_switch", False)):
                    raise RuntimeError("Opening admission disabled")
            intent = OrderIntent(id=cid, binding_id=binding.id, account_id=account.id,
                inst_id=order["inst_id"], pos_side=order["pos_side"],
                source_event_id=source_event_id, state="SUBMITTING", requested_qty=order["sz"],
                filled_qty="0", avg_px="0", reduce_only=order["reduce_only"], target_usd=str(target_usd), source_time_ms=source_time_ms)
            db.add(intent)
            await db.commit()  # Durable before POST; restart can only query this client ID.
            try:
                res = await self._place_order_idempotent(adapter=adapter, **order)
            except BaseException:
                intent.state = "UNKNOWN"
                await db.commit()
                raise
        else:
            if intent.state.lower() in TERMINAL_STATES:
                return OrderResult(intent.exchange_order_id or "", cid, 0.0, float(intent.avg_px), intent.state.lower())
            res = OrderResult(intent.exchange_order_id or "", cid, float(intent.filled_qty), float(intent.avg_px))
        # ACK is not a fill. Poll briefly; unresolved outcomes are durable and block increases.
        for attempt in range(4):
            try:
                info = await adapter.get_order(order["inst_id"], cl_ord_id=cid)
            except Exception as exc:
                intent.state = "UNKNOWN"
                await db.commit()
                raise UnknownOrderOutcome(f"Order lookup failed for {cid}") from exc
            if info is None and self._state(res.state) in TERMINAL_STATES:
                info = {"ordId": res.ord_id, "accFillSz": res.fill_sz, "avgPx": res.avg_px, "state": res.state}
            if info is not None:
                info = dict(info)
                if info.get("accFillSz") is None:
                    info["accFillSz"] = res.fill_sz if res.fill_sz is not None and res.fill_sz > 0 else intent.filled_qty
                if info.get("avgPx") in (None, "") and res.avg_px:
                    info["avgPx"] = res.avg_px
                delta, price, state = await self._apply_intent_info(db, intent, info, spec, source_time_ms)
                if state in TERMINAL_STATES:
                    return OrderResult(intent.exchange_order_id or "", cid, delta, price, state)
                if delta > 0:
                    # Allocation is already protected; avoid losing this delta in execution accounting.
                    await self._record_execution(db, binding, await db.get(LeadSource, binding.lead_id),
                        account, order["inst_id"], order["inst_id"], order["side"], order["pos_side"],
                        "PARTIAL_FILL", "PARTIAL", 0, 0, 0, my_qty=delta, my_price=price,
                        order_id=intent.exchange_order_id, details="Partial fill confirmed; remaining order unresolved")
                    await db.commit()
            if attempt < 3:
                await asyncio.sleep(0.1)
        intent.state = "UNKNOWN"
        await db.commit()
        raise UnknownOrderOutcome(f"Order placement outcome ambiguous for {cid}")

    async def reconcile_pending_orders(self, db, account, adapter, instruments):
        intents = (await db.scalars(select(OrderIntent).where(
            OrderIntent.account_id == account.id,
            OrderIntent.state.notin_([s.upper() for s in TERMINAL_STATES]),
        ))).all()
        unresolved = False
        for intent in intents:
            spec = instruments.get(intent.inst_id)
            if spec is None:
                unresolved = True
                continue
            try:
                await db.commit()
                info = await adapter.get_order(intent.inst_id, cl_ord_id=intent.id)
                if info is None:
                    unresolved = True
                    continue
                _, _, state = await self._apply_intent_info(db, intent, info, spec)
                unresolved |= state not in TERMINAL_STATES
            except Exception:
                logger.exception("Pending order remains unresolved: %s", intent.id)
                unresolved = True
        return not unresolved

    async def _record_execution(
        self,
        db: AsyncSession,
        binding: CopyBinding,
        lead: LeadSource,
        account: TradingAccount,
        symbol: str,
        inst_id: str,
        side: str,
        position_side: str,
        action: str,
        status: str,
        lead_qty: float,
        lead_price: float,
        lead_notional: float,
        my_qty: float = 0.0,
        my_price: float = 0.0,
        my_notional: float = 0.0,
        slippage_pct: float = 0.0,
        order_id: Optional[str] = None,
        details: str = "",
        notify_cfg: Optional[Dict[str, Any]] = None,
    ) -> ExecutionRecord:
        details = redact_proxy_userinfo(details)
        rec = ExecutionRecord(
            binding_id=binding.id,
            lead_id=lead.id,
            account_id=account.id,
            lead_name=lead.name,
            account_name=account.name,
            symbol=symbol,
            inst_id=inst_id,
            side=side,
            position_side=position_side,
            action=action,
            status=status,
            lead_qty=float(lead_qty),
            lead_price=float(lead_price),
            lead_notional=float(lead_notional),
            my_qty=my_qty,
            my_price=my_price,
            my_notional=my_notional,
            slippage_pct=slippage_pct,
            order_id=order_id,
            details=details,
        )
        db.add(rec)
        delivery = db.info.get("delivery")
        if delivery is not None and status not in ("FAILED", "UNKNOWN", "PARTIAL"):
            delivery.state = "APPLIED"
        await db.flush()

        payload = {
            "type": "execution",
            "data": {
                "id": rec.id,
                "bindingId": binding.id,
                "leadName": lead.name,
                "accountName": account.name,
                "symbol": symbol,
                "instId": inst_id,
                "action": action,
                "status": status,
                "myQty": my_qty,
                "myPrice": my_price,
                "myNotional": my_notional,
                "slippagePct": slippage_pct,
                "details": details,
                "createdAt": rec.created_at.isoformat() if rec.created_at else None,
            },
        }
        if notify_cfg:
            should_notify = (
                (status == "EXECUTED" and notify_cfg.get("notify_on_execution", True))
                or (
                    (status.startswith("BLOCKED") or status == "FAILED" or status.startswith("SKIPPED_STALE") or status == "SKIPPED_UNSUPPORTED")
                    and notify_cfg.get("notify_on_risk_block", True)
                )
            )
            if should_notify:
                title = f"MirrorQuant [{status}] {symbol}"
                exch_label = "欧易" if "okx" in str(account.exchange).lower() else account.exchange
                is_demo = str(account.environment).lower() in ("demo", "simulated")
                tag = f"{exch_label}{'模拟盘' if is_demo else ''}跟单"

                action_map = {
                    "OPEN_LONG": "开多", "ADD_LONG": "加多", "OPEN_SHORT": "开空", "ADD_SHORT": "加空",
                    "FULL_CLOSE": "全额平仓", "PARTIAL_REDUCE": "减仓",
                    "CLOSE_LONG": "平多", "REDUCE_LONG": "减多", "CLOSE_SHORT": "平空", "REDUCE_SHORT": "减空",
                }
                act_str = action_map.get(action, action)
                if status == "EXECUTED":
                    px_str = f"{my_price:g}" if isinstance(my_price, (int, float)) else str(my_price)
                    notional_val = f" (~${my_notional:,.0f})" if my_notional else ""
                    msg = f"{tag}: 已同步{act_str} {my_qty}张 均价 {px_str}{notional_val}"
                elif status == "SKIPPED_UNSUPPORTED":
                    msg = f"{tag}: 模拟盘未上线该合约 ({symbol})，已跳过"
                elif status == "SKIPPED_NO_LOCAL_POS":
                    msg = f"{tag}: 本地无 {symbol} 持仓，跳过平仓"
                elif status.startswith("BLOCKED_SLIPPAGE"):
                    msg = f"{tag}: 滑点拦截 {symbol} ({details})"
                elif status.startswith("BLOCKED"):
                    msg = f"{tag}: 风控拦截 {symbol} ({status})"
                elif status == "FAILED":
                    msg = f"{tag}: 下单失败 {symbol} ({details})"
                else:
                    msg = f"{tag}: {status} {symbol} ({details})"

                enqueue_notification(db, notify_cfg, title, msg, event_type=status, execution_id=rec.id)
        await db.commit()  # The execution and notification become visible together.
        await self._emit(payload)
        return rec

    @serialized_execution
    async def process_lead_events(
        self, db: AsyncSession, lead: LeadSource, raw_orders: List[Dict[str, Any]],
        raw_closed: List[Dict[str, Any]], snapshot: Dict[str, Any], proxies: List[ProxyNode],
    ) -> None:
        """Enqueue cumulative order deltas or each independent fill once per follower."""
        if not is_authoritative_snapshot(snapshot):
            return
        await db.refresh(lead)
        latest = json.loads(lead.snapshot_json or "{}")
        latest_observed = (latest.get("openPositionsMeta") or {}).get("asOf") or 0
        failed_at = (latest.get("openPositionsMeta") or {}).get("lastFailureStartedAtMs") or 0
        if (latest_observed > snapshot["openPositionsMeta"]["asOf"]
                or failed_at >= snapshot["openPositionsMeta"]["asOf"]):
            return
        seen_keys = list(json.loads(lead.seen_order_keys_json or "[]"))
        seen_closed = list(json.loads(lead.seen_closed_ids_json or "[]"))
        old_seen = set(seen_keys)
        expanded_orders = sorted([expand_slim_order(o) for o in raw_orders],
            key=lambda o: (o.get("orderTime") or 0, order_dedup_key(o)))
        bindings = (await db.scalars(select(CopyBinding).where(CopyBinding.lead_id == lead.id)
                    .execution_options(populate_existing=True))).all()
        for binding in bindings:
            baseline = not binding.baseline_seeded
            for order in expanded_orders:
                key = order_dedup_key(order)
                cumulative, price = validate_source_fill(order.get("executedQty"), order.get("avgPrice"))
                notional = cumulative * price
                progress = await db.get(BindingSourceProgress, (binding.id, key))
                if progress is None:
                    for alias in order.get("_legacy_source_keys", []):
                        progress = await db.get(BindingSourceProgress, (binding.id, alias))
                        if progress is not None:
                            progress.source_key = key
                            break
                if progress is None:
                    progress = BindingSourceProgress(binding_id=binding.id, source_key=key,
                        cumulative_qty="0", cumulative_notional="0")
                    db.add(progress)
                    legacy_payload = [order.get("orderTime"), order.get("symbol"), order.get("side"),
                        order.get("positionSide"), order.get("type"), float(cumulative), float(order.get("avgPrice") or 0)]
                    legacy_key = hashlib.sha1(json.dumps(legacy_payload, sort_keys=True).encode()).hexdigest()[:24]
                    if baseline or key in old_seen or legacy_key in old_seen or any(k in old_seen for k in order.get("_legacy_keys", [])):
                        progress.cumulative_qty, progress.cumulative_notional = str(cumulative), str(notional)
                        continue
                previous = exact(progress.cumulative_qty)
                if source_fill_kind(order) == "individual":
                    if previous not in (0, cumulative) or (previous > 0 and exact(progress.cumulative_notional) != notional):
                        logger.error("Conflicting individual source fill %s; suspending source event", key)
                        continue
                elif cumulative < previous:
                    logger.error("Regressing source fill %s; suspending source event", key)
                    continue
                delta = cumulative - previous
                if delta <= 0:
                    continue
                delta_notional = notional - exact(progress.cumulative_notional)
                if delta_notional <= 0:
                    continue
                updated = {**order, "executedQty": str(delta), "avgPrice": str(delta_notional / delta)}
                batches = sorted(group_orders_into_batches([updated]),
                    key=lambda b: (b.get("startTime", 0), b.get("endTime", 0), b.get("key", "")))
                for batch in batches:
                    event_id = key + ":" + str(cumulative)
                    batch.update(qty=str(delta), qtyExact=str(delta), notional=str(delta_notional), notionalExact=str(delta_notional), avgPrice=str(delta_notional / delta), _event_id=event_id)
                    sequence = int((order.get("orderUpdateTime") if previous > 0 else None) or batch.get("startTime") or 0)
                    prior_sequence = await db.scalar(select(BindingDelivery.source_sequence).where(
                        BindingDelivery.binding_id == binding.id, BindingDelivery.source_event_id.startswith(key + ":"))
                        .order_by(BindingDelivery.source_sequence.desc()).limit(1))
                    if prior_sequence is not None:
                        sequence = max(sequence, prior_sequence + 1)
                    db.add(BindingDelivery(binding_id=binding.id, source_event_id=event_id,
                        source_sequence=sequence, payload_json=json.dumps(batch), state="PENDING"))
                progress.cumulative_qty, progress.cumulative_notional = str(cumulative), str(notional)
            # Historical closures cannot authorize closing a subsequently reopened cycle.
            if not baseline:
                for closed in raw_closed:
                    cid = str(closed.get("id") or closed.get("positionId") or "")
                    if cid and cid not in seen_closed:
                        event_id = "closed:" + cid
                        if await db.get(BindingDelivery, (binding.id, event_id)) is None:
                            db.add(BindingDelivery(binding_id=binding.id, source_event_id=event_id,
                                source_sequence=int(closed.get("closed") or closed.get("updateTime") or 0),
                                payload_json=json.dumps({"_closed": closed, "_event_id": event_id}), state="PENDING"))
            binding.baseline_seeded = True
        for order in expanded_orders:
            key = order_dedup_key(order)
            if key not in old_seen:
                seen_keys.append(key)
                old_seen.add(key)
        for closed in raw_closed:
            cid = str(closed.get("id") or closed.get("positionId") or "")
            if cid and cid not in seen_closed:
                seen_closed.append(cid)
        lead.seen_order_keys_json = json.dumps(seen_keys[-6000:])
        lead.seen_closed_ids_json = json.dumps(seen_closed[-1000:])
        await db.commit()
        notify_cfg = await self._get_config(db, "notifications", DEFAULT_SYSTEM_CONFIG["notifications"])
        resilience_cfg = await self._get_config(db, "resilience", DEFAULT_SYSTEM_CONFIG["resilience"])
        lead_margin_balance = (snapshot.get("profile") or {}).get("marginBalance") or lead.margin_balance or 0
        active = {f"{p['symbol']}_{p['positionSide']}": exact(p.get("qtyExact", p.get("qty")) or 0)
                  for p in snapshot.get("openPositions") or []}
        for binding in bindings:
            await db.refresh(binding)
            await db.refresh(lead)
            account = await db.get(TradingAccount, binding.account_id, populate_existing=True)
            if account is None:
                continue
            try:
                adapter = create_exchange_adapter(exchange=account.exchange,
                    credentials=self.vault.decrypt_dict(account.encrypted_credentials),
                    environment=account.environment,
                    proxy_url=ProxyManager.select_proxy_url(proxies, account.proxy_id, exchange=account.exchange),
                    margin_mode=account.margin_mode, position_mode=account.position_mode)
                adapter._time_offset_ms = int(account.clock_offset_ms or 0)
                await db.commit()
                account.clock_offset_ms = await adapter.sync_time()
                instruments = await adapter.get_instruments()
                if not await self.reconcile_pending_orders(db, account, adapter, instruments):
                    continue
                deliveries = (await db.scalars(select(BindingDelivery).where(
                    BindingDelivery.binding_id == binding.id, BindingDelivery.state != "APPLIED")
                    .order_by(BindingDelivery.source_sequence, BindingDelivery.source_event_id))).all()
                # Recover inventory immediately after each pending source event from
                # the current snapshot and the chronological suffix of known events.
                remaining_by_event = {}
                inventory = dict(active)
                for pending in reversed(deliveries):
                    payload = json.loads(pending.payload_json)
                    if "_closed" in payload:
                        continue
                    leg = f"{payload['symbol']}_{payload['positionSide']}"
                    quantity = exact(payload.get("qtyExact", payload.get("qty")) or 0)
                    remaining_by_event[pending.source_event_id] = inventory.get(leg, Decimal("0"))
                    if is_open_leg(payload.get("side"), payload.get("positionSide")):
                        inventory[leg] = max(Decimal("0"), inventory.get(leg, Decimal("0")) - quantity)
                    else:
                        inventory[leg] = inventory.get(leg, Decimal("0")) + quantity
                for delivery in deliveries:
                    if not is_authoritative_snapshot(snapshot):
                        break
                    await db.commit()
                    bal = await adapter.get_usdt_balance()
                    positions = await adapter.get_positions()
                    account.hwm_equity = max(account.hwm_equity, bal.eq)
                    self._refresh_day_start(account, bal.eq)
                    account.usdt_equity, account.avail_equity = bal.eq, bal.avail_eq
                    account.total_notional = sum(p.notional_usd for p in positions)
                    account.positions_json = json.dumps([p.to_dict() for p in positions])
                    account.status, account.last_error = "connected", None
                    batch = json.loads(delivery.payload_json)
                    batch["_observed_at_ms"] = snapshot["openPositionsMeta"]["asOf"]
                    batch["_lead_remaining_qty"] = remaining_by_event.get(delivery.source_event_id, 0)
                    # Historical openings inside a known completed lead cycle must not
                    # attach to the same symbol's currently reopened position.
                    if "_closed" not in batch and is_open_leg(batch.get("side"), batch.get("positionSide")):
                        timestamp = int(batch.get("startTime") or 0)
                        for closed in raw_closed:
                            if (closed.get("symbol") == batch.get("symbol") and
                                str(closed.get("side") or "").upper() == batch.get("positionSide") and
                                int(closed.get("opened") or 0) <= timestamp <= int(closed.get("closed") or 0)):
                                batch["_closed_cycle"] = True
                    db.info["delivery"] = delivery
                    if "_closed" in batch:
                        c = batch["_closed"]
                        if active.get(f"{c.get('symbol')}_{str(c.get('side', '')).upper()}", 0) <= 0:
                            await self._reconcile_closed_position(db, binding, lead, account, adapter, c, positions, instruments, notify_cfg)
                        delivery.state = "APPLIED"
                    else:
                        await self._execute_batch_for_binding(db, binding, lead, account, adapter,
                            batch, lead_margin_balance, active, bal.eq, positions, instruments,
                            bool(await self._get_config(db, "global_kill_switch", False)), notify_cfg, resilience_cfg)
                    db.info.pop("delivery", None)
                    await db.commit()
                    if not await self.reconcile_pending_orders(db, account, adapter, instruments):
                        break
                    if delivery.state != "APPLIED":
                        break
            except Exception as exc:
                db.info.pop("delivery", None)
                logger.exception("Follower delivery deferred")
                if isinstance(exc, SQLAlchemyError) or not db.is_active:
                    await db.rollback()
                    await db.refresh(account)
                    await db.refresh(lead)
                    for stale_binding in bindings:
                        await db.refresh(stale_binding)
                account.status, account.last_error = "error", redact_proxy_userinfo(str(exc))[:400]
                await db.commit()

    async def _execute_batch_for_binding(
        self,
        db: AsyncSession,
        binding: CopyBinding,
        lead: LeadSource,
        account: TradingAccount,
        adapter: BaseExchangeAdapter,
        batch: Dict[str, Any],
        lead_margin_balance: float,
        active_lead_positions: Dict[str, float],
        my_equity: float,
        positions: List[ExchangePosition],
        instruments: Dict[str, Any],
        global_kill: bool,
        notify_cfg: Dict[str, Any],
        resilience_cfg: Optional[Dict[str, Any]] = None,
    ) -> None:
        resilience_cfg = resilience_cfg or DEFAULT_SYSTEM_CONFIG["resilience"]
        symbol = batch["symbol"]
        side = batch["side"]
        ps = batch["positionSide"]
        lead_qty = exact(batch.get("qtyExact", batch["qty"]) or 0)
        lead_avg_px = float(batch.get("avgPrice") or 0.0)
        # Zero denotes unavailable pricing. Keep invalid prices recordable (NaN
        # becomes SQL NULL) while letting the catch-up guard reject the entry.
        if not math.isfinite(lead_avg_px):
            lead_avg_px = 0.0
        lead_notional = (
            batch.get("notionalExact", batch.get("notional")) or (exact(batch["qty"]) * exact(batch.get("avgPrice") or 0))
        ) if lead_avg_px > 0 else 0
        is_open = is_open_leg(side, ps)
        action = direction_label(side, ps, not is_open)

        whitelist = json.loads(binding.symbol_whitelist_json or "[]")
        blacklist = json.loads(binding.symbol_blacklist_json or "[]")
        symbol_map = json.loads(binding.symbol_map_json or "{}")

        inst_id, is_supported = adapter.resolve_symbol(symbol, instruments, symbol_map)
        if not is_supported:
            await self._record_execution(
                db, binding, lead, account, symbol, inst_id, side, ps, action,
                "SKIPPED_UNSUPPORTED", lead_qty, lead_avg_px, lead_notional,
                details=f"Instrument {inst_id} not available on {account.exchange} ({account.environment})",
                notify_cfg=notify_cfg,
            )
            return

        filt = RiskEngine.check_symbol_filter(symbol, whitelist, blacklist)
        if is_open and not filt.allowed:
            await self._record_execution(
                db, binding, lead, account, symbol, inst_id, side, ps, action,
                "BLOCKED_FILTER", lead_qty, lead_avg_px, lead_notional,
                details=filt.reason or "Filtered", notify_cfg=notify_cfg,
            )
            return

        if is_open and (batch.get("_closed_cycle") or active_lead_positions.get(f"{symbol}_{ps}", 0.0) <= 0):
            await self._record_execution(
                db, binding, lead, account, symbol, inst_id, side, ps, action,
                "SKIPPED_CLOSED_CYCLE", lead_qty, lead_avg_px, lead_notional,
                details="Lead position is already closed in current snapshot; skipping historical open",
                notify_cfg=notify_cfg)
            return
        spec = instruments[inst_id]
        local_pos_side = "long" if ps == "LONG" else "short"
        local_pos = next(
            (p for p in positions if p.inst_id == inst_id and p.pos_side == local_pos_side),
            None,
        )
        local_pos_sz = local_pos.pos if local_pos else 0.0
        debt = await self._get_debt(db, binding.id, inst_id, ps)

        allocation = await self._get_allocation(db, binding.id, account.id, inst_id, local_pos_side)
        event_id = batch.get("_event_id") or hashlib.sha256(json.dumps(batch, sort_keys=True).encode()).hexdigest()

        # Exits are limited by both binding ownership and physically reducible quantity.
        if not is_open:
            source_time = int(batch.get("startTime") or 0)
            if source_time and source_time < allocation.opened_at_ms:
                await self._record_execution(db, binding, lead, account, symbol, inst_id, side, ps, action,
                    "SKIPPED_CLOSED_CYCLE", lead_qty, lead_avg_px, lead_notional,
                    details="Historical reduction predates this allocation's opening cycle", notify_cfg=notify_cfg)
                return
            local_pos_sz = min(self._owned_qty(allocation), exact(local_pos_sz),
                               exact(local_pos.avail_pos) if local_pos else Decimal("0"))
            if local_pos_sz <= 0:
                await self._record_execution(db, binding, lead, account, symbol, inst_id, side, ps, action,
                    "SKIPPED_NO_LOCAL_POS", lead_qty, lead_avg_px, lead_notional,
                    details="This binding owns no reducible local position", notify_cfg=notify_cfg)
                return
            lead_rem_qty = batch.get("_lead_remaining_qty", active_lead_positions.get(f"{symbol}_{ps}", 0.0))
            is_full_close = exact(lead_rem_qty) <= 0
            close_sz = PositionSizer.size_close_order(local_pos_sz, is_full_close, lead_qty, lead_rem_qty, spec)
            if close_sz <= Decimal("0"):
                await self._record_execution(db, binding, lead, account, symbol, inst_id, side, ps, action,
                    "SKIPPED_BELOW_MIN", lead_qty, lead_avg_px, lead_notional,
                    details="Partial close below min_sz; no quantity rounded up", notify_cfg=notify_cfg)
                return
            try:
                await self._close_owned(db, binding, lead, account, adapter, local_pos, spec,
                    close_sz, event_id, "FULL_CLOSE" if is_full_close else "PARTIAL_REDUCE", notify_cfg,
                    lead_qty=lead_qty, lead_price=lead_avg_px, lead_notional=lead_notional)
            except Exception as exc:
                await self._handle_order_failure(db, binding, lead, account, symbol, inst_id, side, ps, action,
                    lead_qty, lead_avg_px, lead_notional, f"Close order error: {exc}", notify_cfg, resilience_cfg)
            return

        # CASE B: OPEN / ADD
        if global_kill or not binding.is_enabled or not account.is_enabled:
            await self._record_execution(
                db, binding, lead, account, symbol, inst_id, side, ps, action,
                "BLOCKED_KILL_SWITCH", lead_qty, lead_avg_px, lead_notional,
                details="Global kill switch is active", notify_cfg=notify_cfg,
            )
            return

        if not await self.reconcile_pending_orders(db, account, adapter, instruments):
            await self._record_execution(db, binding, lead, account, symbol, inst_id, side, ps, action,
                "UNKNOWN", lead_qty, lead_avg_px, lead_notional,
                details="Account has unresolved orders; increases suspended", notify_cfg=notify_cfg)
            return

        allocations = (await db.scalars(select(PositionAllocation).where(PositionAllocation.account_id == account.id))).all()
        physical = {(p.inst_id, p.pos_side): exact(p.pos) for p in positions}
        claimed = {}
        for owned in allocations:
            key = (owned.inst_id, owned.pos_side)
            claimed[key] = claimed.get(key, Decimal("0")) + self._owned_qty(owned)
        if any(qty > physical.get(key, Decimal("0")) for key, qty in claimed.items()):
            await self._record_execution(db, binding, lead, account, symbol, inst_id, side, ps, action,
                "BLOCKED_ALLOCATION_MISMATCH", lead_qty, lead_avg_px, lead_notional,
                details="Exchange inventory is below allocated quantity; reconciliation required", notify_cfg=notify_cfg)
            return

        dd_res = RiskEngine.check_drawdown(account.hwm_equity, my_equity, binding.max_drawdown_pct)
        if binding.circuit_breaker_tripped or not dd_res.allowed:
            binding.circuit_breaker_tripped = True
            binding.circuit_breaker_reason = dd_res.reason or binding.circuit_breaker_reason or "Drawdown breaker"
            await self._record_execution(
                db, binding, lead, account, symbol, inst_id, side, ps, action,
                "BLOCKED_DRAWDOWN", lead_qty, lead_avg_px, lead_notional,
                details=binding.circuit_breaker_reason, notify_cfg=notify_cfg,
            )
            return

        daily_res = RiskEngine.check_daily_loss(
            getattr(account, "day_start_equity", 0.0),
            my_equity,
            getattr(binding, "daily_max_loss_pct", 10.0),
        )
        if not daily_res.allowed:
            binding.circuit_breaker_tripped = True
            binding.circuit_breaker_reason = daily_res.reason or "Daily loss breaker"
            await self._record_execution(
                db, binding, lead, account, symbol, inst_id, side, ps, action,
                "BLOCKED_DAILY_LOSS", lead_qty, lead_avg_px, lead_notional,
                details=binding.circuit_breaker_reason, notify_cfg=notify_cfg,
            )
            return

        await db.commit()
        book = await adapter.get_orderbook(inst_id, depth=5)
        order_side = "buy" if ps == "LONG" else "sell"
        best_bid = book.bids[0][0] if book.bids else 0.0
        best_ask = book.asks[0][0] if book.asks else 0.0
        best_bid = best_bid if math.isfinite(best_bid) else 0.0
        best_ask = best_ask if math.isfinite(best_ask) else 0.0
        # Use only the executable side; substituting the lead price would fabricate
        # zero drift on an unavailable book. Zero is rejected and safely recorded.
        ref_px = best_ask if order_side == "buy" else best_bid

        # Every opening must pass, including batches with missing timestamps.
        batch_ts_ms = int(batch.get("endTime") or batch.get("time") or 0)
        catchup_res = RiskEngine.check_missed_order_catchup(
            order_time_ms=batch_ts_ms,
            now_ms=int(time.time() * 1000),
            max_age_sec=int(resilience_cfg.get("missed_order_max_age_sec", 900)),
            ref_price=ref_px,
            lead_avg_price=lead_avg_px,
            catchup_max_price_dev_pct=float(resilience_cfg.get("catchup_max_price_dev_pct", 0.5)),
        )
        if not catchup_res.allowed:
            await self._record_execution(
                db, binding, lead, account, symbol, inst_id, side, ps, action,
                catchup_res.code, lead_qty, lead_avg_px, lead_notional,
                my_price=ref_px, slippage_pct=catchup_res.value,
                details=catchup_res.reason or "Missed order rejected", notify_cfg=notify_cfg,
            )
            return

        # Orderbook spread guard
        spread_res = RiskEngine.check_spread(best_bid, best_ask, getattr(binding, "max_spread_pct", 0.3))
        if not spread_res.allowed:
            await self._record_execution(
                db, binding, lead, account, symbol, inst_id, side, ps, action,
                "BLOCKED_SPREAD", lead_qty, lead_avg_px, lead_notional,
                my_price=ref_px, slippage_pct=spread_res.value,
                details=spread_res.reason or "Spread guard triggered", notify_cfg=notify_cfg,
            )
            return

        slip_res = RiskEngine.check_slippage(ref_px, lead_avg_px, binding.max_slippage_pct)
        if not slip_res.allowed:
            await self._record_execution(
                db, binding, lead, account, symbol, inst_id, side, ps, action,
                "BLOCKED_SLIPPAGE", lead_qty, lead_avg_px, lead_notional,
                my_price=ref_px, slippage_pct=slip_res.value,
                details=slip_res.reason or "Slippage limit exceeded", notify_cfg=notify_cfg,
            )
            return

        target_usd = PositionSizer.calculate_target_usd(
            follower_equity=my_equity,
            lead_margin_balance=lead_margin_balance,
            risk_scale=binding.risk_scale,
            lead_notional=lead_notional,
            pending_debt_usd=self._pending_debt(debt),
        )

        sized = PositionSizer.size_open_order(target_usd, ref_px, spec)
        if sized.below_min:
            self._set_debt(debt, target_usd)
            await self._record_execution(
                db, binding, lead, account, symbol, inst_id, side, ps, action,
                "ACCUMULATED_DEBT", lead_qty, lead_avg_px, lead_notional,
                my_price=ref_px, slippage_pct=slip_res.value,
                details=f"Target ${target_usd:.2f} < min ${sized.min_usd:.2f}; carried forward",
                notify_cfg=notify_cfg,
            )
            return

        capped_qty, cap_reason = RiskEngine.cap_by_leverage(
            requested_qty=sized.contracts,
            ref_price=ref_px,
            spec=spec,
            follower_equity=my_equity,
            positions=positions,
            inst_id=inst_id,
            max_single_leverage=binding.max_single_leverage,
            max_total_leverage=binding.max_total_leverage,
        )
        if capped_qty <= 0:
            await self._record_execution(
                db, binding, lead, account, symbol, inst_id, side, ps, action,
                "BLOCKED_LEVERAGE", lead_qty, lead_avg_px, lead_notional,
                my_price=ref_px, slippage_pct=slip_res.value,
                details=cap_reason or "Leverage cap reached", notify_cfg=notify_cfg,
            )
            return

        projected_notional = sum(exact(p.notional_usd) for p in positions if p.inst_id == inst_id) + exact(sized.estimated_notional)
        tier_max = await adapter.get_max_leverage(inst_id, projected_notional)
        max_allowed_lvr = int(min(binding.max_instrument_leverage, spec.max_lvr, tier_max))
        target_lvr = PositionSizer.compute_dynamic_leverage(
            target_notional=sized.estimated_notional or (exact(sized.contracts) * exact(ref_px) * spec.ct_val),
            equity=my_equity, max_symbol_leverage=max_allowed_lvr,
        )
        margin_policy = str(resilience_cfg.get("insufficient_margin_policy", "auto_scale_down"))
        avail_eq = account.avail_equity
        capped_qty, margin_err, margin_note = RiskEngine.adjust_for_available_margin(
            requested_qty=capped_qty,
            ref_price=ref_px,
            spec=spec,
            avail_equity=avail_eq,
            leverage=target_lvr,
            policy=margin_policy,
        )
        if margin_err or capped_qty <= 0:
            await self._record_execution(
                db, binding, lead, account, symbol, inst_id, side, ps, action,
                "BLOCKED_INSUFFICIENT_MARGIN", lead_qty, lead_avg_px, lead_notional,
                my_price=ref_px, slippage_pct=slip_res.value,
                details=margin_err or "Insufficient available margin", notify_cfg=notify_cfg,
            )
            return

        await db.commit()  # Never hold a write transaction across leverage/book/order I/O.
        confirmed = await adapter.ensure_leverage(inst_id, target_lvr, pos_side=local_pos_side, mgn_mode=account.margin_mode)
        if confirmed is not True:
            raise RuntimeError("Exchange leverage could not be confirmed")

        # A price-limited IOC makes the configured slippage limit an execution bound.
        # Market-only and IOC-then-market settings cannot bypass that bound.
        offset = exact(binding.ioc_offset_pct) / Decimal("100")
        slippage = exact(binding.max_slippage_pct) / Decimal("100")
        if order_side == "buy":
            limit = min(exact(ref_px) * (1 + offset), exact(lead_avg_px) * (1 + slippage))
            ioc_px_dec = quantize_down(limit, spec.tick_sz)
        else:
            limit = max(exact(ref_px) * (1 - offset), exact(lead_avg_px) * (1 - slippage))
            ioc_px_dec = (limit / spec.tick_sz).to_integral_value(rounding=ROUND_UP) * spec.tick_sz
        spec.validate_order("ioc", capped_qty, ioc_px_dec)
        try:
            res = await self._submit_confirmed(db, binding, account, adapter, spec,
                source_event_id=event_id, target_usd=target_usd,
                source_time_ms=int(batch.get("startTime") or 0), observed_at_ms=batch.get("_observed_at_ms", 0), inst_id=inst_id,
                side=order_side, pos_side=local_pos_side, ord_type="ioc",
                sz=format_decimal(capped_qty, spec.lot_sz), px=format_decimal(ioc_px_dec, spec.tick_sz),
                reduce_only=False, td_mode=account.margin_mode)
            fill_sz, fill_px = res.fill_sz, res.avg_px
            if fill_sz > 0:
                actual_usd = exact(fill_sz) * spec.ct_val * exact(fill_px)
                binding.consecutive_failures = 0
                owned_entry = float(self._entry_cost(allocation) / self._owned_qty(allocation))
                tp_sl_note = await self._apply_tp_sl_after_fill(db, binding, account, adapter,
                    inst_id, local_pos_side, owned_entry, allocation.owned_qty, spec)
                notes = [n for n in (cap_reason, margin_note, f"Filled {fill_sz} @ {fill_px}", tp_sl_note) if n]
                await self._record_execution(db, binding, lead, account, symbol, inst_id, side, ps, action,
                    "EXECUTED", lead_qty, lead_avg_px, lead_notional,
                    my_qty=fill_sz, my_price=fill_px, my_notional=float(actual_usd),
                    order_id=res.ord_id, details="; ".join(notes), notify_cfg=notify_cfg)
            else:
                # Zero fill never consumes debt or removes protection.
                await self._record_execution(db, binding, lead, account, symbol, inst_id, side, ps, action,
                    "UNFILLED", lead_qty, lead_avg_px, lead_notional, order_id=res.ord_id,
                    details="Terminal order has no newly confirmed fills; debt retained", notify_cfg=notify_cfg)
        except Exception as exc:
            await self._handle_order_failure(db, binding, lead, account, symbol, inst_id, side, ps, action,
                lead_qty, lead_avg_px, lead_notional, f"Order execution failed: {exc}", notify_cfg, resilience_cfg)

    async def _close_owned(self, db, binding, lead, account, adapter, pos, spec, quantity,
                           source_event_id, action, notify_cfg, lead_qty=0.0, lead_price=0.0, lead_notional=0.0):
        allocation = await self._get_allocation(db, binding.id, account.id, pos.inst_id, pos.pos_side)
        qty = quantize_down(min(exact(quantity), self._owned_qty(allocation), exact(pos.pos), exact(pos.avail_pos)), spec.lot_sz)
        if qty <= 0:
            return False
        order_side = "sell" if pos.pos_side == "long" else "buy"
        res = await self._submit_confirmed(db, binding, account, adapter, spec,
            source_event_id=source_event_id, inst_id=pos.inst_id, side=order_side,
            pos_side=pos.pos_side, ord_type="market", sz=format_decimal(qty, spec.lot_sz),
            px=None, reduce_only=True, td_mode=account.margin_mode)
        fill_sz, fill_px = res.fill_sz, res.avg_px
        await self._record_execution(db, binding, lead, account, pos.symbol, pos.inst_id,
            order_side.upper(), pos.pos_side.upper(), action, "EXECUTED" if fill_sz > 0 else "UNFILLED",
            lead_qty, lead_price, lead_notional, my_qty=fill_sz, my_price=fill_px,
            my_notional=float(exact(fill_sz) * spec.ct_val * exact(fill_px)), order_id=res.ord_id,
            details=f"Confirmed close fill {fill_sz}; binding remaining {allocation.owned_qty}", notify_cfg=notify_cfg)
        await db.commit()
        return fill_sz > 0

    async def _apply_tp_sl_after_fill(
        self,
        db: AsyncSession,
        binding: CopyBinding,
        account: TradingAccount,
        adapter: BaseExchangeAdapter,
        inst_id: str,
        local_pos_side: str,
        fill_px: float,
        total_pos_sz: float,
        spec: Any,
    ) -> Optional[str]:
        mode = getattr(binding, "tp_sl_mode", "follow_lead") or "follow_lead"
        await self._upsert_guard(db, binding.id, inst_id, local_pos_side, fill_px)
        if mode == "follow_lead":
            return None

        # Native position-wide stops cannot express binding ownership on shared accounts.
        # Keep the durable software guard; never cancel another strategy's conditional orders.
        return f"TP/SL({mode}) binding allocation software guard active"

    async def _handle_order_failure(
        self,
        db: AsyncSession,
        binding: CopyBinding,
        lead: LeadSource,
        account: TradingAccount,
        symbol: str,
        inst_id: str,
        side: str,
        ps: str,
        action: str,
        lead_qty: float,
        lead_avg_px: float,
        lead_notional: float,
        err_msg: str,
        notify_cfg: Dict[str, Any],
        resilience_cfg: Dict[str, Any],
    ) -> None:
        binding.consecutive_failures = (binding.consecutive_failures or 0) + 1
        max_fail = int(resilience_cfg.get("max_consecutive_failures", 5))
        if binding.consecutive_failures >= max_fail:
            binding.circuit_breaker_tripped = True
            binding.circuit_breaker_reason = f"Consecutive order failures ({binding.consecutive_failures}/{max_fail})"
        await self._record_execution(
            db, binding, lead, account, symbol, inst_id, side, ps, action,
            "FAILED", lead_qty, lead_avg_px, lead_notional,
            details=err_msg, notify_cfg=notify_cfg,
        )

    async def _reconcile_closed_position(
        self,
        db: AsyncSession,
        binding: CopyBinding,
        lead: LeadSource,
        account: TradingAccount,
        adapter: BaseExchangeAdapter,
        closed_pos: Dict[str, Any],
        positions: List[ExchangePosition],
        instruments: Dict[str, Any],
        notify_cfg: Dict[str, Any],
    ) -> None:
        sym = str(closed_pos.get("symbol") or "")
        raw_side = str(closed_pos.get("side") or "").upper()
        if not sym or raw_side not in ("LONG", "SHORT"):
            return
        inst_id, supported = adapter.resolve_symbol(sym, instruments, json.loads(binding.symbol_map_json or "{}"))
        if not supported:
            return
        local_side = raw_side.lower()
        allocation = await self._get_allocation(db, binding.id, account.id, inst_id, local_side)
        if allocation.owned_qty <= 0:
            return
        closed_at = int(closed_pos.get("closed") or closed_pos.get("updateTime") or 0)
        if closed_at <= allocation.opened_at_ms:
            return
        pos = next((p for p in positions if p.inst_id == inst_id and p.pos_side == local_side), None)
        if pos is None or pos.pos <= 0:
            return
        event_id = "closed:" + str(closed_pos.get("id") or closed_pos.get("positionId") or closed_at)
        await self._close_owned(db, binding, lead, account, adapter, pos, instruments[inst_id],
            min(allocation.owned_qty, pos.pos), event_id, "RECONCILE_CLOSE", notify_cfg)

    @serialized_execution
    async def reconcile_orphan_positions(
        self,
        db: AsyncSession,
        lead: LeadSource,
        account: TradingAccount,
        binding: CopyBinding,
        adapter: BaseExchangeAdapter,
        lead_open_positions: List[Dict[str, Any]],
        local_positions: List[ExchangePosition],
        instruments: Dict[str, Any],
        resilience_cfg: Optional[Dict[str, Any]] = None,
        notify_cfg: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """Detect follower positions whose lead leg no longer exists (e.g. closed during outage)."""
        await db.refresh(binding)
        if not await self.reconcile_pending_orders(db, account, adapter, instruments):
            return []
        await db.commit()
        local_positions = await adapter.get_positions()
        res_cfg: Dict[str, Any] = resilience_cfg or await self._get_config(db, "resilience", DEFAULT_SYSTEM_CONFIG["resilience"]) or {}
        ntf_cfg: Dict[str, Any] = notify_cfg or await self._get_config(db, "notifications", DEFAULT_SYSTEM_CONFIG["notifications"]) or {}
        mode = str(res_cfg.get("auto_close_orphan_positions", "alert_only"))

        symbol_map = json.loads(binding.symbol_map_json or "{}")
        lead_keys: set[tuple[str, str]] = set()
        for lp in lead_open_positions:
            sym = str(lp.get("symbol") or "")
            ps = str(lp.get("positionSide") or "").upper()
            if not sym or ps not in ("LONG", "SHORT") or float(lp.get("qty") or 0.0) <= 0:
                continue
            inst_id, ok = adapter.resolve_symbol(sym, instruments, symbol_map)
            if ok:
                lead_keys.add((inst_id, "long" if ps == "LONG" else "short"))

        actions_taken: List[str] = []
        now = time.time()
        for pos in local_positions:
            if pos.pos <= 0 or pos.pos_side not in ("long", "short"):
                continue
            if (pos.inst_id, pos.pos_side) in lead_keys:
                continue
            allocation = await self._get_allocation(db, binding.id, account.id, pos.inst_id, pos.pos_side)
            if allocation.owned_qty <= 0:
                continue

            raw_ps = "LONG" if pos.pos_side == "long" else "SHORT"
            if mode == "auto_close":
                spec = instruments.get(pos.inst_id)
                if not spec:
                    continue
                try:
                    event_id = await self._exit_event_id(db, binding, allocation, "orphan")
                    if await self._close_owned(db, binding, lead, account, adapter, pos, spec,
                        min(allocation.owned_qty, pos.pos), event_id, "ORPHAN_AUTO_CLOSE", ntf_cfg):
                        actions_taken.append(f"closed:{pos.inst_id}:{pos.pos_side}")
                except Exception:
                    logger.exception("Orphan auto-close failed for %s", pos.inst_id)
            else:
                alert_key = f"{binding.id}:{pos.inst_id}:{pos.pos_side}"
                if now - self._orphan_alert_ts.get(alert_key, 0.0) >= 900.0:
                    self._orphan_alert_ts[alert_key] = now
                    await self._record_execution(
                        db, binding, lead, account, pos.symbol, pos.inst_id, "ALERT", raw_ps,
                        "ORPHAN_ALERT", "BLOCKED_ORPHAN_DETECTED", 0.0, 0.0, 0.0,
                        my_qty=allocation.owned_qty, my_price=pos.avg_px,
                        my_notional=round(pos.notional_usd, 2),
                        details="Ghost position detected: present locally but absent from lead openPositions",
                        notify_cfg=ntf_cfg,
                    )
                    actions_taken.append(f"alerted:{pos.inst_id}:{pos.pos_side}")
        return actions_taken

    async def _exit_event_id(self, db, binding, allocation, action):
        prefix = f"{action}:{allocation.id}:{allocation.generation}:"
        ids = (await db.scalars(select(OrderIntent.id).where(OrderIntent.binding_id == binding.id,
                    OrderIntent.source_event_id.startswith(prefix)))).all()
        return prefix + str(len(ids))

    @serialized_execution
    async def check_position_guards(
        self,
        db: AsyncSession,
        lead: LeadSource,
        account: TradingAccount,
        binding: CopyBinding,
        adapter: BaseExchangeAdapter,
        local_positions: List[ExchangePosition],
        instruments: Dict[str, Any],
        notify_cfg: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """Software TP/SL & Trailing Stop fallback monitor for independent / hybrid_guard modes."""
        await db.refresh(binding)
        if not await self.reconcile_pending_orders(db, account, adapter, instruments):
            return []
        mode = getattr(binding, "tp_sl_mode", "follow_lead") or "follow_lead"
        if mode == "follow_lead":
            return []
        notify_cfg = notify_cfg or await self._get_config(db, "notifications", DEFAULT_SYSTEM_CONFIG["notifications"])
        triggered: List[str] = []
        await db.commit()
        local_positions = await adapter.get_positions()

        for pos in local_positions:
            if pos.pos <= 0 or pos.pos_side not in ("long", "short"):
                continue
            spec = instruments.get(pos.inst_id)
            if not spec:
                continue

            allocation = await self._get_allocation(db, binding.id, account.id, pos.inst_id, pos.pos_side)
            if allocation.owned_qty <= 0:
                continue
            await db.commit()
            try:
                book = await adapter.get_orderbook(pos.inst_id, depth=1)
                if not book.bids or not book.asks or not RiskEngine.check_spread(book.bids[0][0], book.asks[0][0], 100).allowed:
                    raise ValueError("Invalid protective quote")
                cur_px = book.bids[0][0] if pos.pos_side == "long" else book.asks[0][0]
            except Exception as exc:
                account.status = "error"
                account.last_error = redact_proxy_userinfo(f"Protective quote unavailable: {exc}")[:400]
                binding.circuit_breaker_tripped = True
                binding.circuit_breaker_reason = "Protective quote unavailable"
                await db.commit()
                continue
            entry = float(self._entry_cost(allocation) / self._owned_qty(allocation))
            guard = await self._upsert_guard(db, binding.id, pos.inst_id, pos.pos_side, entry)
            entry_px = guard.entry_px or pos.avg_px or cur_px
            if cur_px > (guard.peak_px or entry_px):
                guard.peak_px = cur_px
            if guard.trough_px <= 0 or cur_px < guard.trough_px:
                guard.trough_px = cur_px

            reason: Optional[str] = None
            is_long = pos.pos_side == "long"
            tp_px, sl_px = RiskEngine.compute_tp_sl_prices(
                pos_side=pos.pos_side,
                entry_px=entry_px,
                tp_sl_mode=mode,
                enable_tp=bool(getattr(binding, "enable_take_profit", False)),
                tp_pct=float(getattr(binding, "take_profit_pct", 10.0)),
                enable_sl=bool(getattr(binding, "enable_stop_loss", False)),
                sl_pct=float(getattr(binding, "stop_loss_pct", 5.0)),
                hard_sl_pct=float(getattr(binding, "hard_stop_loss_pct", 15.0)),
            )

            if sl_px and ((is_long and cur_px <= sl_px) or (not is_long and cur_px >= sl_px)):
                reason = f"Stop-Loss triggered @ {cur_px:.4f} (threshold {sl_px:.4f})"
            elif tp_px and ((is_long and cur_px >= tp_px) or (not is_long and cur_px <= tp_px)):
                reason = f"Take-Profit triggered @ {cur_px:.4f} (threshold {tp_px:.4f})"
            elif mode == "independent" and bool(getattr(binding, "enable_trailing_stop", False)):
                cb_pct = float(getattr(binding, "trailing_callback_pct", 2.0)) / 100.0
                if is_long and guard.peak_px > entry_px and cur_px <= guard.peak_px * (1.0 - cb_pct):
                    reason = f"Trailing Stop triggered @ {cur_px:.4f} (peak {guard.peak_px:.4f}, cb {cb_pct*100:.1f}%)"
                elif not is_long and guard.trough_px < entry_px and cur_px >= guard.trough_px * (1.0 + cb_pct):
                    reason = f"Trailing Stop triggered @ {cur_px:.4f} (trough {guard.trough_px:.4f}, cb {cb_pct*100:.1f}%)"

            if reason:
                try:
                    event_id = await self._exit_event_id(db, binding, allocation, "guard")
                    if await self._close_owned(db, binding, lead, account, adapter, pos, spec,
                        min(allocation.owned_qty, pos.pos), event_id, "TPSL_CLOSE", notify_cfg):
                        triggered.append(f"{pos.inst_id}:{pos.pos_side}")
                except Exception:
                    logger.exception("Software TP/SL close failed for %s", pos.inst_id)
        return triggered
