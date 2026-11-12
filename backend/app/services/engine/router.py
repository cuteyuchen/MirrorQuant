from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Coroutine, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import DEFAULT_SYSTEM_CONFIG
from app.core.security import CredentialVault
from app.models.domain import (
    BindingDebt,
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
    ExchangeAPIError,
    ExchangePosition,
    OrderResult,
)
from app.services.adapters.factory import create_exchange_adapter
from app.services.engine.risk import RiskEngine
from app.services.engine.sizer import PositionSizer, format_decimal, quantize_step
from app.services.lead_collector.binance_bapi import (
    direction_label,
    expand_slim_order,
    group_orders_into_batches,
    is_open_leg,
    order_dedup_key,
)
from app.services.notifier.service import NotificationService
from app.services.proxy.manager import ProxyManager

logger = logging.getLogger("mirrorquant.router")

BroadcastFn = Optional[Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]]


class CopyRouter:
    """Routes lead order batches & closures across N:M CopyBindings with risk, TP/SL & resilience."""

    def __init__(self, vault: CredentialVault, broadcast_fn: BroadcastFn = None):
        self.vault = vault
        self.broadcast_fn = broadcast_fn
        self._orphan_alert_ts: Dict[str, float] = {}

    async def _emit(self, event: Dict[str, Any]) -> None:
        if self.broadcast_fn:
            try:
                await self.broadcast_fn(event)
            except Exception:
                pass

    @staticmethod
    async def _get_config(db: AsyncSession, key: str, default: Any) -> Any:
        row = await db.scalar(select(SystemConfig).where(SystemConfig.key == key))
        if row:
            try:
                val = json.loads(row.value_json)
                if isinstance(default, dict) and isinstance(val, dict):
                    return {**default, **val}
                return val
            except Exception:
                pass
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
        """Query exchange by clOrdId on transient/timeout failure before retrying."""
        try:
            return await adapter.place_order(
                inst_id=inst_id,
                side=side,
                pos_side=pos_side,
                ord_type=ord_type,
                sz=sz,
                px=px,
                cl_ord_id=cl_ord_id,
                reduce_only=reduce_only,
                td_mode=td_mode,
            )
        except ExchangeAPIError:
            raise
        except Exception as first_exc:
            # Query first so a slow/timed-out request doesn't double-open
            await asyncio.sleep(0.35)
            try:
                existing = await adapter.get_order(inst_id, cl_ord_id=cl_ord_id)
                if existing and existing.get("ordId"):
                    return OrderResult(
                        ord_id=str(existing["ordId"]),
                        cl_ord_id=cl_ord_id,
                        fill_sz=float(existing.get("accFillSz") or 0.0),
                        avg_px=float(existing.get("avgPx") or 0.0),
                        state=str(existing.get("state") or "submitted"),
                        raw=existing.get("raw") or existing,
                    )
            except Exception:
                pass
            # Safe single retry with same cl_ord_id
            logger.warning("Retrying idempotent order %s after transient error: %s", cl_ord_id, first_exc)
            return await adapter.place_order(
                inst_id=inst_id,
                side=side,
                pos_side=pos_side,
                ord_type=ord_type,
                sz=sz,
                px=px,
                cl_ord_id=cl_ord_id,
                reduce_only=reduce_only,
                td_mode=td_mode,
            )

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
            lead_qty=lead_qty,
            lead_price=lead_price,
            lead_notional=lead_notional,
            my_qty=my_qty,
            my_price=my_price,
            my_notional=my_notional,
            slippage_pct=slippage_pct,
            order_id=order_id,
            details=details,
        )
        db.add(rec)
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
        await self._emit(payload)

        if notify_cfg:
            should_notify = (
                (status == "EXECUTED" and notify_cfg.get("notify_on_execution", True))
                or (
                    (status.startswith("BLOCKED") or status == "FAILED" or status.startswith("SKIPPED_STALE"))
                    and notify_cfg.get("notify_on_risk_block", True)
                )
            )
            if should_notify:
                title = f"MirrorQuant [{status}] {symbol}"
                msg = (
                    f"Binding: {binding.name} ({lead.name} -> {account.name})\n"
                    f"Action: {action} ({inst_id})\n"
                    f"Qty: {my_qty} @ {my_price} (~${my_notional:,.2f})\n"
                    f"Detail: {details}"
                )
                try:
                    asyncio.create_task(NotificationService.dispatch(notify_cfg, title, msg, event_type=status))
                except RuntimeError:
                    pass
        return rec

    async def process_lead_events(
        self,
        db: AsyncSession,
        lead: LeadSource,
        raw_orders: List[Dict[str, Any]],
        raw_closed: List[Dict[str, Any]],
        snapshot: Dict[str, Any],
        proxies: List[ProxyNode],
    ) -> None:
        """Compare newly observed orders/closures against lead seen state and route to bindings."""
        seen_order_keys = set(json.loads(lead.seen_order_keys_json or "[]"))
        seen_closed_ids = set(json.loads(lead.seen_closed_ids_json or "[]"))

        expanded_orders = [expand_slim_order(o) for o in raw_orders]
        new_orders = [o for o in expanded_orders if order_dedup_key(o) not in seen_order_keys]
        new_closed = [c for c in raw_closed if str(c.get("id") or c.get("positionId") or "") not in seen_closed_ids]

        bindings = (
            await db.scalars(
                select(CopyBinding).where(
                    CopyBinding.lead_id == lead.id,
                    CopyBinding.is_enabled == True,  # noqa: E712
                )
            )
        ).all()

        # Seed cold-start baselines silently without placing historical trades
        unseeded = [b for b in bindings if not b.baseline_seeded]
        if unseeded or not seen_order_keys:
            for o in expanded_orders:
                seen_order_keys.add(order_dedup_key(o))
            for c in raw_closed:
                cid = str(c.get("id") or c.get("positionId") or "")
                if cid:
                    seen_closed_ids.add(cid)
            for b in unseeded:
                b.baseline_seeded = True
            lead.seen_order_keys_json = json.dumps(list(seen_order_keys)[-3000:])
            lead.seen_closed_ids_json = json.dumps(list(seen_closed_ids)[-500:])
            await db.commit()
            return

        if not new_orders and not new_closed:
            return

        global_kill = bool(await self._get_config(db, "global_kill_switch", False))
        notify_cfg = await self._get_config(db, "notifications", DEFAULT_SYSTEM_CONFIG["notifications"])
        resilience_cfg = await self._get_config(db, "resilience", DEFAULT_SYSTEM_CONFIG["resilience"])

        batches = group_orders_into_batches(new_orders) if new_orders else []
        if resilience_cfg.get("flip_close_first", True) and len(batches) > 1:
            # Close/reduce batches first to release margin before opposite opens
            batches = sorted(
                batches,
                key=lambda b: (1 if is_open_leg(b.get("side"), b.get("positionSide")) else 0, b.get("startTime") or 0),
            )

        lead_margin_balance = float((snapshot.get("profile") or {}).get("marginBalance") or lead.margin_balance or 0.0)

        active_lead_positions: Dict[str, float] = {}
        for pos in snapshot.get("openPositions") or []:
            active_lead_positions[f"{pos['symbol']}_{pos['positionSide']}"] = float(pos.get("qty") or 0.0)

        for binding in bindings:
            account = await db.get(TradingAccount, binding.account_id)
            if not account or not account.is_enabled:
                continue

            try:
                creds = self.vault.decrypt_dict(account.encrypted_credentials)
                proxy_url = (
                    ProxyManager.select_proxy_url(proxies, account.proxy_id)
                    if account.proxy_id is not None
                    else None
                )
                adapter = create_exchange_adapter(
                    exchange=account.exchange,
                    credentials=creds,
                    environment=account.environment,
                    proxy_url=proxy_url,
                    margin_mode=account.margin_mode,
                    position_mode=account.position_mode,
                )
                adapter._time_offset_ms = int(getattr(account, "clock_offset_ms", 0) or 0)
                bal = await adapter.get_usdt_balance()
                positions = await adapter.get_positions()
                instruments = await adapter.get_instruments()
            except Exception as exc:
                logger.error("Adapter init/sync failed for account %s: %s", account.name, exc)
                account.status = "error"
                account.last_error = str(exc)[:400]
                continue

            if bal.eq > account.hwm_equity:
                account.hwm_equity = bal.eq
            self._refresh_day_start(account, bal.eq)
            account.usdt_equity = bal.eq
            account.avail_equity = bal.avail_eq
            account.total_notional = sum(p.notional_usd for p in positions)
            account.unrealized_pnl = sum(p.upl for p in positions)
            account.positions_json = json.dumps([p.to_dict() for p in positions])
            account.status = "connected"
            account.last_error = None

            for batch in batches:
                await self._execute_batch_for_binding(
                    db=db,
                    binding=binding,
                    lead=lead,
                    account=account,
                    adapter=adapter,
                    batch=batch,
                    lead_margin_balance=lead_margin_balance,
                    active_lead_positions=active_lead_positions,
                    my_equity=bal.eq,
                    positions=positions,
                    instruments=instruments,
                    global_kill=global_kill,
                    notify_cfg=notify_cfg,
                    resilience_cfg=resilience_cfg,
                )
                # Refresh local positions & avail balance after each batch (critical for close-before-open flip!)
                try:
                    positions = await adapter.get_positions()
                    bal = await adapter.get_usdt_balance()
                    account.usdt_equity = bal.eq
                    account.avail_equity = bal.avail_eq
                    account.positions_json = json.dumps([p.to_dict() for p in positions])
                except Exception:
                    pass

            # Safety reconciliation for full closed positions from position-history
            for c in new_closed:
                await self._reconcile_closed_position(
                    db=db,
                    binding=binding,
                    lead=lead,
                    account=account,
                    adapter=adapter,
                    closed_pos=c,
                    positions=positions,
                    instruments=instruments,
                    notify_cfg=notify_cfg,
                )

        # Mark processed orders & closures as seen
        for o in new_orders:
            seen_order_keys.add(order_dedup_key(o))
        for c in new_closed:
            cid = str(c.get("id") or c.get("positionId") or "")
            if cid:
                seen_closed_ids.add(cid)

        lead.seen_order_keys_json = json.dumps(list(seen_order_keys)[-3000:])
        lead.seen_closed_ids_json = json.dumps(list(seen_closed_ids)[-500:])
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
        lead_qty = float(batch["qty"] or 0.0)
        lead_avg_px = float(batch.get("avgPrice") or 0.0)
        lead_notional = float(batch.get("notional") or (lead_qty * lead_avg_px))
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
        if not filt.allowed:
            await self._record_execution(
                db, binding, lead, account, symbol, inst_id, side, ps, action,
                "BLOCKED_FILTER", lead_qty, lead_avg_px, lead_notional,
                details=filt.reason or "Filtered", notify_cfg=notify_cfg,
            )
            return

        spec = instruments[inst_id]
        local_pos_side = "long" if ps == "LONG" else "short"
        local_pos = next(
            (p for p in positions if p.inst_id == inst_id and p.pos_side == local_pos_side),
            None,
        )
        local_pos_sz = local_pos.pos if local_pos else 0.0
        debt = await self._get_debt(db, binding.id, inst_id, ps)

        # CASE A: REDUCE / CLOSE (always permitted even under drawdown/daily breaker)
        if not is_open:
            if local_pos_sz <= 0:
                debt.pending_usd = 0.0
                await self._clear_guard(db, binding.id, inst_id, local_pos_side)
                await self._record_execution(
                    db, binding, lead, account, symbol, inst_id, side, ps, action,
                    "SKIPPED_NO_LOCAL_POS", lead_qty, lead_avg_px, lead_notional,
                    details="No local position to close; reset pending debt",
                    notify_cfg=notify_cfg,
                )
                return

            lead_rem_qty = active_lead_positions.get(f"{symbol}_{ps}", 0.0)
            is_full_close = lead_rem_qty <= 1e-6
            close_sz = PositionSizer.size_close_order(
                local_pos_sz=local_pos_sz,
                is_full_close=is_full_close,
                lead_close_qty=lead_qty,
                lead_remaining_qty=lead_rem_qty,
                spec=spec,
            )
            order_side = "sell" if ps == "LONG" else "buy"
            cl_ord_id = f"mqc{int(time.time()*1000)}{uuid.uuid4().hex[:6]}"[:32]
            try:
                res = await self._place_order_idempotent(
                    adapter=adapter,
                    inst_id=inst_id,
                    side=order_side,
                    pos_side=local_pos_side,
                    ord_type="market",
                    sz=format_decimal(close_sz, spec.lot_sz),
                    px=None,
                    cl_ord_id=cl_ord_id,
                    reduce_only=True,
                    td_mode=account.margin_mode,
                )
                await asyncio.sleep(0.2)
                ord_info = await adapter.get_order(inst_id, ord_id=res.ord_id, cl_ord_id=cl_ord_id)
                fill_sz = float((ord_info or {}).get("accFillSz") or res.fill_sz or close_sz)
                fill_px = float((ord_info or {}).get("avgPx") or res.avg_px or lead_avg_px)
                my_notional = fill_sz * float(spec.ct_val) * fill_px
                slip = ((fill_px - lead_avg_px) / lead_avg_px * 100.0) if lead_avg_px > 0 else 0.0
                if is_full_close:
                    debt.pending_usd = 0.0
                    await self._clear_guard(db, binding.id, inst_id, local_pos_side)
                    try:
                        await adapter.cancel_tp_sl(inst_id, local_pos_side)
                    except Exception:
                        pass
                binding.consecutive_failures = 0
                await self._record_execution(
                    db, binding, lead, account, symbol, inst_id, side, ps,
                    "FULL_CLOSE" if is_full_close else "PARTIAL_REDUCE",
                    "EXECUTED", lead_qty, lead_avg_px, lead_notional,
                    my_qty=fill_sz, my_price=fill_px, my_notional=round(my_notional, 2),
                    slippage_pct=round(slip, 3), order_id=res.ord_id,
                    details=f"Closed {fill_sz} @ {fill_px}", notify_cfg=notify_cfg,
                )
            except Exception as exc:
                await self._handle_order_failure(
                    db, binding, lead, account, symbol, inst_id, side, ps, action,
                    lead_qty, lead_avg_px, lead_notional, f"Close order error: {exc}",
                    notify_cfg, resilience_cfg,
                )
            return

        # CASE B: OPEN / ADD
        if global_kill:
            await self._record_execution(
                db, binding, lead, account, symbol, inst_id, side, ps, action,
                "BLOCKED_KILL_SWITCH", lead_qty, lead_avg_px, lead_notional,
                details="Global kill switch is active", notify_cfg=notify_cfg,
            )
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

        book = await adapter.get_orderbook(inst_id, depth=5)
        order_side = "buy" if ps == "LONG" else "sell"
        best_bid = book.bids[0][0] if book.bids else 0.0
        best_ask = book.asks[0][0] if book.asks else 0.0
        if order_side == "buy":
            ref_px = best_ask if best_ask > 0 else lead_avg_px
        else:
            ref_px = best_bid if best_bid > 0 else lead_avg_px

        # Disconnect / missed order catch-up check
        batch_ts_ms = int(batch.get("endTime") or batch.get("time") or 0)
        if batch_ts_ms > 0:
            catchup_res = RiskEngine.check_missed_order_catchup(
                order_time_ms=batch_ts_ms,
                now_ms=int(time.time() * 1000),
                max_age_sec=int(resilience_cfg.get("missed_order_max_age_sec", 120)),
                ref_price=ref_px,
                lead_avg_price=lead_avg_px,
                catchup_max_price_dev_pct=float(resilience_cfg.get("catchup_max_price_dev_pct", 0.8)),
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
            pending_debt_usd=debt.pending_usd,
        )

        sized = PositionSizer.size_open_order(target_usd, ref_px, spec)
        if sized.below_min:
            debt.pending_usd = target_usd
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

        target_lvr = int(min(binding.max_instrument_leverage, spec.max_lvr))
        margin_policy = str(resilience_cfg.get("insufficient_margin_policy", "auto_scale_down"))
        avail_eq = account.avail_equity if account.avail_equity > 0 else my_equity
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

        await adapter.ensure_leverage(inst_id, target_lvr, pos_side=local_pos_side, mgn_mode=account.margin_mode)

        cl_ord_id = f"mqo{int(time.time()*1000)}{uuid.uuid4().hex[:6]}"[:32]
        use_market_only = binding.order_mode == "market_only"
        ioc_offset = binding.ioc_offset_pct / 100.0
        ioc_px = ref_px * (1.0 + ioc_offset) if order_side == "buy" else ref_px * (1.0 - ioc_offset)
        ioc_px_dec = quantize_step(ioc_px, spec.tick_sz)

        try:
            res = await self._place_order_idempotent(
                adapter=adapter,
                inst_id=inst_id,
                side=order_side,
                pos_side=local_pos_side,
                ord_type="market" if use_market_only else "ioc",
                sz=format_decimal(capped_qty, spec.lot_sz),
                px=None if use_market_only else format_decimal(ioc_px_dec, spec.tick_sz),
                cl_ord_id=cl_ord_id,
                reduce_only=False,
                td_mode=account.margin_mode,
            )
            await asyncio.sleep(0.2)
            ord_info = await adapter.get_order(inst_id, ord_id=res.ord_id, cl_ord_id=cl_ord_id)
            fill_sz = float((ord_info or {}).get("accFillSz") or res.fill_sz or 0.0)
            fill_px = float((ord_info or {}).get("avgPx") or res.avg_px or ref_px)

            # Fallback to market if IOC didn't fill and policy allows
            if fill_sz <= 0 and binding.order_mode == "ioc_then_market":
                book2 = await adapter.get_orderbook(inst_id, depth=2)
                ref_px2 = (
                    (book2.asks[0][0] if book2.asks else ref_px)
                    if order_side == "buy"
                    else (book2.bids[0][0] if book2.bids else ref_px)
                )
                slip2 = RiskEngine.check_slippage(ref_px2, lead_avg_px, binding.max_slippage_pct)
                if slip2.allowed:
                    mkt_cid = f"mqm{int(time.time()*1000)}{uuid.uuid4().hex[:6]}"[:32]
                    res = await self._place_order_idempotent(
                        adapter=adapter,
                        inst_id=inst_id,
                        side=order_side,
                        pos_side=local_pos_side,
                        ord_type="market",
                        sz=format_decimal(capped_qty, spec.lot_sz),
                        px=None,
                        cl_ord_id=mkt_cid,
                        reduce_only=False,
                        td_mode=account.margin_mode,
                    )
                    await asyncio.sleep(0.2)
                    ord_info2 = await adapter.get_order(inst_id, ord_id=res.ord_id, cl_ord_id=mkt_cid)
                    fill_sz = float((ord_info2 or {}).get("accFillSz") or res.fill_sz or float(capped_qty))
                    fill_px = float((ord_info2 or {}).get("avgPx") or res.avg_px or ref_px2)

            if fill_sz > 0:
                actual_usd = fill_sz * float(spec.ct_val) * fill_px
                debt.pending_usd = max(0.0, target_usd - actual_usd)
                slip_actual = ((fill_px - lead_avg_px) / lead_avg_px * 100.0) if lead_avg_px > 0 else 0.0
                binding.consecutive_failures = 0

                # Update software guard & exchange conditional TP/SL per tp_sl_mode
                tp_sl_note = await self._apply_tp_sl_after_fill(
                    db=db,
                    binding=binding,
                    account=account,
                    adapter=adapter,
                    inst_id=inst_id,
                    local_pos_side=local_pos_side,
                    fill_px=fill_px,
                    total_pos_sz=local_pos_sz + fill_sz,
                    spec=spec,
                )

                notes = [n for n in (cap_reason, margin_note, f"Filled {fill_sz} @ {fill_px}; debt=${debt.pending_usd:.2f}", tp_sl_note) if n]
                await self._record_execution(
                    db, binding, lead, account, symbol, inst_id, side, ps, action,
                    "EXECUTED", lead_qty, lead_avg_px, lead_notional,
                    my_qty=fill_sz, my_price=fill_px, my_notional=round(actual_usd, 2),
                    slippage_pct=round(slip_actual, 3), order_id=res.ord_id,
                    details="; ".join(notes), notify_cfg=notify_cfg,
                )
            else:
                debt.pending_usd = target_usd
                await self._record_execution(
                    db, binding, lead, account, symbol, inst_id, side, ps, action,
                    "ACCUMULATED_DEBT", lead_qty, lead_avg_px, lead_notional,
                    my_price=ref_px, details="IOC expired unfilled; debt retained",
                    notify_cfg=notify_cfg,
                )
        except Exception as exc:
            await self._handle_order_failure(
                db, binding, lead, account, symbol, inst_id, side, ps, action,
                lead_qty, lead_avg_px, lead_notional, f"Order execution failed: {exc}",
                notify_cfg, resilience_cfg,
            )

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

        tp_px, sl_px = RiskEngine.compute_tp_sl_prices(
            pos_side=local_pos_side,
            entry_px=fill_px,
            tp_sl_mode=mode,
            enable_tp=bool(getattr(binding, "enable_take_profit", False)),
            tp_pct=float(getattr(binding, "take_profit_pct", 10.0)),
            enable_sl=bool(getattr(binding, "enable_stop_loss", False)),
            sl_pct=float(getattr(binding, "stop_loss_pct", 5.0)),
            hard_sl_pct=float(getattr(binding, "hard_stop_loss_pct", 15.0)),
        )
        if not tp_px and not sl_px:
            return None

        tp_str = format_decimal(quantize_step(tp_px, spec.tick_sz), spec.tick_sz) if tp_px else None
        sl_str = format_decimal(quantize_step(sl_px, spec.tick_sz), spec.tick_sz) if sl_px else None
        sz_str = format_decimal(Decimal(str(total_pos_sz)), spec.lot_sz)
        try:
            await adapter.cancel_tp_sl(inst_id, local_pos_side)
            res = await adapter.place_tp_sl(
                inst_id=inst_id,
                pos_side=local_pos_side,
                sz=sz_str,
                tp_px=tp_str,
                sl_px=sl_str,
                td_mode=account.margin_mode,
            )
            if res.get("ok"):
                return f"TP/SL({mode}): tp={tp_str or '-'} sl={sl_str or '-'}"
            return f"TP/SL({mode}) software-guard active (tp={tp_str or '-'} sl={sl_str or '-'})"
        except Exception as exc:
            logger.warning("Exchange TP/SL placement failed (%s); software guard active: %s", inst_id, exc)
            return f"TP/SL({mode}) software-guard fallback"

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
        symbol_map = json.loads(binding.symbol_map_json or "{}")
        inst_id, is_supported = adapter.resolve_symbol(sym, instruments, symbol_map)
        if not is_supported:
            return
        local_side = "long" if raw_side == "LONG" else "short"
        target_pos = next((p for p in positions if p.inst_id == inst_id and p.pos_side == local_side), None)
        if not target_pos or target_pos.pos <= 0:
            return
        spec = instruments[inst_id]
        order_side = "sell" if raw_side == "LONG" else "buy"
        cl_ord_id = f"mqr{int(time.time()*1000)}{uuid.uuid4().hex[:6]}"[:32]
        try:
            res = await self._place_order_idempotent(
                adapter=adapter,
                inst_id=inst_id,
                side=order_side,
                pos_side=local_side,
                ord_type="market",
                sz=format_decimal(Decimal(str(target_pos.pos)), spec.lot_sz),
                px=None,
                cl_ord_id=cl_ord_id,
                reduce_only=True,
                td_mode=account.margin_mode,
            )
            debt = await self._get_debt(db, binding.id, inst_id, raw_side)
            debt.pending_usd = 0.0
            await self._clear_guard(db, binding.id, inst_id, local_side)
            await self._record_execution(
                db, binding, lead, account, sym, inst_id, order_side.upper(), raw_side,
                "RECONCILE_CLOSE", "EXECUTED", 0.0, 0.0, 0.0,
                my_qty=target_pos.pos, my_price=target_pos.avg_px,
                my_notional=round(target_pos.notional_usd, 2), order_id=res.ord_id,
                details="Closed residual position matching lead position-history closure",
                notify_cfg=notify_cfg,
            )
        except Exception as exc:
            logger.error("Reconcile close failed for %s: %s", inst_id, exc)

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
            # Check if this binding actually ever traded or holds a guard on this position, OR symbol passes binding filter
            whitelist = json.loads(binding.symbol_whitelist_json or "[]")
            blacklist = json.loads(binding.symbol_blacklist_json or "[]")
            if not RiskEngine.check_symbol_filter(pos.symbol, whitelist, blacklist).allowed:
                continue

            raw_ps = "LONG" if pos.pos_side == "long" else "SHORT"
            if mode == "auto_close":
                spec = instruments.get(pos.inst_id)
                if not spec:
                    continue
                order_side = "sell" if pos.pos_side == "long" else "buy"
                cl_ord_id = f"mqo_orp{int(now*1000)}"[:32]
                try:
                    res = await self._place_order_idempotent(
                        adapter=adapter,
                        inst_id=pos.inst_id,
                        side=order_side,
                        pos_side=pos.pos_side,
                        ord_type="market",
                        sz=format_decimal(Decimal(str(pos.pos)), spec.lot_sz),
                        px=None,
                        cl_ord_id=cl_ord_id,
                        reduce_only=True,
                        td_mode=account.margin_mode,
                    )
                    await self._clear_guard(db, binding.id, pos.inst_id, pos.pos_side)
                    await self._record_execution(
                        db, binding, lead, account, pos.symbol, pos.inst_id, order_side.upper(), raw_ps,
                        "ORPHAN_AUTO_CLOSE", "EXECUTED", 0.0, 0.0, 0.0,
                        my_qty=pos.pos, my_price=pos.avg_px,
                        my_notional=round(pos.notional_usd, 2), order_id=res.ord_id,
                        details="Auto-closed ghost/orphan position missing from lead openPositions",
                        notify_cfg=ntf_cfg,
                    )
                    actions_taken.append(f"closed:{pos.inst_id}:{pos.pos_side}")
                except Exception as exc:
                    logger.error("Orphan auto-close failed for %s: %s", pos.inst_id, exc)
            else:
                alert_key = f"{binding.id}:{pos.inst_id}:{pos.pos_side}"
                if now - self._orphan_alert_ts.get(alert_key, 0.0) >= 900.0:
                    self._orphan_alert_ts[alert_key] = now
                    await self._record_execution(
                        db, binding, lead, account, pos.symbol, pos.inst_id, "ALERT", raw_ps,
                        "ORPHAN_ALERT", "BLOCKED_ORPHAN_DETECTED", 0.0, 0.0, 0.0,
                        my_qty=pos.pos, my_price=pos.avg_px,
                        my_notional=round(pos.notional_usd, 2),
                        details="Ghost position detected: present locally but absent from lead openPositions",
                        notify_cfg=ntf_cfg,
                    )
                    actions_taken.append(f"alerted:{pos.inst_id}:{pos.pos_side}")
        return actions_taken

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
        mode = getattr(binding, "tp_sl_mode", "follow_lead") or "follow_lead"
        if mode == "follow_lead":
            return []
        notify_cfg = notify_cfg or await self._get_config(db, "notifications", DEFAULT_SYSTEM_CONFIG["notifications"])
        triggered: List[str] = []

        for pos in local_positions:
            if pos.pos <= 0 or pos.pos_side not in ("long", "short"):
                continue
            spec = instruments.get(pos.inst_id)
            if not spec:
                continue

            try:
                book = await adapter.get_orderbook(pos.inst_id, depth=1)
                cur_px = (book.bids[0][0] if pos.pos_side == "long" and book.bids else
                          book.asks[0][0] if pos.pos_side == "short" and book.asks else pos.avg_px)
            except Exception:
                cur_px = pos.avg_px
            if cur_px <= 0:
                continue

            guard = await self._upsert_guard(db, binding.id, pos.inst_id, pos.pos_side, pos.avg_px or cur_px)
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
                order_side = "sell" if is_long else "buy"
                raw_ps = "LONG" if is_long else "SHORT"
                cl_ord_id = f"mqtpsl{int(time.time()*1000)}"[:32]
                try:
                    res = await self._place_order_idempotent(
                        adapter=adapter,
                        inst_id=pos.inst_id,
                        side=order_side,
                        pos_side=pos.pos_side,
                        ord_type="market",
                        sz=format_decimal(Decimal(str(pos.pos)), spec.lot_sz),
                        px=None,
                        cl_ord_id=cl_ord_id,
                        reduce_only=True,
                        td_mode=account.margin_mode,
                    )
                    await self._clear_guard(db, binding.id, pos.inst_id, pos.pos_side)
                    await self._record_execution(
                        db, binding, lead, account, pos.symbol, pos.inst_id, order_side.upper(), raw_ps,
                        "TPSL_CLOSE", "EXECUTED", 0.0, 0.0, 0.0,
                        my_qty=pos.pos, my_price=cur_px,
                        my_notional=round(pos.pos * float(spec.ct_val) * cur_px, 2),
                        order_id=res.ord_id, details=reason, notify_cfg=notify_cfg,
                    )
                    triggered.append(f"{pos.inst_id}:{pos.pos_side}")
                except Exception as exc:
                    logger.error("Software TP/SL close failed for %s: %s", pos.inst_id, exc)
        return triggered
