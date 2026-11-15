import json
from decimal import Decimal
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select
from app.core.database import get_session_factory
from app.core.security import CredentialVault
from app.models.domain import BindingDelivery, LeadSource, PositionAllocation
from app.services.adapters.base import BalanceInfo
from app.services.engine.router import CopyRouter
from test_routing_deliveries import exit_delivery_workflow, order, snapshot
from test_review_execution import close_transport, run_closed

async def route_order(w, reduced=False):
    src = order(orderId='audit-reduce', side='SELL', executedQty='4' if reduced else '10', orderTime=w.source_time, orderUpdateTime=w.source_time)
    snap = snapshot()
    if reduced:
        snap['openPositions'][0].update(qty=6, qtyExact='6')
    else:
        snap['openPositions'] = []
        snap['openPositionsMeta']['emptyVerified'] = True
    async with get_session_factory()() as db:
        lead = await db.get(LeadSource, w.lead_id)
        lead.snapshot_json = json.dumps(snap)
        await db.commit()
        await CopyRouter(CredentialVault()).process_lead_events(db, lead, [src], [], snap, [])
    async with get_session_factory()() as db:
        delivery = await db.scalar(select(BindingDelivery).where(BindingDelivery.binding_id == w.binding_id))
        allocation = await db.get(PositionAllocation, w.owned_id)
        return delivery.state, Decimal(allocation.owned_qty_exact)

async def test_order_exit_unavailable_should_remain_pending(exit_delivery_workflow):
    w = exit_delivery_workflow
    w.adapter.get_contract_balance = AsyncMock(return_value=BalanceInfo(100,100,100,0))
    w.position.avail_pos = 0
    state = await route_order(w)
    w.position.avail_pos = 10
    again = await route_order(w)
    assert (state, again) == (('PENDING', Decimal('4')), ('APPLIED', Decimal('0'))), f'actual={(state, again)}; orders={w.adapter.place_order.await_count}'

async def test_order_terminal_partial_should_retry_remainder(exit_delivery_workflow):
    w = exit_delivery_workflow
    orders, _ = close_transport(w, [('canceled', Decimal('1.2')), ('filled', Decimal('2.8'))])
    state = await route_order(w)
    again = await route_order(w)
    assert (state, again) == (('PENDING', Decimal('2.8')), ('APPLIED', Decimal('0'))), f'actual={(state, again)}; sizes={[v["sz"] for v in orders.values()]}'

async def test_partial_source_exit_sizing_must_use_owned_not_available(exit_delivery_workflow):
    w = exit_delivery_workflow
    orders, _ = close_transport(w, [('filled', Decimal('0.4'))])
    w.position.avail_pos = 1
    state = await route_order(w, reduced=True)
    assert state == ('PENDING', Decimal('3.6')), f'actual={state}; sizes={[v["sz"] for v in orders.values()]}'

async def test_closed_retry_wrongly_completes_when_allocation_deficit(exit_delivery_workflow):
    w = exit_delivery_workflow
    w.adapter.get_contract_balance = AsyncMock(return_value=BalanceInfo(100,100,100,0))
    w.position.pos = w.position.avail_pos = 7
    state = await run_closed(w)
    assert state == ("PENDING", Decimal("4")), f"actual={state}; orders={w.adapter.place_order.await_count}"
