import json
import time
from decimal import Decimal
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select
from app.core.database import get_session_factory
from app.core.security import CredentialVault
from app.models.domain import LeadSource, PositionAllocation, BindingDelivery, OrderIntent
from app.services.adapters.base import BalanceInfo
from app.services.engine.router import CopyRouter
from test_routing_deliveries import exit_delivery_workflow, snapshot

@pytest.mark.parametrize('kind', ['future', 'reversed', 'boolean'])
async def test_unprovable_close_never_submits(exit_delivery_workflow, kind):
    w = exit_delivery_workflow
    w.adapter.get_contract_balance = AsyncMock(return_value=BalanceInfo(100, 100, 100, 0))
    current = snapshot()
    current['openPositions'] = []
    current['openPositionsMeta']['emptyVerified'] = True
    closed = {'id': 'adversarial-closure', 'symbol': 'BTCUSDT', 'side': 'LONG', 'opened': w.source_time-1000, 'closed': w.source_time}
    if kind == 'future': closed['closed'] = int(time.time()*1000)+60000
    elif kind == 'reversed': closed['opened'] = w.source_time+1000
    else: closed['closed'] = True
    async with get_session_factory()() as db:
        lead = await db.get(LeadSource, w.lead_id)
        lead.snapshot_json = json.dumps(current)
        await db.commit()
        await CopyRouter(CredentialVault()).process_lead_events(db, lead, [], [closed], current, [])
        own = await db.get(PositionAllocation, w.owned_id, populate_existing=True)
        intents = (await db.scalars(select(OrderIntent))).all()
        print('CASE',kind,'submitted',w.adapter.place_order.await_count,'owned',own.owned_qty_exact,'intents',[(i.state,i.requested_qty) for i in intents])
        assert w.adapter.place_order.await_count == 0
        assert Decimal(own.owned_qty_exact) == Decimal('4')

async def test_concurrent_distinct_closed_events_do_not_double_reduce(exit_delivery_workflow):
    import asyncio
    w = exit_delivery_workflow
    w.adapter.get_contract_balance = AsyncMock(return_value=BalanceInfo(100, 100, 100, 0))
    current = snapshot()
    current['openPositions'] = []
    current['openPositionsMeta']['emptyVerified'] = True
    async with get_session_factory()() as db:
        lead = await db.get(LeadSource, w.lead_id)
        lead.snapshot_json = json.dumps(current)
        await db.commit()
    async def route(label):
        async with get_session_factory()() as db:
            lead = await db.get(LeadSource, w.lead_id)
            closed = {'id': label, 'symbol': 'BTCUSDT', 'side': 'LONG', 'opened': w.source_time - 1000, 'closed': w.source_time}
            await CopyRouter(CredentialVault()).process_lead_events(db, lead, [], [closed], current, [])
    await asyncio.gather(route('first'), route('second'))
    async with get_session_factory()() as db:
        own = await db.get(PositionAllocation, w.owned_id)
        other = await db.get(PositionAllocation, w.other_owned_id)
        intents = (await db.scalars(select(OrderIntent))).all()
        print('DISTINCT CONCURRENT EVENTS submitted',w.adapter.place_order.await_count,'owned',own.owned_qty_exact,'other',other.owned_qty_exact,'intents',len(intents))
        assert w.adapter.place_order.await_count == 1
        assert Decimal(own.owned_qty_exact) == 0 and Decimal(other.owned_qty_exact) == 5
        assert len(intents) == 1
