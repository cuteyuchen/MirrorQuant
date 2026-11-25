"""Fail-closed lifecycle checks, called while holding the execution lock."""
from __future__ import annotations

import asyncio
import math

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import CredentialVault
from app.models.domain import CopyBinding, OrderIntent, PositionAllocation, PositionGuardState, ProxyNode, TradingAccount
from app.services.adapters.factory import create_exchange_adapter
from app.services.proxy.manager import ProxyManager

TERMINAL_ORDER_STATES = ("FILLED", "CANCELED", "REJECTED", "EXPIRED")


async def require_binding_flat(db: AsyncSession, binding: CopyBinding, *, deleting: bool = False) -> None:
    allocation = await db.scalar(select(PositionAllocation.id).where(
        PositionAllocation.binding_id == binding.id, PositionAllocation.owned_qty > 0,
    ).limit(1))
    guard = await db.scalar(select(PositionGuardState.id).where(PositionGuardState.binding_id == binding.id).limit(1))
    intent_query = select(OrderIntent.id).where(OrderIntent.binding_id == binding.id)
    if not deleting:
        intent_query = intent_query.where(OrderIntent.state.not_in(TERMINAL_ORDER_STATES))
    intent = await db.scalar(intent_query.limit(1))
    if allocation is not None or guard is not None or intent is not None:
        detail = ("Binding has exposure, protection, or retained order history; disable it and retain its ledger"
                  if deleting else "Drain and reconcile binding exposure before changing its mapping or protection")
        raise HTTPException(status_code=409, detail=detail)


async def _verify_account_flat_on_exchange(
    db: AsyncSession, account: TradingAccount, *, allow_unreachable: bool = False
) -> None:
    """Confirm the account holds no physical positions and no open orders on the exchange.

    Shared by the hard-delete flat check and the soft-delete check; a verification
    failure is fail-closed (409) in both cases unless allow_unreachable is explicitly
    requested for a locally-flat account whose API key or exchange is unreachable.
    """
    try:
        credentials = CredentialVault().decrypt_dict(account.encrypted_credentials)
        proxies = list((await db.scalars(select(ProxyNode).order_by(ProxyNode.priority))).all())
        proxy_url = ProxyManager.select_proxy_url(proxies, account.proxy_id, exchange=account.exchange)
        adapter = create_exchange_adapter(account.exchange, credentials, account.environment, proxy_url,
                                          margin_mode=account.margin_mode, position_mode=account.position_mode,
                                          account_id=account.id)  # 生命周期检查复用同一账户连接。
        positions = await asyncio.wait_for(adapter.get_positions(), timeout=15)
        get_open_orders = getattr(adapter, "get_open_orders", None)
        if get_open_orders is None:
            raise RuntimeError("Exchange open-order verification unavailable")
        orders = await asyncio.wait_for(get_open_orders(), timeout=15)
        if not isinstance(positions, list) or not isinstance(orders, list):
            raise ValueError("Incomplete exchange inventory")
        if any(not math.isfinite(float(position.pos)) for position in positions):
            raise ValueError("Invalid exchange position inventory")
    except Exception as exc:
        if allow_unreachable:
            return
        raise HTTPException(
            status_code=409,
            detail={
                "code": "EXCHANGE_UNREACHABLE_CAN_FORCE",
                "message": "Cannot verify a flat account; keep it disabled until reconciliation succeeds",
            },
        ) from exc
    if any(abs(position.pos) > 0 for position in positions) or orders:
        raise HTTPException(status_code=409, detail="Drain physical positions and pending orders before changing or deleting the account")


async def require_account_flat(db: AsyncSession, account: TradingAccount, *, deleting: bool = False) -> None:
    bindings = list((await db.scalars(select(CopyBinding).where(
        CopyBinding.account_id == account.id, CopyBinding.deleted_at.is_(None)))).all())
    for binding in bindings:
        await require_binding_flat(db, binding, deleting=deleting)
    await _verify_account_flat_on_exchange(db, account)


async def require_binding_soft_deletable(db: AsyncSession, binding: CopyBinding) -> None:
    """Gate for logical delete: the binding must be drained, but its terminal order
    history is the retained ledger and does NOT block deletion.
    Orphan PositionGuardState rows whose allocation is already flat are cleaned up.
    """
    live_allocs = list((await db.scalars(select(PositionAllocation).where(
        PositionAllocation.binding_id == binding.id, PositionAllocation.owned_qty > 0,
    ))).all())
    live_keys = {(a.inst_id, a.pos_side) for a in live_allocs}
    guards = list((await db.scalars(select(PositionGuardState).where(
        PositionGuardState.binding_id == binding.id,
    ))).all())
    stale_guards = [g for g in guards if (g.inst_id, g.pos_side) not in live_keys]
    for g in stale_guards:
        await db.delete(g)
    live_guard = next((g for g in guards if (g.inst_id, g.pos_side) in live_keys), None)
    intent = await db.scalar(select(OrderIntent).where(
        OrderIntent.binding_id == binding.id, OrderIntent.state.not_in(TERMINAL_ORDER_STATES),
    ).limit(1))
    if live_allocs or live_guard is not None or intent is not None:
        details = []
        if live_allocs:
            details.append(", ".join(f"{a.inst_id}({a.pos_side})={a.owned_qty:g}" for a in live_allocs[:3]))
        if intent is not None:
            details.append(f"pending order {intent.inst_id} ({intent.state})")
        suffix = f": {'; '.join(details)}" if details else ""
        raise HTTPException(
            status_code=409,
            detail=f"Binding has live exposure, protection, or non-terminal order history; drain it before deletion{suffix}",
        )


async def require_account_soft_deletable(
    db: AsyncSession,
    account: TradingAccount,
    *,
    auto_disable: bool = False,
    force: bool = False,
) -> list[CopyBinding]:
    """Gate for logical delete of an account: every live (non-deleted) binding must be
    disabled (unless auto_disable=True) and soft-deletable, and the account must be
    physically flat on the exchange (or force=True when exchange is unreachable and local ledger is flat).
    """
    from app.models.domain import ExecutionRecord

    bindings = list((await db.scalars(select(CopyBinding).where(
        CopyBinding.account_id == account.id, CopyBinding.deleted_at.is_(None)))).all())
    for binding in bindings:
        if binding.is_enabled and not auto_disable:
            raise HTTPException(
                status_code=409,
                detail="Disable all bindings before deleting the account",
            )
        await require_binding_soft_deletable(db, binding)

    if account.last_sync_at is None:
        has_any_intent = await db.scalar(select(OrderIntent.id).where(
            OrderIntent.account_id == account.id).limit(1))
        has_any_exec = await db.scalar(select(ExecutionRecord.id).where(
            ExecutionRecord.account_id == account.id).limit(1))
        if has_any_intent is None and has_any_exec is None:
            return bindings

    await _verify_account_flat_on_exchange(db, account, allow_unreachable=force)
    return bindings
