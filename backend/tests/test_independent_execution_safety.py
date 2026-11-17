"""Offline lock ownership, timestamp boundaries and notification provenance."""
import asyncio
import json
import subprocess
import sys
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database import get_session_factory
from app.core.security import CredentialVault
from app.models.domain import BindingDelivery, BindingPositionProgress, ExecutionRecord, LeadSource, NotificationOutbox, OrderIntent, PositionAllocation
from app.services.engine.router import CopyRouter
from app.services.engine.serialization import execution_lock, serialized_execution
from test_auto_follow_hidden_positions import workflow, fixed_clock, hidden_snapshot, opening_order, NOW_MS
from test_follow_recovery_audit import trading, source_delivery
from test_router_remediations import OfflineExchange


async def test_nested_decorators_and_child_context_do_not_deadlock_or_bypass_lock():
    sequence = []

    @serialized_execution
    @serialized_execution
    async def child():
        sequence.append('child')

    async with execution_lock():
        child_task = asyncio.create_task(child())
        await asyncio.sleep(0.01)
        assert sequence == []
    await asyncio.wait_for(child_task, timeout=2)
    assert sequence == ['child']


async def test_canceled_executor_releases_task_and_os_locks():
    entered = asyncio.Event()

    async def owner():
        async with execution_lock():
            entered.set()
            await asyncio.Event().wait()

    owner_task = asyncio.create_task(owner())
    await entered.wait()
    owner_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner_task

    @serialized_execution
    async def successor():
        return 'acquired'

    assert await asyncio.wait_for(successor(), timeout=2) == 'acquired'


async def test_ordering_review_is_sticky_when_new_ambiguous_events_arrive(workflow):
    snapshot = hidden_snapshot()
    snapshot['openPositions'][0].update(qty=2, qtyExact='2')
    sources = [opening_order(), {**opening_order(), 'orderId': 'equal-time-source'}]
    workflow.lead.snapshot_json = json.dumps(snapshot)
    await workflow.db.commit()
    await workflow.router.process_lead_events(workflow.db, workflow.lead, sources, [], snapshot, [workflow.proxy])
    state = await workflow.db.get(BindingPositionProgress, (workflow.binding.id, 'BTCUSDT', 'LONG'))
    reason = state.review_reason
    assert reason
    sources.append({**opening_order(), 'orderId': 'malformed-new-source', 'orderTime': 'invalid', 'orderUpdateTime': 'invalid'})
    snapshot['openPositions'][0].update(qty=3, qtyExact='3')
    workflow.lead.snapshot_json = json.dumps(snapshot)
    await workflow.db.commit()
    async with get_session_factory()() as restarted:
        lead = await restarted.get(LeadSource, workflow.lead.id)
        for replay in range(2):
            await CopyRouter(CredentialVault()).process_lead_events(restarted, lead, sources, [], snapshot, [workflow.proxy])
        state = await restarted.get(BindingPositionProgress, (workflow.binding.id, 'BTCUSDT', 'LONG'))
        assert state.review_reason == reason
        records = (await restarted.scalars(select(ExecutionRecord))).all()
        assert len(records) == 3 and all(row.status == 'BLOCKED_SOURCE_ORDERING' for row in records)
        assert not (await restarted.scalars(select(OrderIntent))).all()
    workflow.adapter.place_order.assert_not_awaited()


