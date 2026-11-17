"""Causal-order regressions use fake adapters, persisted state and no sink I/O."""
import asyncio
import json
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database import get_session_factory
from app.core.security import CredentialVault
from app.models.domain import BindingPositionProgress, BindingDelivery, CopyBinding, ExecutionRecord, LeadSource, OrderIntent, PositionAllocation, TradingAccount
from app.services.adapters.base import ExchangePosition
from app.services.engine.router import CopyRouter
from test_auto_follow_hidden_positions import workflow as hidden_workflow, fixed_clock, hidden_snapshot, opening_order, NOW_MS
from test_router_remediations import trading
from test_independent_lifecycle_regressions import prepare_simulation


@pytest.fixture
async def workflow(hidden_workflow):
    async def positions():
        quantity = sum((Decimal(call.kwargs['sz']) for call in hidden_workflow.adapter.place_order.await_args_list), Decimal('0'))
        return [ExchangePosition('BTCUSDT', 'BTCUSDT', 'long', 'cross', float(quantity), float(quantity),
            100, 0, 0, float(quantity * 100), 10)] if quantity else []
    hidden_workflow.adapter.get_positions.side_effect = positions
    return hidden_workflow


@pytest.mark.parametrize('arrival', ['chronological', 'reverse'])
async def test_same_batch_reductions_with_trustworthy_order_finish_at_one(trading, monkeypatch, arrival):
    simulation, engine, other = await prepare_simulation(trading, monkeypatch)
    for opening in range(4):
        simulation.event('BUY')
        await engine.sync_lead(trading.db, trading.lead, proxies=[])
    simulation.event('SELL', '2')
    simulation.history[-1]['orderId'] = 'zz-earlier'
    simulation.event('SELL', '1')
    simulation.history[-1]['orderId'] = 'aa-later'
    if arrival == 'reverse':
        simulation.history[-2:] = list(reversed(simulation.history[-2:]))
    await engine.sync_lead(trading.db, trading.lead, proxies=[])
    own = await trading.db.scalar(select(PositionAllocation).where(PositionAllocation.binding_id == trading.binding.id))
    assert Decimal(own.owned_qty_exact) == 1 and simulation.physical == 6 and other.owned_qty_exact == '2'
    assert [Decimal(order['quantity']) for order in simulation.orders.values()] == [1, 1, 1, 1, 2, 1]
    state = await trading.db.get(BindingPositionProgress, (trading.binding.id, 'BTCUSDT', 'LONG'))
    assert state.source_remaining_qty == '1' and state.review_reason is None
    async with get_session_factory()() as restarted:
        lead = await restarted.get(LeadSource, trading.lead.id)
        for replay in range(2):
            await CopyRouter(engine.router.vault).process_lead_events(restarted, lead,
                list(json.loads(lead.orders_store_json).values()), [], json.loads(lead.snapshot_json), [])
        assert len((await restarted.scalars(select(OrderIntent))).all()) == 6
    assert len(simulation.orders) == 6


@pytest.mark.parametrize('timestamp_kind', ['equal', 'missing', 'zero', 'future', 'reversed_update', 'malformed', 'boolean'])
async def test_unprovable_ordering_blocks_before_any_mutation(workflow, timestamp_kind):
    snapshot = hidden_snapshot()
    snapshot['openPositions'][0]['qty'] = 2
    snapshot['openPositions'][0]['qtyExact'] = '2'
    first = opening_order()
    second = {**opening_order(), 'orderId': 'another-event'}
    if timestamp_kind == 'missing':
        first['orderTime'] = first['orderUpdateTime'] = None
    elif timestamp_kind == 'zero':
        first['orderTime'] = first['orderUpdateTime'] = 0
    elif timestamp_kind == 'future':
        first['orderTime'] = first['orderUpdateTime'] = NOW_MS + 1
    elif timestamp_kind == 'reversed_update':
        first['orderUpdateTime'] = first['orderTime'] - 1
    elif timestamp_kind == 'malformed':
        first['orderTime'] = first['orderUpdateTime'] = 'invalid'
    elif timestamp_kind == 'boolean':
        first['orderTime'] = first['orderUpdateTime'] = True
    workflow.lead.snapshot_json = json.dumps(snapshot)
    await workflow.db.commit()
    for router in (workflow.router, CopyRouter(CredentialVault())):
        await router.process_lead_events(workflow.db, workflow.lead, [second, first], [], snapshot, [workflow.proxy])
    workflow.adapter.place_order.assert_not_awaited()
    state = await workflow.db.get(BindingPositionProgress, (workflow.binding.id, 'BTCUSDT', 'LONG'))
    assert state.review_reason
    deliveries = (await workflow.db.scalars(select(BindingDelivery))).all()
    assert len(deliveries) == 2 and all(row.exit_progress == 'REVIEW_REQUIRED' for row in deliveries)
    assert not (await workflow.db.scalars(select(OrderIntent))).all()
    blocked = (await workflow.db.scalars(select(ExecutionRecord))).all()
    assert len(blocked) == 2 and all(row.status == 'BLOCKED_SOURCE_ORDERING' for row in blocked)


