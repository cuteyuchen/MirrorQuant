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


async def require_account_flat(db: AsyncSession, account: TradingAccount, *, deleting: bool = False) -> None:
    bindings = list((await db.scalars(select(CopyBinding).where(CopyBinding.account_id == account.id))).all())
    for binding in bindings:
        await require_binding_flat(db, binding, deleting=deleting)
    try:
        credentials = CredentialVault().decrypt_dict(account.encrypted_credentials)
        proxies = list((await db.scalars(select(ProxyNode).order_by(ProxyNode.priority))).all())
        proxy_url = ProxyManager.select_proxy_url(proxies, account.proxy_id, exchange=account.exchange)
        adapter = create_exchange_adapter(account.exchange, credentials, account.environment, proxy_url,
                                          margin_mode=account.margin_mode, position_mode=account.position_mode)
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
        raise HTTPException(status_code=409, detail="Cannot verify a flat account; keep it disabled until reconciliation succeeds") from exc
    if any(abs(position.pos) > 0 for position in positions) or orders:
        raise HTTPException(status_code=409, detail="Drain physical positions and pending orders before changing or deleting the account")
