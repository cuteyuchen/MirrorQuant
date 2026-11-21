from __future__ import annotations

from app.core.time_util import BEIJING_TZ, to_utc_iso

import asyncio
import json
import logging
import math
import time
import hashlib
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_UP
from typing import Any, Callable, Coroutine, Dict, List, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import DEFAULT_SYSTEM_CONFIG
from app.core.security import CredentialVault
from app.core.secret_mask import redact_proxy_userinfo
from app.schemas.api import ResilienceSettings, RiskDefaults
from app.models.domain import (
    BindingDebt,
    BindingDelivery,
    BindingPositionProgress,
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
    NotificationOutbox,
    UserLeadSubscription,
    UserPreference,
)
from app.services.adapters.base import (
    BaseExchangeAdapter,
    ExchangeAPIError,
    ExchangePosition,
    OrderRejected,
    OrderResult,
    get_collateral_balance,
)
from app.services.adapters.factory import create_exchange_adapter
from app.services.engine.risk import RiskEngine
from app.services.engine.equity import collateral_available, equity_baseline, persist_equity
from app.services.engine.sizer import PositionSizer, exact, format_decimal, format_exact, quantize_down
from app.services.engine.serialization import serialized_execution
from app.services.lead_collector.binance_bapi import (
    direction_label,
    expand_slim_order,
    group_orders_into_batches,
    is_open_leg,
    order_dedup_key,
    is_authoritative_snapshot,
    is_routable_snapshot,
    positions_have_close_evidence,
    source_fill_kind,
    validate_source_fill,
)
from app.services.notifier.outbox import CHANNELS, enqueue_notification
from app.services.lead_collector.snapshot_clock import (
    closed_interval, event_interval, event_timestamp, snapshot_is_current,
    snapshot_observed_ms, snapshot_source_bound_ms, snapshot_times, source_batch_interval,
)
from app.services.notifier.trade import _lead_section, lead_only_message, resolve_display_unit, trade_message, trade_sections
from app.services.proxy.manager import ProxyManager

logger = logging.getLogger("mirrorquant.router")

BroadcastFn = Optional[Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]]


class UnknownOrderOutcome(RuntimeError):
    """A mutation may have been accepted; reconciliation must precede further work."""


class SourceExitUnproven(UnknownOrderOutcome):
    """出场证据不足时只延期，不消耗下单失败预算。"""


TERMINAL_STATES = {"filled", "canceled", "rejected", "expired"}

# 交易所查无此单后，意图超龄多少才可转为 CANCELED（毫秒）：宽限期内
# 刚提交的订单可能尚未可查，不得误杀。
NOT_FOUND_CANCEL_GRACE_MS = 300_000

# 开仓终态死意图最多重驱次数：超过则停手等人看，避免无限重试。
MAX_OPEN_RETRY_ATTEMPTS = 3

# 未动钱的风控结论（拦截/跳过/失败/累积待执行）：同一投递事件只推送一次。
# 重启后重新评估同一待处理投递时，结论变化也不得重复推送；真正成交、
# 部分成交、限价单挂出/失效属于资金动作，不受此限。
_GUARD_NO_TRADE_STATUSES = ("FAILED", "ACCUMULATED_DEBT")


def _is_guard_outcome(status: str) -> bool:
    return status.startswith("BLOCKED") or status.startswith("SKIPPED") or status in _GUARD_NO_TRADE_STATUSES


def _source_notional_conflicts(recorded, fresh) -> bool:
    """名义金额冲突只认超出舍入抖动的差异。

    数量是硬证据（调用处已精确比对）；名义金额由源端均价派生，
    BAPI 在不同拉取中会对同一订单返回末位不同的均价，实测抖动
    不超过名义金额的 0.05%。0.1% 容差只放行舍入噪声，真正的
    价格重述仍会被拦截。
    """
    recorded, fresh = exact(recorded), exact(fresh)
    tolerance = max(abs(recorded) * Decimal("0.001"), Decimal("0.01"))
    return abs(fresh - recorded) > tolerance


def _sequence_matches_interval(source_sequence, interval) -> bool:
    """投递序号与区间终点允许 1ms 的端点约定差。

    批次分组用左闭右开区间、投递序号取区间端点时，同一事件的
    两个派生值会稳定相差 1ms；这是约定差，不是顺序矛盾。
    """
    return interval is not None and abs(int(source_sequence) - int(interval[1])) <= 1


def _exposure_ratio_matches(lead_ratio, my_ratio, tolerance_pct) -> bool:
    """敞口比率（仓位名义金额 / 账户权益）相对差异在容差内即视为同一仓位。

    与下单 sizing 同一口径：sizer 按“带单名义金额 / 带单保证金余额”
    折算跟单目标，这里反向用同一比率判断已有仓位是否就是带单员
    的那笔仓位。容差为 0 只认完全一致；任一比率无效或容差为负
    都不匹配，绝不猜测。
    """
    try:
        lead, mine, tol = exact(lead_ratio), exact(my_ratio), exact(tolerance_pct)
    except (ValueError, InvalidOperation, TypeError, AttributeError):
        return False
    if lead <= 0 or mine <= 0 or tol < 0:
        return False
    return abs(mine - lead) <= lead * tol / 100


def _effective_open_leverage(existing_leverage, target_notional, equity, max_symbol_leverage) -> int:
    """开仓/加仓杠杆：品种已有仓位时沿用交易所当前杠杆，无仓位时动态计算。

    杠杆是按品种设置的：每次加仓都重算并重设，会擅自改动整仓（含认领仓位）的杠杆。
    已有仓位时沿用其杠杆，认领仓与后续加仓自然一致；档位上限是交易所硬约束，
    已有杠杆高于档位上限时才降到上限。已有杠杆无效（空/零/负/NaN）时回落动态计算。
    """
    try:
        current = float(existing_leverage) if existing_leverage is not None else 0.0
    except (TypeError, ValueError):
        current = 0.0
    if current > 0:
        return min(int(max_symbol_leverage), max(1, int(current)))
    return PositionSizer.compute_dynamic_leverage(
        target_notional=target_notional, equity=equity, max_symbol_leverage=max_symbol_leverage)


def lead_position_context(db) -> Optional[Dict[str, Any]]:
    """Recover pre/post lead inventory for a notification from the current delivery scope.

    Everything comes from the delivery payload (the independent source fill),
    never from the follower-mapped record fields: a mapped follower close must
    not rewrite the lead's opening action. The admitted snapshot holds
    post-event holdings; pre-event inventory is recovered arithmetically from
    the source fill quantity. Returns None outside a delivery scope, for
    historical closes, for fills without a positive quantity, or on any
    inconsistent input, so callers always fall back to the leg-only labels.
    """
    delivery = db.info.get("delivery")
    admission = db.info.get("snapshot_admission")
    if delivery is None or admission is None:
        return None
    try:
        payload = json.loads(delivery.payload_json)
        if not isinstance(payload, dict) or "_closed" in payload:
            return None
        symbol = payload.get("symbol")
        position_side = payload.get("positionSide")
        side = payload.get("side")
        if side not in ("BUY", "SELL") or position_side not in ("LONG", "SHORT"):
            return None
        qty = exact(payload.get("qtyExact", payload.get("qty")) or 0)
        if qty <= 0:
            return None
        snapshot = admission[1]
        if not isinstance(snapshot, dict):
            return None
        leg = f"{symbol}_{position_side}"
        current = None
        open_count = 0
        for position in snapshot.get("openPositions") or []:
            if not isinstance(position, dict):
                continue
            amount = exact(position.get("qtyExact", position.get("qty")) or 0)
            if amount > 0:
                open_count += 1
            if f"{position.get('symbol')}_{position.get('positionSide')}" == leg:
                current = amount
        if current is None:
            current = Decimal("0")
        previous = current - qty if is_open_leg(side, position_side) else current + qty
        if previous < 0 or current < 0:
            return None
        return {"previous_qty": previous, "current_qty": current, "open_count": open_count}
    except (ValueError, TypeError, KeyError, AttributeError, InvalidOperation):
        return None