async def test_concurrent_intake_and_restart_do_not_duplicate(workflow):
    snapshot = hidden_snapshot()
    workflow.lead.snapshot_json = json.dumps(snapshot)
    await workflow.db.commit()
    async def intake():
        async with get_session_factory()() as session:
            lead = await session.get(LeadSource, workflow.lead.id)
            await CopyRouter(CredentialVault()).process_lead_events(session, lead, [opening_order()], [], snapshot, [workflow.proxy])
    await asyncio.gather(intake(), intake())
    await intake()
    workflow.adapter.place_order.assert_awaited_once()
    assert len((await workflow.db.scalars(select(OrderIntent))).all()) == 1
    assert len((await workflow.db.scalars(select(BindingDelivery))).all()) == 1
    assert len((await workflow.db.scalars(select(ExecutionRecord))).all()) == 1


async def test_partial_source_fill_requires_strictly_new_evidence(workflow):
    snapshot = hidden_snapshot()
    source = opening_order()
    workflow.lead.snapshot_json = json.dumps(snapshot)
    await workflow.db.commit()
    await workflow.router.process_lead_events(workflow.db, workflow.lead, [source], [], snapshot, [workflow.proxy])
    snapshot['openPositions'][0].update(qty=2, qtyExact='2')
    source['executedQty'] = '2'
    workflow.lead.snapshot_json = json.dumps(snapshot)
    await workflow.db.commit()
    await CopyRouter(CredentialVault()).process_lead_events(workflow.db, workflow.lead, [source], [], snapshot, [workflow.proxy])
    workflow.adapter.place_order.assert_awaited_once()
    state = await workflow.db.get(BindingPositionProgress, (workflow.binding.id, 'BTCUSDT', 'LONG'))
    assert state.review_reason and 'equal' in state.review_reason.lower()
    assert len((await workflow.db.scalars(select(OrderIntent))).all()) == 1


async def test_partial_source_fill_with_new_timestamp_is_incremental_and_replay_safe(workflow):
    snapshot = hidden_snapshot()
    source = opening_order()
    workflow.lead.snapshot_json = json.dumps(snapshot)
    await workflow.db.commit()
    await workflow.router.process_lead_events(workflow.db, workflow.lead, [source], [], snapshot, [workflow.proxy])
    snapshot['openPositions'][0].update(qty=2, qtyExact='2')
    source['executedQty'] = '2'
    source['orderUpdateTime'] += 500
    workflow.lead.snapshot_json = json.dumps(snapshot)
    await workflow.db.commit()
    for router in (workflow.router, CopyRouter(CredentialVault())):
        await router.process_lead_events(workflow.db, workflow.lead, [source], [], snapshot, [workflow.proxy])
    assert workflow.adapter.place_order.await_count == 2
    own = await workflow.db.scalar(select(PositionAllocation))
    assert Decimal(own.owned_qty_exact) == 2
    state = await workflow.db.get(BindingPositionProgress, (workflow.binding.id, 'BTCUSDT', 'LONG'))
    assert state.source_remaining_qty == '2' and state.review_reason is None
    assert len((await workflow.db.scalars(select(OrderIntent))).all()) == 2