async def test_sqlite_execution_lock_serializes_independent_processes(tmp_path):
    worker = '''
import asyncio, json, sys
from pathlib import Path
sys.path[:0] = json.loads(sys.argv[1])
from app.core import database
from app.services.engine.serialization import execution_lock
root, label = Path(sys.argv[2]), sys.argv[3]
database.get_engine(f"sqlite+aiosqlite:///{root / 'fake.sqlite'}")
async def run():
    (root / f"ready-{label}").touch()
    while not (root / "go").exists():
        await asyncio.sleep(0.01)
    async with execution_lock():
        with (root / "sequence").open("a") as output:
            output.write(f"start-{label}\\n")
        await asyncio.sleep(0.1)
        with (root / "sequence").open("a") as output:
            output.write(f"end-{label}\\n")
asyncio.run(run())
'''
    processes = [subprocess.Popen([sys.executable, '-I', '-S', '-c', worker,
        json.dumps(sys.path), str(tmp_path), label], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for label in ('first', 'second')]
    try:
        async def ready():
            while not all((tmp_path / f'ready-{label}').exists() for label in ('first', 'second')):
                assert all(process.poll() is None for process in processes)
                await asyncio.sleep(0.01)
        await asyncio.wait_for(ready(), timeout=10)
        (tmp_path / 'go').touch()
        outputs = await asyncio.gather(*(asyncio.to_thread(process.communicate, timeout=10) for process in processes))
        assert all(process.returncode == 0 for process in processes), outputs
        sequence = (tmp_path / 'sequence').read_text().splitlines()
        assert sequence in (['start-first', 'end-first', 'start-second', 'end-second'],
            ['start-second', 'end-second', 'start-first', 'end-first'])
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize('symbol,position_side', [('ETHUSDT', 'LONG'), ('BTCUSDT', 'SHORT')])
async def test_review_blocks_only_the_affected_instrument_and_side(workflow, symbol, position_side):
    snapshot = hidden_snapshot()
    snapshot['openPositions'].append({'symbol': symbol, 'positionSide': position_side, 'qty': 1, 'qtyExact': '1'})
    workflow.adapter.get_instruments.return_value[symbol] = replace(workflow.spec, inst_id=symbol)
    source = {**opening_order(), 'orderId': 'unblocked-source', 'symbol': symbol,
        'positionSide': position_side, 'side': 'BUY' if position_side == 'LONG' else 'SELL'}
    workflow.db.add(BindingPositionProgress(binding_id=workflow.binding.id, symbol='BTCUSDT',
        position_side='LONG', source_time_ms=NOW_MS + 4000, source_event_id='seeded-watermark'))
    # 审查结论是派生 verdict：用超前的顺序水位作证据让重算立起结论，
    # 而不是直接种一个无证据的结论；结论只应冻结受影响的合约方向。
    workflow.lead.snapshot_json = json.dumps(snapshot)
    await workflow.db.commit()
    await workflow.router.process_lead_events(workflow.db, workflow.lead, [opening_order(), source], [], snapshot, [workflow.proxy])
    workflow.adapter.place_order.assert_awaited_once()
    own = await workflow.db.scalar(select(PositionAllocation))
    assert (own.inst_id, own.pos_side, Decimal(own.owned_qty_exact)) == (symbol, position_side.lower(), Decimal('1'))
    async with get_session_factory()() as restarted:
        lead = await restarted.get(LeadSource, workflow.lead.id)
        await CopyRouter(CredentialVault()).process_lead_events(restarted, lead, [opening_order(), source], [], snapshot, [workflow.proxy])
        blocked = (await restarted.scalars(select(ExecutionRecord).where(ExecutionRecord.status == 'BLOCKED_SOURCE_ORDERING'))).all()
        assert len(blocked) == 1
        assert len((await restarted.scalars(select(OrderIntent))).all()) == 1
    workflow.adapter.place_order.assert_awaited_once()


@pytest.mark.parametrize('field', ['asOf', 'generatedAt', 'collectionStartedAtMs'])
@pytest.mark.parametrize('value', [None, 0, True, 'invalid', NOW_MS + 1, NOW_MS - 30_001])
async def test_timestamp_becomes_unsafe_at_durable_submission_boundary(workflow, field, value):
    snapshot = hidden_snapshot()
    snapshot['openPositionsMeta']['asOf'] = None
    snapshot['meta'].pop('generatedAt')
    metadata = snapshot['openPositionsMeta'] if field == 'asOf' else snapshot['meta']
    metadata[field] = NOW_MS
    workflow.lead.snapshot_json = json.dumps(snapshot)
    await workflow.db.commit()
    original_commit = workflow.db.commit
    changed = False

    async def commit():
        nonlocal changed
        submitting = any(isinstance(row, OrderIntent) and row.state == 'SUBMITTING' for row in workflow.db.new)
        await original_commit()
        if submitting and not changed:
            changed = True
            saved = json.loads(json.dumps(snapshot))
            saved_metadata = saved['openPositionsMeta'] if field == 'asOf' else saved['meta']
            saved_metadata[field] = value
            async with get_session_factory()() as concurrent:
                lead = await concurrent.get(LeadSource, workflow.lead.id)
                lead.snapshot_json = json.dumps(saved)
                await concurrent.commit()

    workflow.db.commit = commit
    await workflow.router.process_lead_events(workflow.db, workflow.lead, [opening_order()], [], snapshot, [workflow.proxy])
    assert changed
    workflow.adapter.place_order.assert_not_awaited()
    intents = (await workflow.db.scalars(select(OrderIntent))).all()
    assert len(intents) == 1 and intents[0].state == 'REJECTED'
    async with get_session_factory()() as restarted:
        lead = await restarted.get(LeadSource, workflow.lead.id)
        await CopyRouter(CredentialVault()).process_lead_events(restarted, lead, [opening_order()], [], snapshot, [workflow.proxy])
        assert len((await restarted.scalars(select(OrderIntent))).all()) == 1
        assert len((await restarted.scalars(select(BindingDelivery))).all()) == 1
        allocations = (await restarted.scalars(select(PositionAllocation))).all()
        assert len(allocations) == 1
        assert Decimal(allocations[0].owned_qty_exact) == 0
        assert Decimal(allocations[0].entry_cost_exact) == 0
    workflow.adapter.place_order.assert_not_awaited()


@pytest.mark.parametrize('notification_state', ['unknown', 'pending', 'failed', 'sent', 'not_required'])
async def test_record_without_outbox_never_infers_a_pending_notification(trading, notification_state):
    delivery = await source_delivery(trading)
    delivery.state = 'APPLIED'
    intent = OrderIntent(id='historical-notification', binding_id=trading.binding.id, account_id=trading.account.id,
        inst_id='BTCUSDT', pos_side='long', source_event_id='recovery', state='FILLED',
        requested_qty='1', filled_qty='1', avg_px='100', reduce_only=False, exchange_order_id='historical-order')
    record = ExecutionRecord(binding_id=trading.binding.id, lead_id=trading.lead.id, lead_name='synthetic',
        account_id=trading.account.id, account_name='offline', inst_id='BTCUSDT', symbol='BTCUSDT',
        position_side='LONG', side='BUY', action='OPEN_LONG', status='EXECUTED',
        my_qty=1, my_price=100, order_id='historical-order', details='Historical confirmed fill',
        notification_state=notification_state)
    trading.db.add_all([intent, record])
    await trading.db.commit()
    exchange = OfflineExchange()
    proven = notification_state in ('sent', 'not_required')
    for router in (trading.router, CopyRouter(CredentialVault())):
        assert await router.reconcile_pending_orders(trading.db, trading.account, exchange, {'BTCUSDT': trading.spec}) is proven
    assert record.notification_state == (notification_state if proven else 'unknown')
    assert not (await trading.db.scalars(select(NotificationOutbox))).all()
    assert len((await trading.db.scalars(select(ExecutionRecord))).all()) == 1
    assert exchange.orders == []