class CopyRouter:
    """Routes lead order batches & closures across N:M CopyBindings with risk, TP/SL & resilience."""

    def __init__(self, vault: CredentialVault, broadcast_fn: BroadcastFn = None):
        self.vault = vault
        self.broadcast_fn = broadcast_fn

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
        today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
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
        except OrderRejected:
            # 明确拒单不会产生未决订单，也不进行歧义查单。
            raise
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
        ).execution_options(populate_existing=True))
        if allocation is not None and allocation.account_id != account_id:
            raise UnknownOrderOutcome("Allocation account differs from binding account")
        if allocation is None:
            allocation = PositionAllocation(binding_id=binding_id, account_id=account_id,
                inst_id=inst_id, pos_side=pos_side, owned_qty=0.0, entry_cost=0.0,
                owned_qty_exact="0", entry_cost_exact="0", generation=0, opened_at_ms=0)
            db.add(allocation)
            await db.flush()
        # 旧浮点记录升级为精确文本时也统一格式，避免把 1.0 写成不同的数量字符串。
        if allocation.owned_qty_exact is None:
            allocation.owned_qty_exact = format_exact(self._owned_qty(allocation))
        if allocation.entry_cost_exact is None:
            allocation.entry_cost_exact = format_exact(self._entry_cost(allocation))
        return allocation

    @staticmethod
    def _owned_qty(allocation):
        quantity = exact(allocation.owned_qty_exact if allocation.owned_qty_exact is not None else allocation.owned_qty)
        if quantity < 0:
            raise UnknownOrderOutcome("Negative binding ownership")
        return quantity

    @staticmethod
    def _entry_cost(allocation):
        return exact(allocation.entry_cost_exact if allocation.entry_cost_exact is not None else allocation.entry_cost)

    @staticmethod
    def _pending_debt(debt):
        return exact(debt.pending_usd_exact if debt.pending_usd_exact is not None else debt.pending_usd)

    @staticmethod
    def _set_debt(debt, value):
        debt.pending_usd_exact = format_exact(value)
        debt.pending_usd = float(value)

    @staticmethod
    def _state(value: Any) -> str:
        raw = str(value or "unknown").lower().replace("-", "_")
        return {"cancelled": "canceled", "partiallyfilledcanceled": "canceled",
                "partially_filled_canceled": "canceled", "deactivated": "canceled",
                "expired_in_match": "expired",  # 币安自成交保护终态，不再作为未知订单。
                "full_fill": "filled", "live": "new", "partiallyfilled": "partially_filled"}.get(raw, raw)

    async def _exit_delivery(self, db, binding, account, allocation, event_id, target=None):
        if allocation.account_id != account.id or binding.account_id != account.id:
            raise UnknownOrderOutcome("Exit allocation account differs from binding account")
        delivery = await db.get(BindingDelivery, (binding.id, event_id), populate_existing=True)
        if delivery is None:
            return None
        attempts = (await db.scalars(select(OrderIntent).where(
            OrderIntent.binding_id == binding.id,
            or_(OrderIntent.source_event_id == event_id,
                OrderIntent.delivery_event_id == event_id)))).all()
        if delivery.exit_target_qty is None:
            if attempts or delivery.exit_progress == "REVIEW_REQUIRED":
                delivery.state, delivery.exit_progress = "PENDING", "REVIEW_REQUIRED"
                await db.commit()
                raise UnknownOrderOutcome("Legacy exit target cannot be safely reconstructed")
            if target is None:
                raise UnknownOrderOutcome("Exit target missing")
            owned = self._owned_qty(allocation)
            target = exact(target)
            if target < 0 or target > owned:
                raise UnknownOrderOutcome("Invalid initial exit target")
            delivery.exit_account_id = account.id
            delivery.exit_inst_id = allocation.inst_id
            delivery.exit_pos_side = allocation.pos_side
            delivery.exit_allocation_id = allocation.id
            delivery.exit_generation = allocation.generation
            # 冻结目标与后续成交进度使用同一精确文本格式，重启后仍可直接比较。
            delivery.exit_initial_owned_qty = format_exact(owned)
            delivery.exit_target_qty = format_exact(target)
            delivery.exit_filled_qty = "0"
            delivery.exit_remaining_qty = format_exact(target)
            delivery.exit_progress = "READY" if target > 0 else "COMPLETE"
            delivery.state = "PENDING" if target > 0 else "APPLIED"
            await db.commit()
        scope = (account.id, allocation.inst_id, allocation.pos_side, allocation.id, allocation.generation)
        saved = (delivery.exit_account_id, delivery.exit_inst_id, delivery.exit_pos_side,
                 delivery.exit_allocation_id, delivery.exit_generation)
        if scope != saved or allocation.account_id != account.id or binding.account_id != account.id:
            raise UnknownOrderOutcome("Exit allocation identity or generation changed")
        target, filled, remaining = map(exact, (delivery.exit_target_qty, delivery.exit_filled_qty,
                                                delivery.exit_remaining_qty))
        if (min(target, filled, remaining) < 0 or target != filled + remaining or
                target > exact(delivery.exit_initial_owned_qty) or remaining > self._owned_qty(allocation)):
            raise UnknownOrderOutcome("Conflicting exit progress or ownership evidence")
        if any((intent.account_id, intent.inst_id, intent.pos_side, intent.reduce_only) !=
               (account.id, allocation.inst_id, allocation.pos_side, True) for intent in attempts):
            raise UnknownOrderOutcome("Exit intent scope conflicts with delivery")
        if sum((exact(intent.filled_qty) for intent in attempts), Decimal("0")) != filled:
            raise UnknownOrderOutcome("Exit cumulative fills conflict with delivery")
        return delivery

    async def _absorb_exit_remainder(self, db, binding, lead, account, delivery, symbol, inst_id,
                                     side, position_side, action, lead_qty, lead_price, lead_notional,
                                     remaining, minimum) -> None:
        """Complete an exit delivery whose remainder can never trade.

        The remainder is a quantization residue: the exit target carries more
        precision than the exchange lot step, so the last fill rounds down and
        leaves dust below the exchange minimum. Parking on it stalls the
        delivery forever and blocks every later event for the binding, so the
        remainder is folded into the filled quantity and the delivery is
        marked complete. The allocation is untouched: the dust never
        corresponded to physical quantity, and the ledger already reflects
        the fills exactly. The completion is recorded quietly (no push): the
        substantive fill was already notified.
        """
        delivery.exit_filled_qty = format_exact(exact(delivery.exit_target_qty))
        delivery.exit_remaining_qty = "0"
        delivery.exit_progress = "COMPLETE"
        delivery.state = "APPLIED"
        await self._record_execution(
            db, binding, lead, account, symbol, inst_id, side, position_side, action,
            "EXIT_DUST_ABSORBED", lead_qty, lead_price, lead_notional,
            details=(f"Exit remainder {format_exact(remaining)} below exchange minimum "
                     f"{format_exact(minimum)}; absorbed to complete delivery"),
            notify_cfg=None, complete_delivery=False)

    @staticmethod
    def _confirmed_fill(info: Optional[Dict[str, Any]], res: OrderResult) -> float:
        raw = info.get("accFillSz") if info is not None else None
        value = exact(raw if raw is not None else (res.fill_sz if res.fill_sz is not None else 0))
        if value < 0:
            raise ValueError("Invalid negative cumulative fill")
        return float(value)

    @serialized_execution
    async def _apply_intent_info(self, db, intent, info, spec, source_time_ms=0, spread_pct=None):
        await db.refresh(intent)
        binding = await db.get(CopyBinding, intent.binding_id, populate_existing=True)
        account = await db.get(TradingAccount, intent.account_id)
        if binding is None or account is None or binding.account_id != intent.account_id:
            raise UnknownOrderOutcome("Intent account no longer matches binding")
        expected_side = ("sell" if intent.pos_side == "long" else "buy") if intent.reduce_only else (
            "buy" if intent.pos_side == "long" else "sell")
        if spec.inst_id != intent.inst_id:
            raise UnknownOrderOutcome("Order instrument differs from specification")
        for field, expected in (("clOrdId", intent.id), ("instId", intent.inst_id),
                                ("posSide", intent.pos_side), ("accountId", intent.account_id),
                                ("side", expected_side)):
            if info.get(field) not in (None, "") and str(info[field]).lower() != str(expected).lower():
                raise UnknownOrderOutcome(f"Order evidence has conflicting {field}")
        raw = info.get("raw") or {}
        if isinstance(raw, dict):
            expected_side = ("sell" if intent.pos_side == "long" else "buy") if intent.reduce_only else (
                "buy" if intent.pos_side == "long" else "sell")
            for field, expected in (("symbol", intent.inst_id), ("instId", intent.inst_id),
                                    ("positionSide", intent.pos_side), ("posSide", intent.pos_side),
                                    ("side", expected_side), ("clientOrderId", intent.id),
                                    ("clOrdId", intent.id)):
                if raw.get(field) not in (None, "") and str(raw[field]).lower() != str(expected).lower():
                    raise UnknownOrderOutcome(f"Raw order evidence has conflicting {field}")
            info = dict(info)
            for field, aliases in (("accFillSz", ("accFillSz", "executedQty", "cumExecQty", "baseVolume")),
                                   ("avgPx", ("avgPx", "avgPrice", "priceAvg"))):
                for alias in aliases:
                    if raw.get(alias) not in (None, ""):
                        info[field] = raw[alias]
                        break
        if intent.exchange_order_id and info.get("ordId") and str(info["ordId"]) != intent.exchange_order_id:
            raise UnknownOrderOutcome("Exchange order identity changed")
        if info.get("accFillSz") in (None, ""):
            intent.state = "UNKNOWN"
            await db.commit()
            raise UnknownOrderOutcome("Missing cumulative fill quantity")
        cumulative = exact(info["accFillSz"])
        prior = exact(intent.filled_qty)
        if cumulative < prior or cumulative > exact(intent.requested_qty):
            raise UnknownOrderOutcome(f"Invalid cumulative fill for {intent.id}")
        delta = cumulative - prior
        avg_px = exact(info.get("avgPx") or (intent.avg_px if delta == 0 else 0))
        if delta > 0 and avg_px <= 0:
            intent.state = "UNKNOWN"
            await db.commit()
            raise UnknownOrderOutcome(f"Missing fill price for {intent.id}")
        if delta == 0 and prior > 0 and avg_px != exact(intent.avg_px):
            raise UnknownOrderOutcome("Confirmed cumulative fill price changed without a new fill")
        incremental_cost = cumulative * avg_px - prior * exact(intent.avg_px)
        if delta > 0 and (incremental_cost <= 0 or not info.get("ordId")):
            intent.state = "UNKNOWN"
            await db.commit()
            raise UnknownOrderOutcome("Missing order identity or invalid incremental fill cost")
        price = incremental_cost / delta if delta > 0 else avg_px
        previous_state = intent.state.lower()
        allocation = await self._get_allocation(db, intent.binding_id, intent.account_id, intent.inst_id, intent.pos_side)
        delivery = None
        if intent.reduce_only:
            delivery = await self._exit_delivery(db, binding, account, allocation,
                intent.delivery_event_id or intent.source_event_id)
            if delivery is not None and delta > exact(delivery.exit_remaining_qty):
                raise UnknownOrderOutcome("Confirmed close exceeds event remainder")
        debt = await self._get_debt(db, intent.binding_id, intent.inst_id, intent.pos_side.upper())
        if delta > 0:
            owned = self._owned_qty(allocation)
            if intent.reduce_only:
                if delta > owned:
                    raise UnknownOrderOutcome("Confirmed close exceeds binding allocation")
                remaining = max(Decimal("0"), owned - delta)
                allocation.entry_cost_exact = format_exact(self._entry_cost(allocation) * remaining / owned if owned else Decimal("0"))
                allocation.entry_cost = float(allocation.entry_cost_exact)
                allocation.owned_qty_exact = format_exact(remaining)
                allocation.owned_qty = float(remaining)
                if remaining > 0 and owned > 0:
                    self._set_debt(debt, self._pending_debt(debt) * remaining / owned)
                if remaining == 0:
                    self._set_debt(debt, 0)
                    await self._clear_guard(db, intent.binding_id, intent.inst_id, intent.pos_side)
                if delivery is not None:
                    delivery.exit_filled_qty = format_exact(exact(delivery.exit_filled_qty) + delta)
                    delivery.exit_remaining_qty = format_exact(exact(delivery.exit_remaining_qty) - delta)
            else:
                if owned == 0:
                    allocation.generation += 1
                    allocation.opened_at_ms = source_time_ms or intent.source_time_ms or int(time.time() * 1000)
                    await self._clear_guard(db, intent.binding_id, intent.inst_id, intent.pos_side)
                # Average fill prices are cumulative: subtract previous cost, not delta * average.
                new_cost = incremental_cost
                if new_cost <= 0:
                    raise UnknownOrderOutcome(f"Invalid incremental fill cost for {intent.id}")
                allocation.entry_cost_exact = format_exact(self._entry_cost(allocation) + new_cost)
                allocation.entry_cost = float(allocation.entry_cost_exact)
                allocation.owned_qty_exact = format_exact(owned + delta)
                allocation.owned_qty = float(allocation.owned_qty_exact)
                # 待补金额按“下单定价”结算：已成交量按定价折算，只有未成交量与量化零头才结转。
                # 按成交均价结算会把成交价与定价之间的价差噪声记成待补金额（顺价成交凭空产生
                # 欠单、逆价成交又把合法零头清零）；旧意图无定价记录时沿用成交均价。
                sizing_raw = intent.sizing_px
                sizing_px = exact(sizing_raw) if sizing_raw else None
                residue_px = sizing_px if sizing_px is not None and sizing_px > 0 else avg_px
                # 限价单未成交部分到期即失效，不结转为待补金额，避免与后续加仓重复下单。
                self._set_debt(debt, Decimal("0") if intent.expires_at_ms is not None else max(
                    Decimal("0"), exact(intent.target_usd) - cumulative * residue_px * spec.ct_val))
                await self._upsert_guard(db, intent.binding_id, intent.inst_id, intent.pos_side,
                    float(self._entry_cost(allocation) / self._owned_qty(allocation)))
        intent.filled_qty = format_exact(cumulative)
        intent.avg_px = format_exact(avg_px)
        intent.exchange_order_id = str(info.get("ordId") or intent.exchange_order_id or "") or None
        state = self._state(info.get("state"))
        if state in TERMINAL_STATES:
            intent.state = state.upper()
        elif intent.expires_at_ms is not None and state in ("new", "partially_filled"):
            # 滑点超限改挂的限价单：在簿等待是预期状态，不算结果未知。
            intent.state = "RESTING"
        else:
            intent.state = "UNKNOWN"
        if delivery is not None:
            complete = state in TERMINAL_STATES and exact(delivery.exit_remaining_qty) == 0
            delivery.state = "APPLIED" if complete else "PENDING"
            delivery.exit_progress = "COMPLETE" if complete else (
                "READY" if state in TERMINAL_STATES else "WAITING_ORDER")
        elif state in TERMINAL_STATES:
            delivery = await db.get(BindingDelivery, (intent.binding_id, intent.delivery_event_id or intent.source_event_id))
            if delivery is not None:
                delivery.state = "APPLIED"
        if delta > 0 or (state in TERMINAL_STATES and previous_state not in TERMINAL_STATES):
            # One transaction owns fill accounting, delivery, execution and outbox.
            # A crash before this commit rolls everything back to the durable intent.
            await self._record_intent_outcome(db, intent, spec, delta, price, state, spread_pct=spread_pct)
        else:
            await db.commit()
        return float(delta), float(price), state

    async def _record_intent_outcome(self, db, intent, spec, quantity, price, state, historical=False, spread_pct=None):
        binding = await db.get(CopyBinding, intent.binding_id)
        account = await db.get(TradingAccount, intent.account_id)
        lead = await db.get(LeadSource, binding.lead_id)
        delivery = await db.get(BindingDelivery, (binding.id, intent.delivery_event_id or intent.source_event_id))
        source = json.loads(delivery.payload_json) if delivery is not None else {}
        side = ("SELL" if intent.pos_side == "long" else "BUY") if intent.reduce_only else (
            "BUY" if intent.pos_side == "long" else "SELL")
        status = ("EXECUTED" if state in TERMINAL_STATES else "PARTIAL") if quantity > 0 else "UNFILLED"
        limit = None
        if intent.expires_at_ms is not None and not intent.reduce_only:
            # 限价单被撤且本次没有新成交：单独记为“限价单失效”，通知里写明原因与累计成交。
            if quantity <= 0 and state in ("canceled", "expired"):
                status = "LIMIT_EXPIRED"
            limit = {"px": intent.limit_px, "requested": intent.requested_qty, "filled": intent.filled_qty,
                     "reason": "timeout" if int(time.time() * 1000) >= intent.expires_at_ms else "lead_exit"}
        old_delivery = db.info.pop("delivery", None)
        db.info.pop("confirmed_execution", None)
        if delivery is not None:
            db.info["delivery"] = delivery
        try:
            # 与成交、执行记录和 outbox 同一事务确认，历史补账仍需人工审计。
            intent.reporting_complete = state in TERMINAL_STATES and not historical
            # 实际滑点 = 成交均价相对带单均价的绝对偏离（与滑点门同一口径）；无成交时不记。
            lead_avg_px = source.get("avgPrice") or 0
            realized_slip = 0.0
            if quantity > 0 and price and lead_avg_px:
                try:
                    realized_slip = round(abs(float(price) - float(lead_avg_px)) / float(lead_avg_px) * 100.0, 4)
                except (TypeError, ValueError, ZeroDivisionError):
                    realized_slip = 0.0
            record = await self._record_execution(db, binding, lead, account,
                source.get("symbol") or intent.inst_id, intent.inst_id, side, intent.pos_side.upper(),
                ("CLOSE_" if intent.reduce_only else "OPEN_") + intent.pos_side.upper(), status,
                source.get("qty") or 0, lead_avg_px, source.get("notional") or 0,
                my_qty=quantity, my_price=price, slippage_pct=realized_slip, spread_pct=spread_pct,
                my_notional=float(quantity * price * spec.ct_val), order_id=intent.exchange_order_id,
                details=f"Confirmed cumulative {intent.filled_qty} of {intent.requested_qty}; "
                    f"remaining {exact(intent.requested_qty) - exact(intent.filled_qty)}; order {state}. "
                    f"Event remaining {delivery.exit_remaining_qty if delivery is not None else 'untracked'}; "
                    f"replacement only after terminal reconciliation. Intent accounting key {intent.id}" +
                    ("; Notification delivery unknown; human reconciliation required" if historical else ""),
                notify_cfg=None if historical else await self._get_config(db, "notifications", DEFAULT_SYSTEM_CONFIG["notifications"]),
                complete_delivery=not intent.reduce_only,
                notification_state="unknown" if historical else "not_required", limit=limit)
            db.info["confirmed_execution"] = record
        finally:
            db.info.pop("delivery", None)
            if old_delivery is not None:
                db.info["delivery"] = old_delivery

    async def _mark_unsubmitted_source_exit(self, db, intent):
        # 只有未提交且整个出场未成交时，才允许凭新鲜证据重试；人工复核不能被覆盖。
        if (not intent.reduce_only or intent.state != "REJECTED" or
                intent.exchange_order_id is not None or exact(intent.filled_qty) != 0):
            return
        delivery = await db.get(BindingDelivery, (intent.binding_id,
            intent.delivery_event_id or intent.source_event_id))
        if (delivery is not None and delivery.exit_target_qty is not None and
                exact(delivery.exit_filled_qty) == 0 and delivery.exit_progress != "REVIEW_REQUIRED"):
            delivery.exit_progress = "SOURCE_UNPROVEN"

    @staticmethod
    def _may_retry_terminal_attempts(order, attempts) -> bool:
        """终态意图重驱条件：减仓沿用旧规则；开仓只重驱“从未上过
        交易所”的死意图（全部 CANCELED + 零成交 + 无交易所订单号），
        且最多重驱 MAX_OPEN_RETRY_ATTEMPTS 次。已成交/被拒的意图
        绝不重驱，避免重复下单。
        """
        if order["reduce_only"]:
            return True
        if len(attempts) > MAX_OPEN_RETRY_ATTEMPTS:
            return False
        return all(
            intent.state.lower() == "canceled"
            and exact(intent.filled_qty) == 0
            and not intent.exchange_order_id
            for intent in attempts
        )

    @serialized_execution
    async def _submit_confirmed(self, db, binding, account, adapter, spec,
                                source_event_id, target_usd=0, source_time_ms=0, observed_at_ms=0,
                                retry_terminal=False, source_evidence=None, expires_at_ms=None,
                                spread_pct=None, sizing_px=None, **order):
        material = f"{account.id}:{binding.id}:{source_event_id}"
        db.info.pop("confirmed_execution", None)
        cid = "mq" + hashlib.sha256(material.encode()).hexdigest()[:30]
        delivery_event_id = None
        if retry_terminal:
            attempts = (await db.scalars(select(OrderIntent).where(
                OrderIntent.account_id == account.id, OrderIntent.binding_id == binding.id,
                or_(OrderIntent.source_event_id == source_event_id,
                    OrderIntent.delivery_event_id == source_event_id)))).all()
            pending = [intent for intent in attempts if intent.state.lower() not in TERMINAL_STATES]
            if pending:
                cid = pending[0].id
            elif attempts and self._may_retry_terminal_attempts(order, attempts):
                cid = "mq" + hashlib.sha256(f"{material}:retry:{len(attempts)}".encode()).hexdigest()[:30]
                delivery_event_id, source_event_id = source_event_id, "retry:" + cid
        order["cl_ord_id"] = cid
        intent = await db.get(OrderIntent, cid, populate_existing=True)
        if intent is None:
            if source_evidence is not None:
                await self._validate_exit_source(db, binding, account, *source_evidence)
            admission = db.info.get("snapshot_admission")
            saved_required = False
            if admission is not None:
                admission_lead, admission_snapshot = admission
                await db.refresh(admission_lead, ["snapshot_json"])
                try:
                    saved_snapshot = json.loads(admission_lead.snapshot_json or "{}")
                except (ValueError, TypeError):
                    saved_snapshot = None
                saved_required = bool(saved_snapshot)
                if not (is_routable_snapshot(admission_snapshot) and snapshot_is_current(
                        admission_snapshot, saved_snapshot)):
                    # 出场快照失效仍是证据延期，不计为真实下单失败。
                    if order["reduce_only"]:
                        raise SourceExitUnproven("Source exit snapshot expired, superseded or incomparable before submission")
                    raise RuntimeError("Lead snapshot expired, superseded or incomparable before submission")
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
                source_event_id=source_event_id, delivery_event_id=delivery_event_id,
                state="SUBMITTING", requested_qty=order["sz"],
                filled_qty="0", avg_px="0", reduce_only=order["reduce_only"], target_usd=format_exact(target_usd), source_time_ms=source_time_ms,
                # 只有“滑点超限改挂限价单”带到期时间，到期后由对账流程撤单。
                expires_at_ms=expires_at_ms, limit_px=order.get("px") if expires_at_ms is not None else None,
                sizing_px=format_exact(sizing_px) if sizing_px is not None else None)
            db.add(intent)
            await db.commit()  # Durable before POST; restart can only query this client ID.
            if source_evidence is not None:
                try:
                    await self._validate_exit_source(db, binding, account, *source_evidence)
                except Exception as exc:
                    intent.state = "REJECTED"
                    if isinstance(exc, SourceExitUnproven):
                        await self._mark_unsubmitted_source_exit(db, intent)
                    await db.commit()
                    raise
            if admission is not None:
                await db.refresh(admission_lead, ["snapshot_json"])
                try:
                    saved_snapshot = json.loads(admission_lead.snapshot_json or "{}")
                except (ValueError, TypeError):
                    saved_snapshot = None
                if not ((saved_snapshot or not saved_required) and
                        is_routable_snapshot(admission_snapshot) and
                        snapshot_is_current(admission_snapshot, saved_snapshot)):
                    intent.state = "REJECTED"
                    if intent.reduce_only:
                        await self._mark_unsubmitted_source_exit(db, intent)
                    else:
                        delivery = await db.get(BindingDelivery, (binding.id,
                            intent.delivery_event_id or intent.source_event_id))
                        if delivery is not None and delivery.exit_target_qty is None:
                            delivery.state = "APPLIED"
                    await db.commit()
                    if intent.reduce_only:
                        raise SourceExitUnproven("Source exit snapshot became unsafe at submission boundary; no order posted")
                    raise RuntimeError("Lead snapshot became unsafe at submission boundary; no order posted")
            try:
                res = await self._place_order_idempotent(adapter=adapter, **order)
            except OrderRejected:
                intent.state = "REJECTED"
                # 本次开仓投递已明确结束，后续独立事件仍可执行。
                if not intent.reduce_only:
                    delivery = await db.get(BindingDelivery, (binding.id,
                        intent.delivery_event_id or intent.source_event_id))
                    if delivery is not None:
                        delivery.state = "APPLIED"
                await db.commit()
                raise
            except BaseException:
                intent.state = "UNKNOWN"
                await db.commit()
                raise
        else:
            if (intent.account_id, intent.binding_id, intent.inst_id, intent.pos_side, intent.reduce_only) != (
                    account.id, binding.id, order["inst_id"], order["pos_side"], order["reduce_only"]):
                raise UnknownOrderOutcome("Existing intent scope differs from requested order")
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
                info = {"ordId": res.ord_id, "accFillSz": res.fill_sz, "avgPx": res.avg_px,
                        "state": res.state, "raw": res.raw}
            if info is not None:
                info = dict(info)
                if info.get("accFillSz") is None and res.fill_sz is not None and res.fill_sz > 0:
                    info["accFillSz"] = res.fill_sz
                if (info.get("avgPx") in (None, "") and res.avg_px and
                        exact(info.get("accFillSz") or 0) == exact(res.fill_sz or 0)):
                    info["avgPx"] = res.avg_px
                try:
                    delta, price, state = await self._apply_intent_info(db, intent, info, spec, source_time_ms, spread_pct=spread_pct)
                except BaseException:
                    # Do not let a later failure record commit tentative accounting.
                    await db.rollback()
                    db.info.pop("confirmed_execution", None)
                    intent = await db.get(OrderIntent, cid)
                    if intent.state.lower() not in TERMINAL_STATES:
                        intent.state = "UNKNOWN"
                        await db.commit()
                    await db.refresh(binding)
                    await db.refresh(account)
                    await db.get(LeadSource, binding.lead_id, populate_existing=True)
                    delivery = db.info.get("delivery")
                    if delivery is not None:
                        await db.refresh(delivery)
                    raise
                if state in TERMINAL_STATES:
                    return OrderResult(intent.exchange_order_id or "", cid, delta, price, state)
            if attempt < 3:
                await asyncio.sleep(0.1)
        if intent.expires_at_ms is not None and intent.state == "RESTING":
            # 限价单已被交易所受理并在簿等待；后续成交/到期由对账流程处理并通知。
            return OrderResult(intent.exchange_order_id or "", cid, 0.0, float(intent.avg_px), "resting")
        intent.state = "UNKNOWN"
        await db.commit()
        raise UnknownOrderOutcome(f"Order placement outcome ambiguous for {cid}")

    @serialized_execution
    async def reconcile_pending_orders(self, db, account, adapter, instruments, relevant_positions=None,
                                       cancel_resting_for=None):
        db.info.pop("confirmed_execution", None)
        # 历史终态只审计未验证的账本；交易所查单仅针对非终态意图。
        unresolved = not await self._audit_completed_intents(db, account, instruments, relevant_positions)
        intent_ids = (await db.scalars(select(OrderIntent.id).where(
            OrderIntent.account_id == account.id,
            func.lower(OrderIntent.state).not_in(TERMINAL_STATES),
        ))).all()
        for intent_id in intent_ids:
            intent = await db.get(OrderIntent, intent_id, populate_existing=True)
            if relevant_positions is not None and (intent.inst_id, intent.pos_side) not in relevant_positions:
                continue
            spec = instruments.get(intent.inst_id)
            try:
                if not await self._recover_intent_reporting(db, intent, spec,
                        require_notifications=relevant_positions is None):
                    unresolved = True
                    continue
                if intent.state.lower() in TERMINAL_STATES:
                    continue
                if spec is None:
                    unresolved = True
                    continue
                await db.commit()
                info = await adapter.get_order(intent.inst_id, cl_ord_id=intent.id)
                if info is None:
                    if await self._cancel_never_placed_intent(db, account, intent):
                        continue
                    unresolved = True
                    continue
                _, _, state = await self._apply_intent_info(db, intent, info, spec)
                if state not in TERMINAL_STATES and intent.state == "RESTING":
                    # 在簿限价单：到期或要给本绑定的出场让路时撤单；否则继续等待，不阻塞账户。
                    if (int(time.time() * 1000) < intent.expires_at_ms and
                            cancel_resting_for != intent.binding_id):
                        continue
                    state = await self._cancel_resting(db, intent, adapter, spec)
                unresolved |= state not in TERMINAL_STATES
            except Exception:
                binding_id = intent.binding_id
                await db.rollback()
                db.info.pop("confirmed_execution", None)
                await db.refresh(account)
                binding = await db.get(CopyBinding, binding_id, populate_existing=True)
                await db.get(LeadSource, binding.lead_id, populate_existing=True)
                delivery = db.info.get("delivery")
                if delivery is not None:
                    await db.refresh(delivery)
                logger.exception("Pending order remains unresolved: %s", intent_id)
                unresolved = True
        return not unresolved

    async def _cancel_never_placed_intent(self, db, account, intent) -> bool:
        """交易所确认查无此单、且意图从未获得交易所订单号/成交时，
        将超龄意图转为 CANCELED，让投递可以带新客户端订单号重驱。

        宽限期内（创建后 5 分钟）不转：刚提交的订单可能尚未可查。
        只有“查无此单 + 零成交 + 无交易所订单号”三者齐备才转，
        绝不以此处理已成交或已确认存在的订单。
        """
        created = intent.created_at
        if created is not None:
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_ms = (datetime.now(timezone.utc) - created).total_seconds() * 1000
        else:
            age_ms = int(time.time() * 1000) - int(intent.source_time_ms or 0)
        if (intent.exchange_order_id or exact(intent.filled_qty) != 0
                or age_ms <= NOT_FOUND_CANCEL_GRACE_MS):
            return False
        binding = await db.get(CopyBinding, intent.binding_id, populate_existing=True)
        if binding is None:
            return False
        lead = await db.get(LeadSource, binding.lead_id, populate_existing=True)
        if lead is None:
            return False
        intent.state = "CANCELED"
        delivery = await db.get(BindingDelivery,
            (intent.binding_id, intent.delivery_event_id or intent.source_event_id))
        previous_delivery = db.info.get("delivery")
        if delivery is not None:
            db.info["delivery"] = delivery
        try:
            if intent.reduce_only:
                record_side = "SELL" if intent.pos_side == "long" else "BUY"
            else:
                record_side = "BUY" if intent.pos_side == "long" else "SELL"
            await self._record_execution(
                db, binding, lead, account, intent.inst_id, intent.inst_id,
                record_side, intent.pos_side.upper(), "RECONCILE_SOURCE", "CANCELED_ORDER_NOT_FOUND",
                0.0, 0.0, 0.0, details=(
                    f"Exchange confirms no such order for intent {intent.id} after grace window; "
                    "zero fills and no exchange order id; intent canceled so the delivery can re-drive"),
                complete_delivery=False)
        finally:
            if previous_delivery is None:
                db.info.pop("delivery", None)
            else:
                db.info["delivery"] = previous_delivery
        await db.commit()
        return True

    async def _cancel_resting(self, db, intent, adapter, spec):
        """撤销在簿限价单并以交易所查单结果记账；返回订单最新状态。"""
        try:
            await adapter.cancel_order(intent.inst_id, intent.id)
        except ExchangeAPIError:
            # 撤单被拒通常是订单刚好成交或已撤，真实结果以下面的查单为准。
            logger.info("Cancel of resting limit %s rejected; re-querying", intent.id)
        state = "new"
        for attempt in range(3):
            info = await adapter.get_order(intent.inst_id, cl_ord_id=intent.id)
            if info is not None:
                _, _, state = await self._apply_intent_info(db, intent, info, spec)
                if state in TERMINAL_STATES:
                    break
            if attempt < 2:
                await asyncio.sleep(0.1)
        return state

    async def _has_resting(self, db, binding_id, inst_id=None, pos_side=None):
        """该绑定（可限定合约与方向）是否有仍在簿的限价单。"""
        query = select(OrderIntent.id).where(OrderIntent.binding_id == binding_id, OrderIntent.state == "RESTING")
        if inst_id is not None:
            query = query.where(OrderIntent.inst_id == inst_id, OrderIntent.pos_side == pos_side)
        return await db.scalar(query.limit(1)) is not None

    async def _audit_completed_intents(self, db, account, instruments, relevant_positions=None):
        # 新成交已原子记录证据；旧数据只在证据完整后打完成标记。
        intents = (await db.scalars(select(OrderIntent).where(
            OrderIntent.account_id == account.id, OrderIntent.reporting_complete.is_(False),
            func.lower(OrderIntent.state).in_(TERMINAL_STATES),
        ).execution_options(populate_existing=True))).all()
        complete = True
        for intent in intents:
            if relevant_positions is not None and (intent.inst_id, intent.pos_side) not in relevant_positions:
                continue
            spec = instruments.get(intent.inst_id)
            proven = await self._recover_intent_reporting(db, intent, spec, require_notifications=True)
            # 保护性出场沿用旧的局部审计语义，但不能把省略通知的审计记为完整。
            if proven and (spec is not None or exact(intent.filled_qty) == 0):
                intent.reporting_complete = True
            elif not proven and relevant_positions is not None:
                proven = await self._recover_intent_reporting(db, intent, spec, require_notifications=False)
            complete &= proven
        await db.commit()
        return complete

    async def _recover_intent_reporting(self, db, intent, spec, require_notifications=True):
        """Repair demonstrable older reporting gaps from durable confirmed fills only.

        This never changes allocations or posts orders. Uncorrelatable historical
        records block admission instead of fabricating a fill or resending an order.
        """
        if intent.reduce_only:
            delivery = await db.get(BindingDelivery, (intent.binding_id, intent.delivery_event_id or intent.source_event_id))
            imported = (delivery is not None and delivery.state == "APPLIED" and
                        json.loads(delivery.payload_json).get("_imported_consumed_source"))
            if delivery is not None and delivery.exit_target_qty is None and not imported:
                delivery.state, delivery.exit_progress = "PENDING", "REVIEW_REQUIRED"
                await db.commit()
                return False
        cumulative = exact(intent.filled_qty)
        if cumulative <= 0:
            return True
        if not intent.exchange_order_id or exact(intent.avg_px) <= 0:
            return False
        records = (await db.scalars(select(ExecutionRecord).where(
            ExecutionRecord.binding_id == intent.binding_id,
            ExecutionRecord.account_id == intent.account_id,
            ExecutionRecord.inst_id == intent.inst_id,
            ExecutionRecord.position_side == intent.pos_side.upper(),
            ExecutionRecord.order_id == intent.exchange_order_id,
            ExecutionRecord.my_qty > 0))).all()
        tagged = [r for r in records if f"Intent accounting key {intent.id}" in r.details]
        if tagged:
            records = tagged
        else:
            others = await db.scalar(select(OrderIntent.id).where(
                OrderIntent.account_id == intent.account_id,
                OrderIntent.binding_id == intent.binding_id,
                OrderIntent.inst_id == intent.inst_id,
                OrderIntent.pos_side == intent.pos_side,
                OrderIntent.exchange_order_id == intent.exchange_order_id,
                OrderIntent.id != intent.id).limit(1))
            if others is not None:
                return False
        reported = sum((exact(r.my_qty_exact if r.my_qty_exact is not None else r.my_qty) for r in records), Decimal("0"))
        if reported == cumulative:
            # 账本已证实：通知回执只更新状态，不再阻断。通知缺口
            # （如历史回执缺失、回执字段不全）没有人工解除入口，
            # 绝不能因此永久停掉跟单；缺口仍留在记录详情与
            # notification_state 里可见，修复路径也不受影响。
            proven_notifications = await self._notification_evidence(db, records)
            if intent.state.lower() in TERMINAL_STATES:
                return True
            return proven_notifications
        if reported > cumulative or any(exact(r.my_price) <= 0 for r in records):
            return False
        cost = cumulative * exact(intent.avg_px) - sum(
            (exact(r.my_qty_exact if r.my_qty_exact is not None else r.my_qty) *
             exact(r.my_price_exact if r.my_price_exact is not None else r.my_price) for r in records), Decimal("0"))
        if cost <= 0:
            return False
        if spec is None:
            return False
        delta = cumulative - reported
        await self._record_intent_outcome(db, intent, spec, delta, cost / delta, intent.state.lower(), historical=True)
        return False

    @serialized_execution
    async def _notification_evidence(self, db, records):
        # 只读取本批执行记录关联的通知；仍校验 payload 的双重关联，防止错误匹配。
        ids = [record.id for record in records]
        outboxes = (await db.scalars(select(NotificationOutbox).where(or_(
            NotificationOutbox.execution_id.in_(ids),
            NotificationOutbox.event_key.in_([f"execution:{record_id}" for record_id in ids]),
        )))).all() if ids else []
        unresolved = False
        for record in records:
            evidence = []
            correlated = False
            for outbox in outboxes:
                try:
                    payload = json.loads(outbox.payload)
                    correlated |= (payload.get("execution_id") == record.id or
                                   payload.get("event_key") == f"execution:{record.id}")
                    if (type(payload.get("execution_id")) is int and payload["execution_id"] == record.id
                            and payload.get("event_key") == f"execution:{record.id}"
                            and isinstance(payload.get("channels"), list)
                            and isinstance(payload.get("delivered_channels"), list)):
                        evidence.append(outbox)
                except (ValueError, TypeError, AttributeError):
                    continue
            if len(evidence) == 1 and evidence[0].status in ("pending", "sent", "failed"):
                record.notification_state = evidence[0].status
            elif (not correlated and record.notification_state == "unsent" and
                  "Notification delivery unknown" not in record.details):
                config = await self._get_config(db, "notifications", DEFAULT_SYSTEM_CONFIG["notifications"])
                if config.get("notify_on_execution", True) and record.status in ("EXECUTED", "PARTIAL"):
                    account = await db.get(TradingAccount, record.account_id)
                    delivery = await db.get(BindingDelivery, (record.binding_id, record.delivery_event_id)) if record.delivery_event_id else None
                    source = json.loads(delivery.payload_json) if delivery is not None else None
                    # 带单员保证金余额用于把本笔折算到 1000U 本金。
                    source_lead = await db.get(LeadSource, record.lead_id)
                    rec_owner = getattr(account, "owner_user_id", None)
                    outbox = enqueue_notification(db, config, "MirrorQuant",
                        trade_message(record, account, source, getattr(source_lead, "margin_balance", None),
                                      lead=source_lead,
                                      display_unit=resolve_display_unit(config.get("display_unit"))),
                        event_type=record.status, execution_id=record.id, event_key=f"execution:{record.id}",
                        owner_user_id=rec_owner, lead_id=record.lead_id,
                        destination_type="user" if rec_owner else "system")
                    outboxes.append(outbox)
                    record.notification_state = "pending"
                else:
                    record.notification_state = "not_required"
            elif correlated or record.notification_state not in ("not_required", "sent"):
                record.notification_state = "unknown"
                marker = "Notification delivery unknown; human reconciliation required"
                if marker not in record.details:
                    record.details += "; " + marker
                unresolved = True
        await db.commit()
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
        spread_pct: Optional[float] = None,
        order_id: Optional[str] = None,
        details: str = "",
        notify_cfg: Optional[Dict[str, Any]] = None,
        complete_delivery: bool = True,
        notification_state: str = "not_required",
        limit: Optional[Dict[str, Any]] = None,
    ) -> ExecutionRecord:
        confirmed = db.info.pop("confirmed_execution", None)
        if (confirmed is not None and status in ("EXECUTED", "UNFILLED") and
                confirmed.binding_id == binding.id and confirmed.inst_id == inst_id and
                confirmed.order_id == order_id and confirmed.my_qty == my_qty and
                (my_qty == 0 or confirmed.my_price == my_price)):
            # The fill boundary already persisted this outcome and its notification.
            # Keep the durable remainder/state evidence alongside caller annotations.
            confirmed.action = action
            confirmed.details += "; " + redact_proxy_userinfo(details)
            await db.commit()
            return confirmed
        details = redact_proxy_userinfo(details)
        delivery = db.info.get("delivery")
        if delivery is not None and exact(my_qty) == 0:
            previous = await db.scalar(select(ExecutionRecord).where(
                ExecutionRecord.binding_id == binding.id, ExecutionRecord.account_id == account.id,
                ExecutionRecord.delivery_event_id == delivery.source_event_id,
                ExecutionRecord.inst_id == inst_id, ExecutionRecord.position_side == position_side,
                ExecutionRecord.status == status, ExecutionRecord.order_id == order_id,
                ExecutionRecord.details == details))
            if previous is not None:
                await db.commit()
                return previous
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
            notification_state=notification_state,
            lead_qty=float(lead_qty),
            lead_price=float(lead_price),
            lead_notional=float(lead_notional),
            my_qty=float(my_qty),
            my_price=float(my_price),
            my_qty_exact=format_exact(my_qty),
            my_price_exact=format_exact(my_price),
            my_notional=my_notional,
            slippage_pct=slippage_pct,
            spread_pct=spread_pct,
            order_id=order_id,
            delivery_event_id=delivery.source_event_id if delivery is not None else None,
            details=details,
        )
        db.add(rec)
        if (delivery is not None and delivery.exit_target_qty is None and complete_delivery and
                status not in ("FAILED", "UNKNOWN", "PARTIAL", "DEFERRED_SOURCE_UNPROVEN")):
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
                "myQty": float(my_qty),
                "myPrice": float(my_price),
                "myNotional": my_notional,
                "slippagePct": slippage_pct,
                "details": details,
                "createdAt": to_utc_iso(rec.created_at),
            },
        }
        if notify_cfg:
            owner_user_id = getattr(binding, "owner_user_id", None) or getattr(account, "owner_user_id", None)
            target_cfg = notify_cfg
            if owner_user_id:
                pref = await db.get(UserPreference, owner_user_id)
                if pref and pref.notifications_json:
                    try:
                        target_cfg = {**DEFAULT_SYSTEM_CONFIG["notifications"], **json.loads(pref.notifications_json)}
                    except (ValueError, TypeError):
                        target_cfg = notify_cfg
            should_notify = (
                # 限价单的挂出与失效同属执行结果通知。UNKNOWN 是未定结论，
                # 投递保持待处理、下一轮会再评估并推送确定结论，不单独推送。
                # 认领每个绑定基线期最多发生一次，值得单独通知一次。
                (status in ("EXECUTED", "UNFILLED", "PARTIAL", "LIMIT_PLACED", "LIMIT_EXPIRED",
                            "ADOPTED_POSITION")
                 and target_cfg.get("notify_on_execution", True))
                or (
                    _is_guard_outcome(status)
                    and target_cfg.get("notify_on_risk_block", True)
                )
            )
            if (should_notify and delivery is not None and _is_guard_outcome(status)
                    and exact(my_qty) == 0):
                # 同一投递事件已有已推送/待推送的风控结论：只记账，不再推送。
                # 回执失败（failed）的结论不在此列，重新评估仍可补推。
                already_notified = await db.scalar(select(ExecutionRecord.id).where(
                    ExecutionRecord.id != rec.id,
                    ExecutionRecord.binding_id == binding.id, ExecutionRecord.account_id == account.id,
                    ExecutionRecord.delivery_event_id == delivery.source_event_id,
                    ExecutionRecord.inst_id == inst_id, ExecutionRecord.position_side == position_side,
                    ExecutionRecord.notification_state.in_(("pending", "sent")),
                    or_(ExecutionRecord.status.startswith("BLOCKED"), ExecutionRecord.status.startswith("SKIPPED"),
                        ExecutionRecord.status.in_(_GUARD_NO_TRADE_STATUSES))))
                if already_notified is not None:
                    should_notify = False
            if should_notify:
                source = json.loads(delivery.payload_json) if delivery is not None else None
                unit = resolve_display_unit(target_cfg.get("display_unit"))
                msg = trade_message(rec, account, source, getattr(lead, "margin_balance", None), limit=limit,
                                    lead=lead, position=lead_position_context(db), display_unit=unit)
                merge_registry = db.info.get("merge_row_ids")
                dest_type = "user" if owner_user_id else "system"
                lead_ref_id = lead.id if lead else None
                if (merge_registry is not None and delivery is not None
                        and status != "ADOPTED_POSITION"):
                    # 记账行立即落盘（与执行记录同事务），只在发送时合并：
                    # 行上带分段文本与事件键，整批处理完后封口，drainer
                    # 按事件键分组一次发出，带单员部分只出现一次。
                    lead_text, follower_text = trade_sections(
                        rec, account, source, getattr(lead, "margin_balance", None),
                        limit=limit, lead=lead, position=lead_position_context(db), display_unit=unit)
                    row = enqueue_notification(db, target_cfg, "MirrorQuant", msg, event_type=status,
                                               execution_id=rec.id, event_key=f"execution:{rec.id}",
                                               merge_key=delivery.source_event_id,
                                               lead_section=lead_text, follower_section=follower_text,
                                               owner_user_id=owner_user_id,
                                               lead_id=lead_ref_id,
                                               destination_type=dest_type)
                    await db.flush()
                    merge_registry.setdefault(delivery.source_event_id, []).append(row.id)
                else:
                    event_key = f"execution:{rec.id}"
                    enqueue_notification(db, target_cfg, "MirrorQuant", msg, event_type=status,
                                         execution_id=rec.id, event_key=event_key,
                                         owner_user_id=owner_user_id,
                                         lead_id=lead_ref_id,
                                         destination_type=dest_type)
                rec.notification_state = "pending"
                if delivery is not None and delivery.source_event_id:
                    executed_users = db.info.setdefault("executed_source_users", {})
                    executed_users.setdefault(delivery.source_event_id, set()).add(owner_user_id)
        await db.commit()  # The execution and notification become visible together.
        await self._emit(payload)
        return rec

    async def _seal_merged_notifications(self, db: AsyncSession) -> None:
        """Seal this batch's mergeable notification rows so the drainer may send them grouped.

        Rows stay individually durable; sealing only marks the batch complete.
        A failed seal leaves rows unsealed and the drainer sends them grouped
        once they age past the seal grace period, so no notification is lost.
        """
        registry = db.info.pop("merge_row_ids", None)
        if not registry:
            return
        try:
            row_ids = [row_id for ids in registry.values() for row_id in ids]
            rows = (await db.scalars(select(NotificationOutbox).where(
                NotificationOutbox.id.in_(row_ids),
                NotificationOutbox.status == "pending"))).all()
            for row in rows:
                try:
                    payload = json.loads(row.payload)
                except (ValueError, TypeError):
                    continue
                if payload.get("merge_key") is None:
                    continue
                payload["sealed"] = True
                row.payload = json.dumps(payload, ensure_ascii=False)
            await db.commit()
        except Exception:
            # 封口失败不影响已落盘的执行结果与通知行；只留 warning，不写 traceback，
            # 避免链路监控把一次通知缺口当成系统异常。
            logger.warning("Merged notification seal failed; rows will send grouped after the seal grace period")
            await db.rollback()

    async def _dispatch_lead_activity_notifications(
        self,
        db: AsyncSession,
        lead: LeadSource,
        new_source_events: Dict[str, Dict[str, Any]],
        lead_margin_balance: Any,
        notify_cfg: Dict[str, Any],
    ) -> None:
        """Dispatch notifications triggered by lead activity (the lead's own trades).

        1. If the lead has its own notification config (lead.notification_config_json),
           send a lead-only notification to the lead's route/webhook.
        2. If users have subscriptions to this lead (UserLeadSubscription):
           - For subscribers who had an execution recorded for this event, their notification
             already includes lead + follower execution sections.
           - For subscribers who did NOT have an execution recorded, send a lead-only notification
             via their configured channels.
        3. If no executions occurred for any follower and no lead channel is configured,
           send a lead-only notification to the system notification route if notify_on_lead_order is enabled.
        """
        if not new_source_events:
            return

        executed_users_by_event = db.info.get("executed_source_users", {})
        merge_registry = db.info.setdefault("merge_row_ids", {})

        # 1. Lead's own notification configuration
        lead_cfg = None
        if lead.notification_config_json:
            try:
                lead_cfg = json.loads(lead.notification_config_json)
            except (ValueError, TypeError):
                lead_cfg = None

        has_lead_channel = (
            lead_cfg is not None
            and any(bool(lead_cfg.get(f"{ch}_enabled")) for ch in CHANNELS)
            and lead_cfg.get("notify_on_lead_order", True)
        )

        # 2. Query active user subscriptions for this lead
        subs = (await db.scalars(
            select(UserLeadSubscription).where(
                UserLeadSubscription.lead_id == lead.id,
                UserLeadSubscription.is_enabled.is_(True),
            )
        )).all()

        for event_id, source in new_source_events.items():
            executed_users = executed_users_by_event.get(event_id, set())

            # Destination A: Lead's own channel
            if has_lead_channel:
                unit = resolve_display_unit(lead_cfg.get("display_unit"))
                lead_text = lead_only_message(source, lead_margin_balance, lead=lead, display_unit=unit)
                row = enqueue_notification(
                    db, lead_cfg, "MirrorQuant", lead_text,
                    event_type="LEAD_ACTIVITY",
                    event_key=f"lead:{lead.id}:{event_id}",
                    merge_key=event_id,
                    lead_section=lead_text,
                    follower_section=None,
                    owner_user_id=None,
                    lead_id=lead.id,
                    destination_type="lead",
                )
                await db.flush()
                merge_registry.setdefault(event_id, []).append(row.id)

            # Destination B: User subscriptions
            for sub in subs:
                user_id = sub.user_id
                if user_id in executed_users:
                    # Follower execution was already recorded and enqueued for this user
                    continue

                pref = await db.get(UserPreference, user_id)
                user_cfg = dict(DEFAULT_SYSTEM_CONFIG["notifications"])
                if pref and pref.notifications_json:
                    try:
                        user_cfg.update(json.loads(pref.notifications_json))
                    except (ValueError, TypeError):
                        pass

                # Overlay channel-specific subscription overrides if present
                if sub.channels_json:
                    try:
                        sub_ch = json.loads(sub.channels_json)
                        if isinstance(sub_ch, list):
                            for ch in CHANNELS:
                                user_cfg[f"{ch}_enabled"] = (ch in sub_ch) and bool(user_cfg.get(f"{ch}_enabled"))
                        elif isinstance(sub_ch, dict):
                            for ch_k, ch_v in sub_ch.items():
                                if isinstance(ch_v, dict) and ch_k in user_cfg:
                                    user_cfg[ch_k] = {**user_cfg[ch_k], **ch_v}
                                else:
                                    user_cfg[ch_k] = ch_v
                    except (ValueError, TypeError):
                        pass

                has_user_channel = any(bool(user_cfg.get(f"{ch}_enabled")) for ch in CHANNELS)
                if not has_user_channel or not user_cfg.get("notify_on_lead_order", True):
                    continue

                unit = resolve_display_unit(user_cfg.get("display_unit"))
                lead_text = lead_only_message(source, lead_margin_balance, lead=lead, display_unit=unit)
                row = enqueue_notification(
                    db, user_cfg, "MirrorQuant", lead_text,
                    event_type="LEAD_ACTIVITY",
                    event_key=f"user_sub:{user_id}:{lead.id}:{event_id}",
                    merge_key=event_id,
                    lead_section=lead_text,
                    follower_section=None,
                    owner_user_id=user_id,
                    lead_id=lead.id,
                    destination_type="user",
                )
                await db.flush()
                merge_registry.setdefault(event_id, []).append(row.id)

    def _source_interval(self, order):
        start, end = event_timestamp(order.get("orderTime")), event_timestamp(order.get("orderUpdateTime"))
        return (start, end) if start is not None and end is not None else (0, 0)

    async def _validate_exit_source(self, db, binding, account, lead, payload, allocation,
                                    source_snapshot=None, saved_snapshot_required=False):
        await db.refresh(allocation)
        await db.refresh(binding)
        if (allocation.account_id != account.id or binding.account_id != account.id or
                allocation.binding_id != binding.id or binding.lead_id != lead.id):
            raise SourceExitUnproven("Source exit allocation scope changed")
        admission = db.info.get("snapshot_admission")
        await db.refresh(lead, ["snapshot_json"])
        try:
            saved = json.loads(lead.snapshot_json or "{}")
        except (ValueError, TypeError):
            raise SourceExitUnproven("Source exit observation is invalid")
        if saved_snapshot_required and not saved:
            raise SourceExitUnproven("Source exit persisted observation disappeared")
        snapshot = source_snapshot if source_snapshot is not None else (admission[1] if admission is not None else saved)
        observed = snapshot_observed_ms(snapshot)
        if not (is_routable_snapshot(snapshot) and snapshot_is_current(snapshot, saved)):
            raise SourceExitUnproven("Source exit observation is stale or incomparable")
        if observed is None or not 0 <= int(time.time() * 1000) - observed <= 30000:
            raise SourceExitUnproven("Source exit lacks a fresh observation")
        if "_closed" not in payload and source_batch_interval(payload, observed) is None:
            raise SourceExitUnproven("Source exit timestamps are inconsistent")
        delivery = db.info.get("delivery")
        if delivery is not None:
            if delivery.binding_id != binding.id or delivery.source_event_id != payload.get("_event_id"):
                raise SourceExitUnproven("Source exit delivery scope changed")
            original = payload.get("_closed") or payload
            await db.refresh(delivery)
            payload = json.loads(delivery.payload_json)
            source = payload.get("_closed") or payload
            if (source.get("symbol"), source.get("side"), source.get("positionSide")) != (
                    original.get("symbol"), original.get("side"), original.get("positionSide")):
                raise SourceExitUnproven("Source exit identity changed")
            if delivery.exit_target_qty is not None:
                await self._exit_delivery(db, binding, account, allocation, delivery.source_event_id)
        source = payload.get("_closed") or payload
        position_side = source.get("side") if "_closed" in payload else source.get("positionSide")
        if (position_side not in ("LONG", "SHORT") or allocation.pos_side != position_side.lower() or
                not source.get("symbol") or
                ("_closed" in payload and source.get("positionSide", position_side) != position_side) or
                ("_closed" not in payload and source.get("side") != ("SELL" if position_side == "LONG" else "BUY"))):
            raise SourceExitUnproven("Source exit leg identity is inconsistent")
        active = {f"{position['symbol']}_{position['positionSide']}":
                  exact(position.get("qtyExact", position.get("qty")) or 0)
                  for position in snapshot.get("openPositions") or []}
        if "_closed" in payload and active.get(f"{source['symbol']}_{position_side}", 0) > 0:
            raise SourceExitUnproven("Closed cycle conflicts with observed lead exposure")
        if "_closed" in payload:
            interval = closed_interval(payload["_closed"], observed)
        else:
            interval = source_batch_interval(payload, observed)
        opened = event_timestamp(allocation.opened_at_ms)
        if interval is None or (self._owned_qty(allocation) > 0 and
                (opened is None or ("_closed" in payload and not interval[0] <= opened < interval[1]) or
                 ("_closed" not in payload and interval[0] <= opened))):
            raise SourceExitUnproven("Source exit cycle or interval is unprovable")
        if delivery is not None:
            rows = (await db.scalars(select(BindingDelivery).where(
                BindingDelivery.binding_id == binding.id).order_by(
                BindingDelivery.source_sequence, BindingDelivery.source_event_id)
                .execution_options(populate_existing=True))).all()
            states = await self._position_ordering(db, binding, rows, snapshot, active)
            source = payload.get("_closed") or payload
            position = (source.get("symbol"), source.get("positionSide") or str(source.get("side") or "").upper())
            state = states.get(position)
            if state is not None and (state.review_reason or
                    (state.source_time_ms >= interval[1] and state.source_event_id != delivery.source_event_id)):
                delivery.exit_progress = "REVIEW_REQUIRED"
                await db.commit()
                raise SourceExitUnproven(state.review_reason or "Source exit is stale or unordered")
        return snapshot, saved_snapshot_required or bool(saved)

    def _source_timestamp(self, order):
        return max(self._source_interval(order))

    async def _position_ordering(self, db, binding, deliveries, snapshot, active, received_at_ms=None):
        grouped = {}
        closures = {}
        for delivery in deliveries:
            payload = json.loads(delivery.payload_json)
            if "_closed" in payload:
                closed = payload["_closed"]
                position = (closed.get("symbol"), str(closed.get("side") or "").upper())
                closures.setdefault(position, []).append((delivery, closed))
                grouped.setdefault(position, [])
                continue
            position = (payload["symbol"], payload["positionSide"])
            grouped.setdefault(position, []).append((delivery, payload))
        states = {}
        observed = snapshot_observed_ms(snapshot)
        source_bound = snapshot_source_bound_ms(snapshot, received_at_ms) or 0
        for position, rows in grouped.items():
            state = await db.get(BindingPositionProgress, (binding.id, *position), populate_existing=True)
            if state is None:
                state = BindingPositionProgress(binding_id=binding.id, symbol=position[0],
                    position_side=position[1], source_time_ms=0, source_event_id="", cycle_tracked=False)
                db.add(state)
                for delivery, payload in rows:
                    if delivery.state == "APPLIED":
                        interval = source_batch_interval(payload, source_bound)
                        if not _sequence_matches_interval(delivery.source_sequence, interval):
                            state.review_reason = "Historical applied source ordering is unprovable"
                            continue
                        if is_open_leg(payload.get("side"), payload.get("positionSide")):
                            state.cycle_tracked = True
                        timestamp = payload.get("_source_time_ms", payload.get("startTime")) or 0
                        if type(timestamp) is not int or timestamp <= 0:
                            state.review_reason = "Historical applied source ordering is unprovable"
                        elif timestamp > state.source_time_ms:
                            state.source_time_ms, state.source_event_id = timestamp, delivery.source_event_id
            else:
                # 审查结论是派生 verdict，每轮按当前证据重算：证据恢复后
                # 自动解除，证据仍坏则下面的检查会重新立起。只清结论，
                # 不动 source_time_ms 等持久化顺序水位。
                state.review_reason = None
            if state.review_reason:
                states[position] = state
                continue
            pending = [(delivery, payload) for delivery, payload in rows if delivery.state != "APPLIED"]
            intervals = []
            prior_by_source = {}
            for delivery, payload in sorted(rows, key=lambda row: row[0].source_sequence):
                interval = source_batch_interval(payload, source_bound)
                if not _sequence_matches_interval(delivery.source_sequence, interval):
                    state.review_reason = "Missing, malformed, reversed or future source ordering evidence"
                    continue
                source_key = payload.get("_source_key") or delivery.source_event_id.rsplit(":", 1)[0]
                end = payload.get("_source_time_ms", payload.get("endTime")) or 0
                start = payload.get("_source_start_ms", payload.get("startTime")) or 0
                previous_end = prior_by_source.get(source_key)
                if previous_end is not None:
                    start = previous_end
                    if type(end) is int and end <= previous_end:
                        state.review_reason = "Incremental source interval lacks strictly newer evidence"
                if (type(start) is not int or type(end) is not int or
                        start <= 0 or end < start or (source_bound is not None and end > source_bound)):
                    state.review_reason = "Missing, malformed or future source ordering evidence"
                    continue
                for other_key, other_start, other_end in intervals:
                    if source_key != other_key and max(start, other_start) <= min(end, other_end):
                        state.review_reason = "Overlapping source intervals cannot prove causal ordering"
                intervals.append((source_key, start, end))
                prior_by_source[source_key] = end
            if not state.review_reason:
                for delivery, payload in sorted(rows, key=lambda row: row[0].source_sequence):
                    if (delivery.state == "APPLIED" and payload.get("_imported_consumed_source") and
                            delivery.source_sequence > state.source_time_ms):
                        if state.source_remaining_qty is not None:
                            quantity = exact(payload.get("qtyExact", payload.get("qty")))
                            remaining = exact(state.source_remaining_qty) - quantity
                            if remaining < 0:
                                state.review_reason = "Consumed legacy source conflicts with persisted inventory"
                                break
                            state.source_remaining_qty = str(remaining)
                        state.source_time_ms = delivery.source_sequence
                        state.source_event_id = delivery.source_event_id
            for completed in deliveries:
                closed = json.loads(completed.payload_json).get("_closed")
                if (completed.state == "APPLIED" and closed and
                        (closed.get("symbol"), str(closed.get("side") or "").upper()) == position):
                    interval = closed_interval(closed, observed) if observed is not None else None
                    closed_at = interval[1] if interval is not None else 0
                    if closed_at > state.source_time_ms:
                        state.source_time_ms, state.source_event_id = closed_at, completed.source_event_id
                        state.source_remaining_qty = "0"
            known_closures = []
            for delivery, closed in closures.get(position, []):
                interval = closed_interval(closed, observed) if observed is not None else None
                if interval is None:
                    state.review_reason = "Missing, malformed, reversed or future closed-cycle evidence"
                    continue
                for other in known_closures:
                    if interval != other and max(interval[0], other[0]) <= min(interval[1], other[1]):
                        state.review_reason = "Overlapping closed cycles cannot prove causal ordering"
                known_closures.append(interval)
                for source_delivery, payload in rows:
                    start = payload.get("_source_start_ms", payload.get("startTime"))
                    end = payload.get("_source_time_ms", payload.get("endTime"))
                    source_interval = source_batch_interval(payload, source_bound)
                    if source_interval is None:
                        continue
                    if (source_interval[0] <= interval[1] <= source_interval[1] and
                            (source_interval[1] > interval[1] or
                             is_open_leg(payload.get("side"), payload.get("positionSide")))):
                        state.review_reason = "Source interval overlaps a closed-cycle boundary"
            timestamps = {}
            for delivery, payload in pending:
                timestamp = payload.get("_source_time_ms", payload.get("startTime")) or 0
                if (type(timestamp) is not int or timestamp <= 0 or
                        (source_bound is not None and timestamp > source_bound)):
                    state.review_reason = "Missing, malformed or future source ordering evidence"
                elif (timestamp <= state.source_time_ms and delivery.source_event_id != state.source_event_id):
                    state.review_reason = "Late or equal source event conflicts with persisted position ordering"
                timestamps.setdefault(timestamp, []).append(delivery.source_event_id)
            if any(len(event_ids) > 1 for event_ids in timestamps.values()):
                state.review_reason = "Equal timestamps cannot order distinct source events; order IDs are not causal evidence"
            if pending and state.source_remaining_qty is not None and not state.review_reason:
                predicted = exact(state.source_remaining_qty)
                for delivery, payload in pending:
                    if delivery.source_event_id == state.source_event_id:
                        continue
                    quantity = exact(payload.get("qtyExact", payload.get("qty")))
                    predicted += quantity if is_open_leg(payload.get("side"), payload.get("positionSide")) else -quantity
                if predicted < 0 or predicted != active.get(f"{position[0]}_{position[1]}", Decimal("0")):
                    state.review_reason = "Snapshot inventory conflicts with persisted source inventory; missing causal events or target"
            states[position] = state
        await db.commit()
        return states

    async def _reconcile_review_deliveries(self, db, binding, lead, account, adapter, instruments,
                                           deliveries, notify_cfg):
        """把已证明完成的待审查平仓投递转为 APPLIED，不再靠人工清。

        只处理同时满足四条的投递：①平仓事件（开仓与闭环事件绝不
        自动 reconciled）；②关联意图全部终态（或无意图）；③本绑定
        该合约所有权为零；④交易所当前也无该合约持仓（以本轮同步的
        positions_json 为准）。任一不满足就留在 REVIEW 里等人看。
        exit 账目按终态意图合计重建（无意图则全零空操作），每次转换
        都写审计记录注明依据；部分成交（filled < target）不转，
        避免把未完成的出场记成已完成。
        """
        try:
            stored_positions = json.loads(account.positions_json or "[]")
        except (ValueError, TypeError):
            stored_positions = []
        physical = {}
        for entry in stored_positions if isinstance(stored_positions, list) else []:
            if isinstance(entry, dict):
                physical[(str(entry.get("instId")), str(entry.get("posSide") or "").upper())] = exact(
                    entry.get("pos") or 0)
        for delivery in deliveries:
            if delivery.state != "PENDING" or delivery.exit_progress != "REVIEW_REQUIRED":
                continue
            try:
                payload = json.loads(delivery.payload_json)
            except (ValueError, TypeError):
                continue
            if "_closed" in payload:
                continue
            symbol = payload.get("symbol")
            position_side = str(payload.get("positionSide") or "").upper()
            if not symbol or position_side not in ("LONG", "SHORT"):
                continue
            if is_open_leg(payload.get("side"), payload.get("positionSide")):
                continue
            inst_id, supported = adapter.resolve_symbol(
                symbol, instruments, json.loads(binding.symbol_map_json or "{}"))
            if not supported:
                continue
            allocation = await db.scalar(select(PositionAllocation).where(
                PositionAllocation.binding_id == binding.id,
                PositionAllocation.account_id == account.id,
                PositionAllocation.inst_id == inst_id,
                PositionAllocation.pos_side == position_side.lower()))
            if allocation is not None and self._owned_qty(allocation) != 0:
                continue
            if physical.get((inst_id, position_side), Decimal("0")) != 0:
                continue
            attempts = (await db.scalars(select(OrderIntent).where(
                OrderIntent.binding_id == binding.id,
                or_(OrderIntent.source_event_id == delivery.source_event_id,
                    OrderIntent.delivery_event_id == delivery.source_event_id)))).all()
            if any(intent.state.lower() not in TERMINAL_STATES for intent in attempts):
                continue
            target = sum((exact(intent.requested_qty) for intent in attempts), Decimal("0"))
            filled = sum((exact(intent.filled_qty) for intent in attempts), Decimal("0"))
            if filled != target:
                continue
            delivery.exit_account_id = account.id
            delivery.exit_inst_id = inst_id
            delivery.exit_pos_side = position_side.lower()
            delivery.exit_allocation_id = allocation.id if allocation is not None else None
            delivery.exit_generation = allocation.generation if allocation is not None else 0
            delivery.exit_initial_owned_qty = format_exact(target)
            delivery.exit_target_qty = format_exact(target)
            delivery.exit_filled_qty = format_exact(filled)
            delivery.exit_remaining_qty = "0"
            delivery.exit_progress = "COMPLETE"
            delivery.state = "APPLIED"
            previous_delivery = db.info.get("delivery")
            db.info["delivery"] = delivery
            try:
                basis = (f"{len(attempts)} terminal intent(s), requested {format_exact(target)}, "
                         f"filled {format_exact(filled)}" if attempts else
                         "no intents; no position held, close was a no-op")
                await self._record_execution(
                    db, binding, lead, account, symbol, inst_id,
                    payload.get("side", ""), position_side, "RECONCILE_SOURCE", "RECONCILED_REVIEW",
                    float(exact(payload.get("qtyExact", payload.get("qty")) or 0)),
                    float(exact(payload.get("avgPrice") or 0)),
                    float(exact(payload.get("notionalExact", payload.get("notional")) or 0)),
                    my_qty=float(filled),
                    details=(f"Review delivery {delivery.source_event_id} reconciled from durable evidence: "
                             f"{basis}; allocation and exchange both flat"),
                    notify_cfg=notify_cfg, complete_delivery=False)
            finally:
                if previous_delivery is None:
                    db.info.pop("delivery", None)
                else:
                    db.info["delivery"] = previous_delivery
        await db.commit()

    async def _unpark_review_deliveries(self, db, binding, deliveries, ordering):
        """审查结论已解除的仓位，把未执行过的待审查投递放回待处理，
        让正常流程按因果顺序重新处理它们。

        只放行同时满足的投递：①所属仓位本轮重算后无审查结论；
        ②exit 目标从未算过（exit_target_qty 为空，即从未执行）；
        ③没有任何关联意图（有意图的投递走 reconciled 或重驱，
        绝不在此放行，避免重复下单）；④非闭环事件。
        放行只是清掉 REVIEW 标记，不下单、不改账；重处理时证据
        若仍坏，排序检查会再次立起结论并重新挂起。
        """
        for delivery in deliveries:
            if delivery.state != "PENDING" or delivery.exit_progress != "REVIEW_REQUIRED":
                continue
            if delivery.exit_target_qty is not None:
                continue
            try:
                payload = json.loads(delivery.payload_json)
            except (ValueError, TypeError):
                continue
            if "_closed" in payload:
                continue
            symbol = payload.get("symbol")
            position_side = str(payload.get("positionSide") or "").upper()
            if not symbol or position_side not in ("LONG", "SHORT"):
                continue
            state = ordering.get((symbol, payload.get("positionSide"))) or ordering.get(
                (symbol, position_side))
            if state is None or state.review_reason:
                continue
            attempts = await db.scalar(select(OrderIntent.id).where(
                OrderIntent.binding_id == binding.id,
                or_(OrderIntent.source_event_id == delivery.source_event_id,
                    OrderIntent.delivery_event_id == delivery.source_event_id)).limit(1))
            if attempts is not None:
                continue
            delivery.exit_progress = None
        await db.commit()

    @staticmethod
    async def _adoption_window_open(db: AsyncSession, binding: CopyBinding) -> bool:
        """自动认领只在接入窗口内跑：绑定还没有任何归属账目与投递记录。

        存量绑定（已有跟单账目或投递历史）不自动认领，改走手动端点；
        这样自动认领绝不会在已有跟单历史的绑定上多一次 adapter 调用，
        也不会在部署后悄悄改变存量绑定的账目。基线周期内即使带单员有
        订单也不会产生投递（订单记为基线），所以新绑定的接入窗口不会被
        首批订单关掉；认领若因余额读取失败而推迟，只要窗口未关就会重试。
        """
        has_allocation = await db.scalar(select(PositionAllocation.id).where(
            PositionAllocation.binding_id == binding.id).limit(1))
        if has_allocation is not None:
            return False
        has_delivery = await db.scalar(select(BindingDelivery.binding_id).where(
            BindingDelivery.binding_id == binding.id).limit(1))
        return has_delivery is None

    async def _adopt_matching_positions(
        self, db: AsyncSession, binding: CopyBinding, lead: LeadSource, account: TradingAccount,
        adapter: BaseExchangeAdapter, instruments: Dict[str, Any], snapshot: Dict[str, Any],
        lead_margin_balance: float, received_at_ms: int, notify_cfg: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """认领与带单员持仓一致的已有仓位：只认领未被任何绑定记账的余量。

        匹配口径与下单 sizing 一致：仓位名义金额 / 账户权益（敞口比率），
        相对差异在 adopt_tolerance_pct 内即视为同一仓位。认领只改账——
        把余量记为本绑定的归属量、成本按交易所平均开仓价——不下单；
        认领后带单员减仓/平仓会按归属量跟随处理。已有关联归属量的
        品种跳过，避免中途混账；余额取不到的候选品种留到下一轮重试，
        已认领的不重复认领。
        """
        if not binding.is_enabled or not account.is_enabled:
            return []
        try:
            lead_equity = exact(lead_margin_balance)
        except (ValueError, InvalidOperation, TypeError, AttributeError):
            return []
        if lead_equity <= 0:
            return []
        try:
            tolerance = exact(getattr(binding, "adopt_tolerance_pct", 10.0))
        except (ValueError, InvalidOperation, TypeError, AttributeError):
            tolerance = Decimal("10.0")
        symbol_map = json.loads(binding.symbol_map_json or "{}")
        whitelist = json.loads(binding.symbol_whitelist_json or "[]")
        blacklist = json.loads(binding.symbol_blacklist_json or "[]")
        positions = await adapter.get_positions()
        physical: Dict[tuple, ExchangePosition] = {}
        for position in positions:
            if position.pos > 0:
                physical[(position.inst_id, position.pos_side)] = position
        adopted: List[Dict[str, Any]] = []
        deferred = False
        for entry in snapshot.get("openPositions") or []:
            if not isinstance(entry, dict):
                continue
            symbol = entry.get("symbol")
            position_side = str(entry.get("positionSide") or "").upper()
            if not symbol or position_side not in ("LONG", "SHORT"):
                continue
            if not RiskEngine.check_symbol_filter(symbol, whitelist, blacklist).allowed:
                continue
            inst_id, supported = adapter.resolve_symbol(symbol, instruments, symbol_map)
            if not supported:
                continue
            local_pos_side = "long" if position_side == "LONG" else "short"
            mine = physical.get((inst_id, local_pos_side))
            if mine is None:
                continue
            existing = await db.scalar(select(PositionAllocation).where(
                PositionAllocation.binding_id == binding.id,
                PositionAllocation.inst_id == inst_id,
                PositionAllocation.pos_side == local_pos_side))
            if existing is not None and self._owned_qty(existing) > 0:
                continue
            others = (await db.scalars(select(PositionAllocation).where(
                PositionAllocation.account_id == account.id,
                PositionAllocation.inst_id == inst_id,
                PositionAllocation.pos_side == local_pos_side,
                PositionAllocation.binding_id != binding.id))).all()
            claimed = sum((self._owned_qty(other) for other in others), Decimal("0"))
            try:
                adoptable = exact(mine.pos) - claimed
                lead_qty = exact(entry.get("qtyExact", entry.get("qty")) or 0)
            except (ValueError, InvalidOperation, TypeError, AttributeError):
                continue
            if adoptable <= 0 or lead_qty <= 0:
                continue
            price = Decimal("0")
            for candidate in (mine.cur_px, entry.get("markPrice"), entry.get("currentPrice"),
                              entry.get("lastPrice"), entry.get("avgEntry"), mine.avg_px):
                try:
                    value = exact(candidate)
                except (ValueError, InvalidOperation, TypeError, AttributeError):
                    continue
                if value > 0:
                    price = value
                    break
            if price <= 0:
                continue
            try:
                balance = await get_collateral_balance(adapter, instruments.get(inst_id))
                my_equity = exact(balance.eq)
            except Exception:
                logger.warning("Adoption balance unavailable for %s; will retry", inst_id)
                deferred = True
                continue
            if my_equity <= 0:
                continue
            lead_ratio = lead_qty * price / lead_equity
            my_ratio = adoptable * price / my_equity
            if not _exposure_ratio_matches(lead_ratio, my_ratio, tolerance):
                continue
            try:
                entry_px = exact(mine.avg_px)
            except (ValueError, InvalidOperation, TypeError, AttributeError):
                entry_px = Decimal("0")
            if entry_px <= 0:
                entry_px = price
            entry_cost = adoptable * entry_px
            allocation = await self._get_allocation(db, binding.id, account.id, inst_id, local_pos_side)
            allocation.generation += 1
            allocation.opened_at_ms = received_at_ms
            await self._clear_guard(db, binding.id, inst_id, local_pos_side)
            allocation.entry_cost_exact = format_exact(entry_cost)
            allocation.entry_cost = float(allocation.entry_cost_exact)
            allocation.owned_qty_exact = format_exact(adoptable)
            allocation.owned_qty = float(allocation.owned_qty_exact)
            await self._upsert_guard(db, binding.id, inst_id, local_pos_side,
                                     float(entry_cost / adoptable))
            details = (f"Adopted existing position: my exposure {my_ratio:.2f}x, "
                       f"lead exposure {lead_ratio:.2f}x, tolerance {tolerance:.1f}%")
            await self._record_execution(
                db, binding, lead, account, symbol, inst_id,
                "BUY" if local_pos_side == "long" else "SELL", position_side,
                "ADOPT", "ADOPTED_POSITION",
                float(lead_qty), float(price), float(lead_qty * price),
                my_qty=float(adoptable), my_price=float(entry_px),
                my_notional=float(entry_cost), details=details, notify_cfg=notify_cfg)
            adopted.append({"symbol": symbol, "positionSide": position_side,
                            "qty": float(adoptable), "myRatio": float(my_ratio),
                            "leadRatio": float(lead_ratio)})
        if not deferred:
            binding.adoption_completed = True
        await db.commit()
        return adopted

    @serialized_execution
    async def process_lead_events(
        self, db: AsyncSession, lead: LeadSource, raw_orders: List[Dict[str, Any]],
        raw_closed: List[Dict[str, Any]], snapshot: Dict[str, Any], proxies: List[ProxyNode],
    ) -> None:
        """Enqueue cumulative order deltas or each independent fill once per follower."""
        received_at_ms = int(time.time() * 1000)
        if not is_routable_snapshot(snapshot):
            return
        await db.refresh(lead)
        try:
            latest = json.loads(lead.snapshot_json or "{}")
        except (ValueError, TypeError):
            return
        if not snapshot_is_current(snapshot, latest):
            return
        is_initial_sync = (lead.seen_order_keys_json is None)
        seen_keys = list(json.loads(lead.seen_order_keys_json or "[]"))
        seen_closed = list(json.loads(lead.seen_closed_ids_json or "[]"))
        old_seen = set(seen_keys)
        new_source_events: Dict[str, Dict[str, Any]] = {}
        expanded_orders = sorted([expand_slim_order(o) for o in raw_orders],
            key=lambda o: (self._source_timestamp(o), order_dedup_key(o)))
        bindings = (await db.scalars(select(CopyBinding).where(
            CopyBinding.lead_id == lead.id, CopyBinding.deleted_at.is_(None))
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
                    if previous not in (0, cumulative):
                        logger.error("Conflicting individual source fill %s; suspending source event", key)
                        continue
                    if previous > 0 and _source_notional_conflicts(
                            exact(progress.cumulative_notional), notional):
                        logger.error("Conflicting individual source fill %s; suspending source event", key)
                        continue
                    if previous > 0 and exact(progress.cumulative_notional) != notional:
                        # 源端均价的舍入抖动不构成冲突：采用最新名义金额，
                        # 让账本收敛，而不是每轮重复挂起同一事件。
                        progress.cumulative_notional = str(notional)
                elif cumulative < previous:
                    logger.error("Regressing source fill %s; suspending source event", key)
                    continue
                delta = cumulative - previous
                event_id = key + ":" + str(cumulative)
                if delta == 0 and previous > 0 and not is_open_leg(order.get("side"), order.get("positionSide")):
                    legacy_intent = await db.scalar(select(OrderIntent).where(
                        OrderIntent.binding_id == binding.id, OrderIntent.account_id == binding.account_id,
                        OrderIntent.source_event_id == event_id, OrderIntent.delivery_event_id.is_(None),
                        OrderIntent.reduce_only.is_(True)))
                    if (legacy_intent is not None and legacy_intent.state.lower() in TERMINAL_STATES and
                            0 < exact(legacy_intent.filled_qty) <= exact(legacy_intent.requested_qty) and
                            await db.get(BindingDelivery, (binding.id, event_id)) is None):
                        start, sequence = self._source_interval(order)
                        imported = group_orders_into_batches([{**order, "orderTime": start, "orderUpdateTime": sequence}])[0]
                        imported.update(_event_id=event_id, _source_key=key, _source_start_ms=start,
                            _source_time_ms=sequence, _imported_consumed_source=True)
                        db.add(BindingDelivery(binding_id=binding.id, source_event_id=event_id,
                            source_sequence=max(start, sequence), payload_json=json.dumps(imported), state="APPLIED"))
                if delta <= 0:
                    continue
                delta_notional = notional - exact(progress.cumulative_notional)
                if delta_notional <= 0:
                    continue
                start, sequence = self._source_interval(order)
                updated = {**order, "orderTime": start, "orderUpdateTime": sequence,
                    "executedQty": str(delta), "avgPrice": str(delta_notional / delta)}
                if previous > 0 and source_fill_kind(order) == "cumulative":
                    # 只有数量/名义金额有历史基线，累计收益不能冒充本次增量收益。
                    updated["totalPnl"], updated["pnlKnown"] = None, False
                batches = sorted(group_orders_into_batches([updated]),
                    key=lambda b: (b.get("startTime", 0), b.get("endTime", 0), b.get("key", "")))
                for batch in batches:
                    event_id = key + ":" + str(cumulative)
                    batch.update(qty=str(delta), qtyExact=str(delta), notional=str(delta_notional), notionalExact=str(delta_notional), avgPrice=str(delta_notional / delta), _event_id=event_id)
                    batch["_source_time_ms"] = sequence
                    batch["_source_start_ms"] = start
                    batch["_source_key"] = key
                    db.add(BindingDelivery(binding_id=binding.id, source_event_id=event_id,
                        source_sequence=max(start, sequence), payload_json=json.dumps(batch), state="PENDING"))
                    new_source_events[event_id] = batch
                progress.cumulative_qty, progress.cumulative_notional = str(cumulative), str(notional)
            # Historical closures cannot authorize closing a subsequently reopened cycle.
            if not baseline:
                for closed in raw_closed:
                    cid = str(closed.get("id") or closed.get("positionId") or "")
                    if cid and cid not in seen_closed:
                        event_id = "closed:" + cid
                        payload_closed = {"_closed": closed, "_event_id": event_id}
                        if await db.get(BindingDelivery, (binding.id, event_id)) is None:
                            db.add(BindingDelivery(binding_id=binding.id, source_event_id=event_id,
                                source_sequence=event_timestamp(closed.get("closed")) or 0,
                                payload_json=json.dumps(payload_closed), state="PENDING"))
                        new_source_events[event_id] = payload_closed
            binding.baseline_seeded = True
        if not is_initial_sync:
            for order in expanded_orders:
                key = order_dedup_key(order)
                if key not in old_seen:
                    try:
                        cumulative, price = validate_source_fill(order.get("executedQty"), order.get("avgPrice"))
                        event_id = key + ":" + str(cumulative)
                        if event_id not in new_source_events:
                            start, sequence = self._source_interval(order)
                            notional = cumulative * price
                            updated = {**order, "orderTime": start, "orderUpdateTime": sequence,
                                       "executedQty": str(cumulative), "avgPrice": str(price)}
                            batches = sorted(group_orders_into_batches([updated]),
                                             key=lambda b: (b.get("startTime", 0), b.get("endTime", 0), b.get("key", "")))
                            for batch in batches:
                                batch.update(qty=str(cumulative), qtyExact=str(cumulative),
                                             notional=str(notional), notionalExact=str(notional),
                                             avgPrice=str(price), _event_id=event_id,
                                             _source_time_ms=sequence, _source_start_ms=start, _source_key=key)
                                new_source_events[event_id] = batch
                    except Exception:
                        pass
            for closed in raw_closed:
                cid = str(closed.get("id") or closed.get("positionId") or "")
                if cid and cid not in seen_closed:
                    event_id = "closed:" + cid
                    if event_id not in new_source_events:
                        new_source_events[event_id] = {"_closed": closed, "_event_id": event_id}
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
        # 本批次产生的可合并通知行先登记，整批处理完后封口，drainer 再按事件分组发送。
        db.info["merge_row_ids"] = {}
        try:
            for binding in bindings:
                await db.refresh(binding)
                await db.refresh(lead)
                account = await db.get(TradingAccount, binding.account_id, populate_existing=True)
                if account is None:
                    continue
                try:
                    adapter = create_exchange_adapter(exchange=account.exchange, account_id=account.id,
                        credentials=self.vault.decrypt_dict(account.encrypted_credentials),
                        environment=account.environment,
                        proxy_url=ProxyManager.select_proxy_url(proxies, account.proxy_id, exchange=account.exchange),
                        margin_mode=account.margin_mode, position_mode=account.position_mode)
                    adapter._time_offset_ms = int(account.clock_offset_ms or 0)
                    await db.commit()
                    account.clock_offset_ms = await adapter.sync_time()
                    instruments = await adapter.get_instruments()
                    await self.reconcile_pending_orders(db, account, adapter, instruments)
                    if (binding.baseline_seeded and not binding.adoption_completed
                            and binding.adopt_existing_positions
                            and await self._adoption_window_open(db, binding)):
                        try:
                            await self._adopt_matching_positions(
                                db, binding, lead, account, adapter, instruments, snapshot,
                                lead_margin_balance, received_at_ms, notify_cfg)
                        except Exception:
                            logger.exception("Position adoption deferred for binding %s", binding.id)
                    completed = (await db.scalars(select(BindingDelivery).where(
                        BindingDelivery.binding_id == binding.id, BindingDelivery.state == "APPLIED"))).all()
                    for previous in completed:
                        closed = json.loads(previous.payload_json).get("_closed")
                        if not closed:
                            continue
                        symbol, pos_side = str(closed.get("symbol") or ""), str(closed.get("side") or "").lower()
                        if pos_side not in ("long", "short") or active.get(f"{symbol}_{pos_side.upper()}", 0) > 0:
                            continue
                        inst_id, supported = adapter.resolve_symbol(symbol, instruments, json.loads(binding.symbol_map_json or "{}"))
                        allocation = await db.scalar(select(PositionAllocation).where(
                            PositionAllocation.binding_id == binding.id, PositionAllocation.account_id == account.id,
                            PositionAllocation.inst_id == inst_id, PositionAllocation.pos_side == pos_side))
                        observed = snapshot_observed_ms(snapshot)
                        interval = closed_interval(closed, observed) if observed is not None else None
                        closed_at = interval[1] if interval is not None else 0
                        if (previous.exit_target_qty is None and allocation is not None and
                                self._owned_qty(allocation) > 0 and closed_at > allocation.opened_at_ms):
                            previous.state = "PENDING"
                    all_deliveries = (await db.scalars(select(BindingDelivery).where(
                        BindingDelivery.binding_id == binding.id)
                        .order_by(BindingDelivery.source_sequence, BindingDelivery.source_event_id))).all()
                    ordering = await self._position_ordering(db, binding, all_deliveries, snapshot, active, received_at_ms)
                    await self._reconcile_review_deliveries(
                        db, binding, lead, account, adapter, instruments, all_deliveries, notify_cfg)
                    await self._unpark_review_deliveries(db, binding, all_deliveries, ordering)
                    deliveries = [delivery for delivery in all_deliveries if delivery.state != "APPLIED"]
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
                        if not is_routable_snapshot(snapshot):
                            break
                        await db.commit()
                        batch = json.loads(delivery.payload_json)
                        source = batch.get("_closed") or batch
                        symbol_map = json.loads(binding.symbol_map_json or "{}")
                        inst_id, supported = adapter.resolve_symbol(source["symbol"], instruments, symbol_map)
                        source_position = (source.get("symbol"), source.get("positionSide") or str(source.get("side") or "").upper())
                        state = ordering.get(source_position)
                        if supported and state is not None and state.review_reason:
                            review_price = 0.0
                            if ("_closed" not in batch and is_open_leg(source.get("side"), source.get("positionSide"))
                                    and snapshot_observed_ms(snapshot) is None
                                    and source_batch_interval(batch, received_at_ms) is not None):
                                try:
                                    book = await adapter.get_orderbook(inst_id, depth=5)
                                    levels = book.asks if source.get("positionSide") == "LONG" else book.bids
                                    price = float(levels[0][0]) if levels else 0.0
                                    if math.isfinite(price) and price > 0:
                                        review_price = price
                                except Exception:
                                    review_price = 0.0
                            delivery.exit_progress = "REVIEW_REQUIRED"
                            db.info["delivery"] = delivery
                            await self._record_execution(db, binding, lead, account, source["symbol"], source["symbol"],
                                source.get("side", ""), source.get("positionSide", ""), "RECONCILE_SOURCE", "BLOCKED_SOURCE_ORDERING",
                                source.get("qty", 0), source.get("avgPrice", 0), source.get("notional", 0),
                                my_price=review_price,
                                details=state.review_reason + "; human reconciliation required", notify_cfg=notify_cfg,
                                complete_delivery=False)
                            db.info.pop("delivery", None)
                            continue
                        if delivery.exit_target_qty is not None and exact(delivery.exit_remaining_qty) > 0:
                            later = [candidate for candidate in deliveries if candidate.source_sequence > delivery.source_sequence
                                     and (json.loads(candidate.payload_json).get("symbol"),
                                          json.loads(candidate.payload_json).get("positionSide")) ==
                                     (source.get("symbol"), source.get("positionSide"))]
                            if not later and state is not None and state.cycle_tracked:
                                attempts = (await db.scalars(select(OrderIntent).where(
                                    OrderIntent.binding_id == binding.id,
                                    or_(OrderIntent.source_event_id == delivery.source_event_id,
                                        OrderIntent.delivery_event_id == delivery.source_event_id)))).all()
                                source_retry = (delivery.exit_progress == "SOURCE_UNPROVEN" and
                                    exact(delivery.exit_filled_qty) == 0 and attempts and
                                    all(intent.reduce_only and intent.state.lower() == "rejected" and
                                        intent.exchange_order_id is None and exact(intent.filled_qty) == 0
                                        for intent in attempts))
                                # 明确未提交的零成交延期可重试，已成交余量仍等待新的源事件。
                                if (attempts and not source_retry and
                                        all(intent.state.lower() in TERMINAL_STATES for intent in attempts)):
                                    delivery.exit_progress = "WAITING_SOURCE"
                                    await db.commit()
                                    break
                            elif delivery.exit_progress == "WAITING_SOURCE":
                                successor = later[-1]
                                successor_payload = json.loads(successor.payload_json)
                                if (not is_open_leg(successor_payload.get("side"), successor_payload.get("positionSide"))
                                        and remaining_by_event.get(successor.source_event_id) == 0):
                                    delivery.state, delivery.exit_progress = "APPLIED", "SUPERSEDED"
                                    delivery.exit_superseded_by = successor.source_event_id
                                    await db.commit()
                                    continue
                        if supported and state is not None and "_closed" not in batch:
                            state.source_time_ms = batch.get("_source_time_ms", batch.get("startTime"))
                            state.source_event_id = delivery.source_event_id
                            state.source_remaining_qty = str(remaining_by_event[delivery.source_event_id])
                            if is_open_leg(batch.get("side"), batch.get("positionSide")):
                                state.cycle_tracked = True
                            await db.commit()
                        try:
                            bal = await get_collateral_balance(adapter, instruments.get(inst_id))
                        except Exception as exc:
                            account.status, account.last_error = "error", redact_proxy_userinfo(str(exc))[:400]
                            position_side = source.get("positionSide") or str(source.get("side", "")).upper()
                            side = source.get("side") if "_closed" not in batch else ("SELL" if position_side == "LONG" else "BUY")
                            delivery.state = "PENDING"
                            db.info["delivery"] = delivery
                            await self._record_execution(
                                db, binding, lead, account, source["symbol"], inst_id, side, position_side,
                                direction_label(side, position_side, not is_open_leg(side, position_side)),
                                "BLOCKED_COLLATERAL_BALANCE", source.get("qty", 0), source.get("avgPrice", 0),
                                source.get("notional", 0), details=account.last_error, notify_cfg=notify_cfg,
                                complete_delivery=False,
                            )
                            db.info.pop("delivery", None)
                            break
                        positions = await adapter.get_positions()
                        await persist_equity(db, account, bal)
                        account.total_notional = sum(p.notional_usd for p in positions)
                        account.positions_json = json.dumps([p.to_dict() for p in positions])
                        account.status, account.last_error = "connected", None
                        batch["_freshness_at_ms"] = min(snapshot_times(snapshot).values())
                        observed = snapshot_observed_ms(snapshot)
                        if observed is not None:
                            batch["_observed_at_ms"] = observed
                        batch["_lead_remaining_qty"] = remaining_by_event.get(delivery.source_event_id, 0)
                        # Historical openings inside a known completed lead cycle must not
                        # attach to the same symbol's currently reopened position.
                        if "_closed" not in batch and is_open_leg(batch.get("side"), batch.get("positionSide")):
                            timestamp = int(batch.get("startTime") or 0)
                            for closed in raw_closed:
                                interval = closed_interval(closed, observed) if observed is not None else None
                                if (closed.get("symbol") == batch.get("symbol") and
                                    str(closed.get("side") or "").upper() == batch.get("positionSide") and
                                    interval is not None and interval[0] <= timestamp <= interval[1]):
                                    batch["_closed_cycle"] = True
                        db.info["delivery"] = delivery
                        db.info["snapshot_admission"] = (lead, snapshot)
                        if "_closed" in batch:
                            c = batch["_closed"]
                            if active.get(f"{c.get('symbol')}_{str(c.get('side', '')).upper()}", 0) <= 0:
                                complete = await self._reconcile_closed_position(db, binding, lead, account, adapter, c, positions, instruments, notify_cfg)
                                delivery.state = "APPLIED" if complete else "PENDING"
                            else:
                                delivery.state = "APPLIED"
                        else:
                            await self._execute_batch_for_binding(db, binding, lead, account, adapter,
                                batch, lead_margin_balance, active, bal.eq, positions, instruments,
                                bool(await self._get_config(db, "global_kill_switch", False)), notify_cfg, resilience_cfg)
                        db.info.pop("delivery", None)
                        db.info.pop("snapshot_admission", None)
                        await db.commit()
                        if not await self.reconcile_pending_orders(db, account, adapter, instruments):
                            break
                        if delivery.state != "APPLIED":
                            break
                except Exception as exc:
                    db.info.pop("delivery", None)
                    db.info.pop("snapshot_admission", None)
                    logger.exception("Follower delivery deferred")
                    if isinstance(exc, SQLAlchemyError) or not db.is_active:
                        await db.rollback()
                        await db.refresh(account)
                        await db.refresh(lead)
                        for stale_binding in bindings:
                            await db.refresh(stale_binding)
                    account.status, account.last_error = "error", redact_proxy_userinfo(str(exc))[:400]
                    await db.commit()
        finally:
            try:
                await self._dispatch_lead_activity_notifications(
                    db, lead, new_source_events, lead_margin_balance, notify_cfg
                )
            except Exception:
                logger.exception("Failed to dispatch lead activity notifications for lead %s", getattr(lead, "id", None))
            await self._seal_merged_notifications(db)

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
            if await self._has_resting(db, binding.id, inst_id, local_pos_side):
                # 带单员已在减仓/平仓：先撤掉本绑定同方向未成交的限价单，避免平仓后又被动开仓。
                await self.reconcile_pending_orders(db, account, adapter, instruments,
                    {(inst_id, local_pos_side)}, cancel_resting_for=binding.id)
            source_time = event_timestamp(batch.get("_source_start_ms", batch.get("startTime")))
            if source_time and source_time < allocation.opened_at_ms:
                await self._record_execution(db, binding, lead, account, symbol, inst_id, side, ps, action,
                    "SKIPPED_CLOSED_CYCLE", lead_qty, lead_avg_px, lead_notional,
                    details="Historical reduction predates this allocation's opening cycle", notify_cfg=notify_cfg)
                return
            lead_rem_qty = batch.get("_lead_remaining_qty", active_lead_positions.get(f"{symbol}_{ps}", 0.0))
            is_full_close = exact(lead_rem_qty) <= 0
            owned = self._owned_qty(allocation)
            close_sz = owned if is_full_close else owned * lead_qty / (lead_qty + exact(lead_rem_qty))
            try:
                source_snapshot, saved_snapshot_required = await self._validate_exit_source(
                    db, binding, account, lead, batch, allocation)
                delivery = await self._exit_delivery(db, binding, account, allocation, event_id, close_sz)
                if delivery is not None:
                    close_sz = exact(delivery.exit_remaining_qty)
                if close_sz <= 0:
                    if owned <= 0:
                        await self._record_execution(db, binding, lead, account, symbol, inst_id, side, ps, action,
                            "SKIPPED_NO_LOCAL_POS", lead_qty, lead_avg_px, lead_notional,
                            details="No binding-owned position to reduce", notify_cfg=notify_cfg)
                    return
                if local_pos is None:
                    if delivery is not None:
                        dust = exact(delivery.exit_remaining_qty)
                        dust_min = exact(spec.market_min_sz or spec.min_sz)
                        if 0 < dust < dust_min:
                            # 交易所已无仓位且剩余只是零头：核销完成，不再空等。
                            await self._absorb_exit_remainder(
                                db, binding, lead, account, delivery, symbol, inst_id, side, ps, action,
                                lead_qty, lead_avg_px, lead_notional, dust, dust_min)
                            return
                        delivery.exit_progress = "WAITING_HOLDINGS"
                    await self._record_execution(db, binding, lead, account, symbol, inst_id, side, ps, action,
                        "SKIPPED_NO_LOCAL_POS", lead_qty, lead_avg_px, lead_notional,
                        details="Binding-owned position absent from exchange holdings", notify_cfg=notify_cfg,
                        complete_delivery=False)
                    return
                await self._close_owned(db, binding, lead, account, adapter, local_pos, spec,
                    close_sz, event_id, "FULL_CLOSE" if is_full_close else "PARTIAL_REDUCE", notify_cfg,
                    lead_qty=lead_qty, lead_price=lead_avg_px, lead_notional=lead_notional,
                    source_evidence=(lead, batch, allocation, source_snapshot, saved_snapshot_required))
            except Exception as exc:
                if owned <= 0 and isinstance(exc, UnknownOrderOutcome):
                    await self._record_execution(db, binding, lead, account, symbol, inst_id, side, ps, action,
                        "SKIPPED_NO_LOCAL_POS", lead_qty, lead_avg_px, lead_notional,
                        details=f"No binding-owned position to reduce; source evidence unavailable: {exc}",
                        notify_cfg=notify_cfg, complete_delivery=False)
                elif isinstance(exc, SourceExitUnproven):
                    # 保留投递与失败计数，下一份新鲜快照可以重试。
                    await self._record_execution(db, binding, lead, account, symbol, inst_id, side, ps, action,
                        "DEFERRED_SOURCE_UNPROVEN", lead_qty, lead_avg_px, lead_notional,
                        details=str(exc), notify_cfg=notify_cfg, complete_delivery=False)
                else:
                    await self._handle_order_failure(db, binding, lead, account, symbol, inst_id, side, ps, action,
                        lead_qty, lead_avg_px, lead_notional, f"Close order error: {exc}", notify_cfg, resilience_cfg,
                        status="REJECTED" if isinstance(exc, OrderRejected) else "FAILED")
            return

        # CASE B: OPEN / ADD
        if global_kill or not binding.is_enabled or not account.is_enabled:
            await self._record_execution(
                db, binding, lead, account, symbol, inst_id, side, ps, action,
                "BLOCKED_KILL_SWITCH", lead_qty, lead_avg_px, lead_notional,
                # 记录所有实际阻断原因，避免把账户停用误报为全局急停。
                details="; ".join(reason for active, reason in (
                    (global_kill, "Global kill switch is active"),
                    (not binding.is_enabled, "Binding is disabled"),
                    (not account.is_enabled, "Account is disabled")) if active),
                notify_cfg=notify_cfg,
            )
            return

        if not await self.reconcile_pending_orders(db, account, adapter, instruments):
            delivery = db.info.get("delivery")
            if delivery is not None:
                reported = await db.scalar(select(ExecutionRecord.id).where(
                    ExecutionRecord.binding_id == binding.id, ExecutionRecord.account_id == account.id,
                    ExecutionRecord.delivery_event_id == delivery.source_event_id,
                    ExecutionRecord.inst_id == inst_id, ExecutionRecord.position_side == ps,
                    ExecutionRecord.status.in_(("PARTIAL", "FAILED", "UNKNOWN"))).limit(1))
                if reported is not None:
                    return
            await self._record_execution(db, binding, lead, account, symbol, inst_id, side, ps, action,
                "UNKNOWN", lead_qty, lead_avg_px, lead_notional,
                details="Account has unresolved orders; increases suspended", notify_cfg=notify_cfg)
            return

        # 加仓限价单允许叠加：每笔加仓按带单员的加仓量独立计算数量、独立挂单，
        # 多笔都成交时合计正好等于带单员的合计加仓量。逐笔保证金仍按现有逻辑
        # 检查，成交时交易所强制保证金。不要在这里用 _has_resting 拦截——
        # 2026-11-21 实测：熬鹰 ZEC 三笔加仓，第二笔挂单后第三笔被挡，
        # 用户明确要求多笔限价单可以同时在簿。
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

        RiskEngine.clear_disabled_loss_breaker(binding)
        if binding.circuit_breaker_tripped:
            await self._record_execution(
                db, binding, lead, account, symbol, inst_id, side, ps, action,
                "BLOCKED_DRAWDOWN", lead_qty, lead_avg_px, lead_notional,
                details=binding.circuit_breaker_reason, notify_cfg=notify_cfg,
            )
            return
        try:
            hwm, day_start = equity_baseline(account, spec.settle_ccy)
            available_collateral = collateral_available(account, spec.settle_ccy)
            lead_quote = next((currency for currency in ("USDT", "USDC") if symbol.endswith(currency)), None)
            if lead_quote != spec.quote_ccy or spec.quote_ccy != spec.settle_ccy:
                raise ValueError("Source and follower price currencies differ; reliable conversion unavailable")
            if getattr(binding, "enable_leverage_cap", False) and any(
                instruments.get(position.inst_id) is None or
                instruments[position.inst_id].settle_ccy != spec.settle_ccy
                for position in positions if position.pos > 0
            ):
                raise ValueError("Portfolio leverage currencies differ; reliable conversion unavailable")
        except ValueError as exc:
            await self._record_execution(db, binding, lead, account, symbol, inst_id, side, ps, action,
                "BLOCKED_COLLATERAL_BALANCE", lead_qty, lead_avg_px, lead_notional,
                details=str(exc), notify_cfg=notify_cfg)
            return
        for result in RiskEngine.loss_breaker_checks(binding, hwm, day_start, my_equity):
            if not result.allowed:
                binding.circuit_breaker_tripped = True
                binding.circuit_breaker_reason = result.reason
                await self._record_execution(
                    db, binding, lead, account, symbol, inst_id, side, ps, action,
                    result.code, lead_qty, lead_avg_px, lead_notional,
                    details=result.reason, notify_cfg=notify_cfg,
                )
                return

        await db.commit()
        try:
            book = await adapter.get_orderbook(inst_id, depth=5)
        except Exception:
            await self._handle_order_failure(db, binding, lead, account, symbol, inst_id, side, ps, action,
                lead_qty, lead_avg_px, lead_notional, "Market quote unavailable; no order submitted",
                notify_cfg, resilience_cfg)
            return
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
            order_side=order_side,
        )
        # 滑点超限处理为“挂限价单”时，只对追价方向（买入现价更高 / 卖出现价更低）的偏离改挂单；
        # 有利方向的偏离（买入现价更低 / 卖出现价更高）不是滑点，直接走市价/IOC。
        # 行情缺失、事件过旧等仍按原规则拦截。
        adverse = ref_px > 0 and lead_avg_px > 0 and (
            ref_px > lead_avg_px if order_side == "buy" else ref_px < lead_avg_px)
        limit_mode = getattr(binding, "slippage_action", "block") == "limit" and adverse
        use_limit = limit_mode and not catchup_res.allowed and catchup_res.code == "BLOCKED_CATCHUP_DRIFT"
        if not catchup_res.allowed and not use_limit:
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
                my_price=ref_px, slippage_pct=spread_res.value, spread_pct=spread_res.value or None,
                details=spread_res.reason or "Spread guard triggered", notify_cfg=notify_cfg,
            )
            return

        slip_res = RiskEngine.check_slippage(ref_px, lead_avg_px, binding.max_slippage_pct, order_side=order_side)
        use_limit = use_limit or (limit_mode and not slip_res.allowed)
        if not slip_res.allowed and not use_limit:
            await self._record_execution(
                db, binding, lead, account, symbol, inst_id, side, ps, action,
                "BLOCKED_SLIPPAGE", lead_qty, lead_avg_px, lead_notional,
                my_price=ref_px, slippage_pct=slip_res.value, spread_pct=spread_res.value or None,
                details=slip_res.reason or "Slippage limit exceeded", notify_cfg=notify_cfg,
            )
            return

        entry_px = ref_px
        limit_px_dec = None
        if use_limit:
            # 限价 = 带单均价按“允许的最大偏离”外扩一档：买单向下取整到最小价位，卖单向上取整。
            allowed = min(exact(binding.max_slippage_pct),
                          exact(resilience_cfg.get("catchup_max_price_dev_pct", 0.5))) / Decimal("100")
            if order_side == "buy":
                limit_px_dec = quantize_down(exact(lead_avg_px) * (1 + allowed), spec.tick_sz)
            else:
                limit_px_dec = (exact(lead_avg_px) * (1 - allowed) / spec.tick_sz).to_integral_value(
                    rounding=ROUND_UP) * spec.tick_sz
            # 仓位、杠杆、保证金都按限价（最差成交价）测算。
            entry_px = float(limit_px_dec)

        target_usd = PositionSizer.calculate_target_usd(
            follower_equity=my_equity,
            lead_margin_balance=lead_margin_balance,
            risk_scale=binding.risk_scale,
            lead_notional=lead_notional,
            pending_debt_usd=self._pending_debt(debt),
        )

        sized = PositionSizer.size_open_order(target_usd, entry_px, spec)
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

        leverage_cap_enabled = getattr(binding, "enable_leverage_cap", False)
        capped_qty, cap_reason = sized.contracts, None
        if leverage_cap_enabled:
            capped_qty, cap_reason = RiskEngine.cap_by_leverage(
                requested_qty=sized.contracts,
                ref_price=entry_px,
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

        projected_notional = sum(abs(exact(p.notional_usd)) for p in positions if p.inst_id == inst_id) + capped_qty * exact(entry_px) * spec.ct_val
        try:
            tier_max = await adapter.get_max_leverage(inst_id, projected_notional)
        except Exception as exc:
            # 合约档位失败只影响当前投递。
            await self._handle_order_failure(db, binding, lead, account, symbol, inst_id, side, ps, action,
                lead_qty, lead_avg_px, lead_notional, f"Leverage tier unavailable: {exc}", notify_cfg, resilience_cfg)
            return
        max_allowed_lvr = int(min(spec.max_lvr, tier_max))
        if leverage_cap_enabled:
            max_allowed_lvr = min(max_allowed_lvr, binding.max_instrument_leverage)
        target_lvr = _effective_open_leverage(
            local_pos.lever if local_pos is not None and local_pos.pos > 0 else None,
            target_notional=capped_qty * exact(entry_px) * spec.ct_val,
            equity=my_equity, max_symbol_leverage=max_allowed_lvr,
        )
        margin_policy = str(resilience_cfg.get("insufficient_margin_policy", "auto_scale_down"))
        avail_eq = available_collateral
        capped_qty, margin_err, margin_note = RiskEngine.adjust_for_available_margin(
            requested_qty=capped_qty,
            ref_price=entry_px,
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
        ord_type, order_px, expires_at_ms = "ioc", ioc_px_dec, None
        limit_ttl_sec = int(getattr(binding, "limit_order_ttl_sec", 120) or 120)
        if use_limit:
            ord_type, order_px = "limit", limit_px_dec
            expires_at_ms = int(time.time() * 1000) + limit_ttl_sec * 1000
        try:
            # 杠杆确认和本地过滤器错误纳入本次投递，不中断整个账户。
            confirmed = await adapter.ensure_leverage(
                inst_id, target_lvr, pos_side=local_pos_side, mgn_mode=account.margin_mode)
            if confirmed is not True:
                raise RuntimeError("Exchange leverage could not be confirmed")
            try:
                spec.validate_order(ord_type, capped_qty, order_px)
            except (ValueError, ArithmeticError) as exc:
                raise OrderRejected(account.exchange, "LOCAL_VALIDATION", str(exc)) from exc
            res = await self._submit_confirmed(db, binding, account, adapter, spec,
                source_event_id=event_id, target_usd=target_usd,
                source_time_ms=int(batch.get("startTime") or 0),
                observed_at_ms=batch.get("_freshness_at_ms", batch.get("_observed_at_ms", 0)), inst_id=inst_id,
                side=order_side, pos_side=local_pos_side, ord_type=ord_type,
                sz=format_decimal(capped_qty, spec.lot_sz), px=format_decimal(order_px, spec.tick_sz),
                reduce_only=False, td_mode=account.margin_mode, expires_at_ms=expires_at_ms,
                spread_pct=spread_res.value or None, sizing_px=entry_px, retry_terminal=True)
            fill_sz, fill_px = res.fill_sz, res.avg_px
            if res.state == "resting":
                # 限价单已挂出但未全部成交：单独记录并通知，成交或失效时再各通知一次。
                binding.consecutive_failures = 0
                resting = await db.get(OrderIntent, res.cl_ord_id)
                await self._record_execution(db, binding, lead, account, symbol, inst_id, side, ps, action,
                    "LIMIT_PLACED", lead_qty, lead_avg_px, lead_notional,
                    my_price=float(limit_px_dec), slippage_pct=slip_res.value, spread_pct=spread_res.value or None, order_id=res.ord_id,
                    details=f"Price drift {slip_res.value}% over limit; resting limit "
                        f"{format_decimal(limit_px_dec, spec.tick_sz)} x {format_decimal(capped_qty, spec.lot_sz)}, "
                        f"auto-cancel after {limit_ttl_sec}s",
                    notify_cfg=notify_cfg,
                    limit={"px": resting.limit_px, "requested": resting.requested_qty,
                           "filled": resting.filled_qty, "ttl_sec": limit_ttl_sec})
            elif fill_sz > 0:
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
                lead_qty, lead_avg_px, lead_notional, f"Order execution failed: {exc}", notify_cfg, resilience_cfg,
                status="REJECTED" if isinstance(exc, OrderRejected) else "FAILED")

    @serialized_execution
    async def _close_owned(self, db, binding, lead, account, adapter, pos, spec, quantity,
                           source_event_id, action, notify_cfg, lead_qty=0.0, lead_price=0.0, lead_notional=0.0,
                           source_evidence=None):
        if source_evidence is not None:
            source = source_evidence[1].get("_closed") or source_evidence[1]
            mapped, supported = adapter.resolve_symbol(source.get("symbol"), {spec.inst_id: spec},
                json.loads(binding.symbol_map_json or "{}"))
            allocation = source_evidence[2]
            if not supported or (mapped, allocation.inst_id, allocation.pos_side) != (pos.inst_id, pos.inst_id, pos.pos_side):
                raise SourceExitUnproven("Source exit instrument or allocation leg changed")
            await self._validate_exit_source(db, binding, account, *source_evidence)
        unresolved = (await db.scalars(select(BindingPositionProgress).where(
            BindingPositionProgress.binding_id == binding.id,
            BindingPositionProgress.position_side == pos.pos_side.upper(),
            BindingPositionProgress.review_reason.is_not(None)))).all()
        for state in unresolved:
            mapped, supported = adapter.resolve_symbol(state.symbol, {spec.inst_id: spec}, json.loads(binding.symbol_map_json or "{}"))
            if supported and mapped == pos.inst_id:
                await self._record_execution(db, binding, lead, account, state.symbol, pos.inst_id,
                    "SELL" if pos.pos_side == "long" else "BUY", pos.pos_side.upper(), action,
                    "BLOCKED_SOURCE_ORDERING", lead_qty, lead_price, lead_notional,
                    details=state.review_reason + "; human reconciliation required", notify_cfg=notify_cfg,
                    complete_delivery=False)
                return False
        allocation = await self._get_allocation(db, binding.id, account.id, pos.inst_id, pos.pos_side)
        delivery = await self._exit_delivery(db, binding, account, allocation, source_event_id, quantity)
        if delivery is None and self._owned_qty(allocation) == 0:
            return False
        if delivery is not None:
            quantity = exact(delivery.exit_remaining_qty)
            if quantity == 0:
                return False
        if not await self.reconcile_pending_orders(db, account, adapter, {spec.inst_id: spec},
                                                   {(pos.inst_id, pos.pos_side)}, cancel_resting_for=binding.id):
            if delivery is not None:
                delivery.exit_progress = "WAITING_ORDER"
                await db.commit()
            return False
        if delivery is not None:
            quantity = exact(delivery.exit_remaining_qty)
        other_allocations = (await db.scalars(select(PositionAllocation).where(
            PositionAllocation.account_id == account.id,
            PositionAllocation.inst_id == pos.inst_id,
            PositionAllocation.pos_side == pos.pos_side,
            PositionAllocation.binding_id != binding.id))).all()
        reserved = sum((self._owned_qty(a) for a in other_allocations), Decimal("0"))
        if exact(pos.pos) < reserved + self._owned_qty(allocation):
            if delivery is None or delivery.exit_progress != "WAITING_HOLDINGS":
                if delivery is not None:
                    delivery.exit_progress = "WAITING_HOLDINGS"
                await self._record_execution(db, binding, lead, account, pos.symbol, pos.inst_id,
                    "ALERT", pos.pos_side.upper(), action, "BLOCKED_ALLOCATION_DEFICIT",
                    lead_qty, lead_price, lead_notional,
                    details="Physical exposure cannot cover this exit and other binding allocations; "
                        "reconcile ownership before any exit", notify_cfg=notify_cfg, complete_delivery=False)
            return False
        physical, available = exact(pos.pos), exact(pos.avail_pos)
        if physical < 0 or available < 0 or available > physical:
            raise UnknownOrderOutcome("Invalid reducible position evidence")
        step = spec.market_lot_sz or spec.lot_sz
        minimum = spec.market_min_sz or spec.min_sz
        maximum = spec.market_max_sz
        limit = min(exact(quantity), self._owned_qty(allocation), max(Decimal("0"), physical - reserved), available)
        if maximum is not None:
            limit = min(limit, exact(maximum))
        qty = quantize_down(limit, step)
        if qty <= 0 or qty < exact(minimum):
            remaining = exact(quantity)
            if delivery is not None and 0 < remaining < exact(minimum):
                # 剩余量本身就是零头：再等也成交不了，只会永久卡住本投递
                # 并阻塞后续事件。直接核销完成，账本不动（见 _absorb_exit_remainder）。
                await self._absorb_exit_remainder(
                    db, binding, lead, account, delivery, pos.symbol, pos.inst_id,
                    "SELL" if pos.pos_side == "long" else "BUY", pos.pos_side.upper(), action,
                    lead_qty, lead_price, lead_notional, remaining, exact(minimum))
                return False
            if delivery is not None:
                delivery.exit_progress = "WAITING_AVAILABILITY" if exact(pos.avail_pos) <= 0 else "WAITING_MINIMUM"
            await self._record_execution(db, binding, lead, account, pos.symbol, pos.inst_id,
                "SELL" if pos.pos_side == "long" else "BUY", pos.pos_side.upper(), action,
                "SKIPPED_BELOW_MIN", lead_qty, lead_price, lead_notional,
                details="Reducible quantity below exchange minimum; remainder retained", notify_cfg=notify_cfg,
                complete_delivery=False)
            return False
        try:
            # 平仓的本地校验同样是确定拒单，不能留下 UNKNOWN 意图。
            spec.validate_order("market", qty, reduce_only=True)
        except (ValueError, ArithmeticError) as exc:
            raise OrderRejected(account.exchange, "LOCAL_VALIDATION", str(exc)) from exc
        if delivery is not None:
            delivery.exit_progress = "WAITING_ORDER"
        order_side = "sell" if pos.pos_side == "long" else "buy"
        res = await self._submit_confirmed(db, binding, account, adapter, spec,
            source_event_id=source_event_id, inst_id=pos.inst_id, side=order_side,
            pos_side=pos.pos_side, ord_type="market", sz=format_decimal(qty, step),
            px=None, reduce_only=True, td_mode=account.margin_mode, retry_terminal=True,
            source_evidence=source_evidence)
        fill_sz, fill_px = res.fill_sz, res.avg_px
        await self._record_execution(db, binding, lead, account, pos.symbol, pos.inst_id,
            order_side.upper(), pos.pos_side.upper(), action, "EXECUTED" if fill_sz > 0 else "UNFILLED",
            lead_qty, lead_price, lead_notional, my_qty=fill_sz, my_price=fill_px,
            my_notional=float(exact(fill_sz) * spec.ct_val * exact(fill_px)), order_id=res.ord_id,
            details=f"Confirmed close fill {fill_sz}; binding remaining {allocation.owned_qty}", notify_cfg=notify_cfg,
            complete_delivery=False)
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
        status: str = "FAILED",
    ) -> None:
        binding.consecutive_failures = (binding.consecutive_failures or 0) + 1
        max_fail = int(resilience_cfg.get("max_consecutive_failures", 5))
        if binding.consecutive_failures >= max_fail:
            binding.circuit_breaker_tripped = True
            binding.circuit_breaker_reason = f"Consecutive order failures ({binding.consecutive_failures}/{max_fail})"
        await self._record_execution(
            db, binding, lead, account, symbol, inst_id, side, ps, action,
            status, lead_qty, lead_avg_px, lead_notional,
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
    ) -> bool:
        sym = str(closed_pos.get("symbol") or "")
        raw_side = str(closed_pos.get("side") or "").upper()
        if not sym or raw_side not in ("LONG", "SHORT"):
            return False
        inst_id, supported = adapter.resolve_symbol(sym, instruments, json.loads(binding.symbol_map_json or "{}"))
        if not supported:
            return False
        local_side = raw_side.lower()
        allocation = await self._get_allocation(db, binding.id, account.id, inst_id, local_side)
        interval = closed_interval(closed_pos)
        if interval is None:
            return False
        closed_at = interval[1]
        if closed_at <= allocation.opened_at_ms:
            return True
        event_id = "closed:" + str(closed_pos.get("id") or closed_pos.get("positionId") or closed_at)
        if self._owned_qty(allocation) <= 0:
            return True
        source_evidence = (lead, {"_closed": closed_pos, "_event_id": event_id}, allocation)
        try:
            source_snapshot, saved_snapshot_required = await self._validate_exit_source(
                db, binding, account, *source_evidence)
        except UnknownOrderOutcome:
            return False
        source_evidence = (*source_evidence, source_snapshot, saved_snapshot_required)
        delivery = await self._exit_delivery(db, binding, account, allocation, event_id, self._owned_qty(allocation))
        if delivery is not None and exact(delivery.exit_remaining_qty) == 0:
            return True
        if self._owned_qty(allocation) <= 0:
            return True
        pos = next((p for p in positions if p.inst_id == inst_id and p.pos_side == local_side), None)
        if pos is None or pos.pos <= 0:
            return False
        try:
            await self._close_owned(db, binding, lead, account, adapter, pos, instruments[inst_id],
                self._owned_qty(allocation), event_id, "RECONCILE_CLOSE", notify_cfg,
                source_evidence=source_evidence)
        except UnknownOrderOutcome:
            return False
        return exact(delivery.exit_remaining_qty) == 0 if delivery is not None else self._owned_qty(allocation) <= 0

    async def _blocked_protection_positions(self, db, binding, account, adapter, instruments):
        allocations = (await db.scalars(select(PositionAllocation).where(
            PositionAllocation.binding_id == binding.id, PositionAllocation.account_id == account.id))).all()
        relevant = {(allocation.inst_id, allocation.pos_side) for allocation in allocations
                    if self._owned_qty(allocation) > 0}
        blocked = set()
        for position_key in sorted(relevant):
            if not await self.reconcile_pending_orders(db, account, adapter, instruments, {position_key}):
                blocked.add(position_key)
        return blocked

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
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """Detect follower positions whose lead leg no longer exists (e.g. closed during outage)."""
        if snapshot is None:
            await db.refresh(lead)
            try:
                snapshot = json.loads(lead.snapshot_json or "{}")
            except (TypeError, ValueError):
                snapshot = {}
        if (not is_authoritative_snapshot(snapshot)
                or snapshot.get("openPositions") != lead_open_positions
                or (not lead_open_positions and (snapshot.get("openPositionsMeta") or {}).get("emptyVerified") is not True)):
            logger.warning("Skipping orphan auto-close: lead open positions empty but unverified or abrupt drop without close events")
            return []
        await db.refresh(binding)
        blocked = await self._blocked_protection_positions(db, binding, account, adapter, instruments)
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
            if (pos.inst_id, pos.pos_side) in blocked:
                continue
            if pos.pos <= 0 or pos.pos_side not in ("long", "short"):
                continue
            if (pos.inst_id, pos.pos_side) in lead_keys:
                continue
            allocation = await self._get_allocation(db, binding.id, account.id, pos.inst_id, pos.pos_side)
            if allocation.owned_qty <= 0:
                continue

            raw_ps = "LONG" if pos.pos_side == "long" else "SHORT"
            if mode == "auto_close":
                if not is_authoritative_snapshot(snapshot):
                    return actions_taken
                # Every omitted leg requires independent evidence for its own cycle.
                reference = {
                    "symbol": next((sym for sym, inst in symbol_map.items() if inst == pos.inst_id), pos.symbol),
                    "positionSide": raw_ps,
                    "qty": allocation.owned_qty,
                    "openTime": allocation.opened_at_ms or None,
                }
                # Follower contracts/quantity cannot establish the lead's quantity.
                # Only independently recorded complete closed-cycle evidence may
                # authorize this autonomous exit. Fill-only quantity is ambiguous.
                if not positions_have_close_evidence([reference], [], snapshot.get("closed") or []):
                    logger.warning("Skipping orphan auto-close: missing leg without close events for its own cycle")
                    evidence_key = (f"Orphan closure evidence missing; allocation {getattr(allocation, 'id', 0)} "
                        f"generation {getattr(allocation, 'generation', 0)}")
                    previous = await db.scalar(select(ExecutionRecord.id).where(
                        ExecutionRecord.binding_id == binding.id,
                        ExecutionRecord.inst_id == pos.inst_id,
                        ExecutionRecord.position_side == raw_ps,
                        ExecutionRecord.status == "BLOCKED_ORPHAN_CLOSE_EVIDENCE",
                        ExecutionRecord.details == evidence_key))
                    if previous is None:
                        await self._record_execution(db, binding, lead, account, pos.symbol, pos.inst_id,
                            "ALERT", raw_ps, "ORPHAN_ALERT", "BLOCKED_ORPHAN_CLOSE_EVIDENCE", 0, 0, 0,
                            details=evidence_key, notify_cfg=ntf_cfg)
                    continue
                spec = instruments.get(pos.inst_id)
                if not spec:
                    continue
                try:
                    event_id = await self._exit_event_id(db, binding, allocation, "orphan")
                    db.info["snapshot_admission"] = (lead, snapshot)
                    closed = next((closed for closed in snapshot.get("closed") or []
                        if closed.get("symbol") == reference["symbol"] and
                        str(closed.get("side") or closed.get("positionSide") or "").upper() == raw_ps and
                        (interval := closed_interval(closed, snapshot_observed_ms(snapshot))) is not None and
                        interval[0] <= allocation.opened_at_ms < interval[1]), None)
                    if closed is None:
                        continue
                    if await self._close_owned(db, binding, lead, account, adapter, pos, spec,
                        min(allocation.owned_qty, pos.pos), event_id, "ORPHAN_AUTO_CLOSE", ntf_cfg,
                        source_evidence=(lead, {"_closed": closed}, allocation)):
                        actions_taken.append(f"closed:{pos.inst_id}:{pos.pos_side}")
                except Exception:
                    logger.exception("Orphan auto-close failed for %s", pos.inst_id)
                finally:
                    db.info.pop("snapshot_admission", None)
            else:
                # 冷却与告警同事务持久化，重启不能重复发送同一轮孤儿仓提示。
                identity = f"{binding.id}:{pos.inst_id}:{pos.pos_side}:{allocation.generation}"
                alert_key = "orphan_alert:" + hashlib.sha256(identity.encode()).hexdigest()[:48]
                cooldown = await db.get(SystemConfig, alert_key, populate_existing=True)
                last_alert = float(json.loads(cooldown.value_json)) if cooldown else 0.0
                if now - last_alert >= 900.0:
                    if cooldown is None:
                        db.add(SystemConfig(key=alert_key, value_json=json.dumps(now)))
                    else:
                        cooldown.value_json = json.dumps(now)
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
        blocked = await self._blocked_protection_positions(db, binding, account, adapter, instruments)
        mode = getattr(binding, "tp_sl_mode", "follow_lead") or "follow_lead"
        if mode == "follow_lead":
            return []
        notify_cfg = notify_cfg or await self._get_config(db, "notifications", DEFAULT_SYSTEM_CONFIG["notifications"])
        triggered: List[str] = []
        await db.commit()
        local_positions = await adapter.get_positions()

        for pos in local_positions:
            if (pos.inst_id, pos.pos_side) in blocked:
                continue
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
                evidence_key = f"Protective quote unavailable; allocation {allocation.id} generation {allocation.generation}"
                previous = await db.scalar(select(ExecutionRecord.id).where(
                    ExecutionRecord.binding_id == binding.id,
                    ExecutionRecord.inst_id == pos.inst_id,
                    ExecutionRecord.status == "BLOCKED_PROTECTION_UNAVAILABLE",
                    ExecutionRecord.details == evidence_key))
                if previous is None:
                    await self._record_execution(db, binding, lead, account, pos.symbol, pos.inst_id,
                        "ALERT", pos.pos_side.upper(), "PROTECTION_ALERT", "BLOCKED_PROTECTION_UNAVAILABLE",
                        0, 0, 0, details=evidence_key, notify_cfg=notify_cfg)
                else:
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