async def test_binding_ordering_state_is_isolated(workflow):
    snapshot = hidden_snapshot()
    workflow.lead.snapshot_json = json.dumps(snapshot)
    second_account = TradingAccount(name='second fake follower', exchange=workflow.account.exchange,
        api_key_masked='***', encrypted_credentials=workflow.account.encrypted_credentials,
        usdt_equity=1000, avail_equity=1000, hwm_equity=1000, day_start_equity=1000)
    workflow.db.add(second_account)
    await workflow.db.flush()
    second = CopyBinding(name='second fake binding', lead_id=workflow.lead.id,
        account_id=second_account.id, baseline_seeded=True)
    workflow.db.add(second)
    workflow.db.add(BindingPositionProgress(binding_id=workflow.binding.id, symbol='BTCUSDT',
        position_side='LONG', source_time_ms=NOW_MS + 4000, source_event_id='seeded-watermark'))
    # 审查结论是派生 verdict：用超前的顺序水位作证据让重算立起结论，
    # 而不是直接种一个无证据的结论；结论只应冻结本绑定本合约方向。
    await workflow.db.commit()
    await workflow.router.process_lead_events(workflow.db, workflow.lead, [opening_order()], [], snapshot, [workflow.proxy])
    workflow.adapter.place_order.assert_awaited_once()
    own = await workflow.db.scalar(select(PositionAllocation))
    assert own.binding_id == second.id and Decimal(own.owned_qty_exact) == 1
    second_state = await workflow.db.get(BindingPositionProgress, (second.id, 'BTCUSDT', 'LONG'))
    assert second_state.review_reason is None and second_state.source_remaining_qty == '1'


async def test_partial_update_interval_overlapping_later_applied_event_is_ambiguous(workflow):
    snapshot = hidden_snapshot()
    first = opening_order()
    workflow.lead.snapshot_json = json.dumps(snapshot)
    await workflow.db.commit()
    await workflow.router.process_lead_events(workflow.db, workflow.lead, [first], [], snapshot, [workflow.proxy])
    second = {**opening_order(), 'orderId': 'later-open', 'orderTime': NOW_MS - 700, 'orderUpdateTime': NOW_MS - 700}
    snapshot['openPositions'][0].update(qty=2, qtyExact='2')
    workflow.lead.snapshot_json = json.dumps(snapshot)
    await workflow.db.commit()
    await workflow.router.process_lead_events(workflow.db, workflow.lead, [first, second], [], snapshot, [workflow.proxy])
    first['executedQty'] = '2'
    first['orderUpdateTime'] = NOW_MS - 500
    snapshot['openPositions'][0].update(qty=3, qtyExact='3')
    workflow.lead.snapshot_json = json.dumps(snapshot)
    await workflow.db.commit()
    await workflow.router.process_lead_events(workflow.db, workflow.lead, [first, second], [], snapshot, [workflow.proxy])
    assert workflow.adapter.place_order.await_count == 2
    state = await workflow.db.get(BindingPositionProgress, (workflow.binding.id, 'BTCUSDT', 'LONG'))
    assert state.review_reason and 'interval' in state.review_reason.lower()


async def test_overlapping_initial_order_intervals_are_not_sorted_by_update_or_id(workflow):
    snapshot = hidden_snapshot()
    snapshot['openPositions'][0].update(qty=2, qtyExact='2')
    earlier = opening_order()
    earlier['orderUpdateTime'] = NOW_MS - 500
    middle = {**opening_order(), 'orderId': 'middle', 'orderTime': NOW_MS - 700, 'orderUpdateTime': NOW_MS - 700}
    workflow.lead.snapshot_json = json.dumps(snapshot)
    await workflow.db.commit()
    await workflow.router.process_lead_events(workflow.db, workflow.lead, [earlier, middle], [], snapshot, [workflow.proxy])
    workflow.adapter.place_order.assert_not_awaited()
    state = await workflow.db.get(BindingPositionProgress, (workflow.binding.id, 'BTCUSDT', 'LONG'))
    assert state.review_reason and 'interval' in state.review_reason.lower()
