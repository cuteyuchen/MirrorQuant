"""跟单自愈回归：名义金额舍入抖动、审查结论重算、死意图取消、开仓重驱、待审查投递 reconciled。

复现 2026-11-17 线上实况：4 个 fill 因 BAPI 均价末位抖动被永久挂起、
5 条平仓投递卡在 REVIEW_REQUIRED、一笔 NVDA 开仓意图 UNKNOWN 导致漏跟。
"""
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.domain import (
    BindingDelivery, BindingPositionProgress, BindingSourceProgress, ExecutionRecord, NotificationOutbox,
    OrderIntent, PositionAllocation,
)
from app.services.engine.router import (
    _sequence_matches_interval, _source_notional_conflicts,
)
from app.services.engine.sizer import exact
from app.services.lead_collector.binance_bapi import expand_slim_order, order_dedup_key
from test_router_remediations import (
    NOW_MS, OfflineExchange, current_snapshot, prepare_event_router, trading,
)

# 线上实测的 4 个冲突 fill：数量完全一致，名义金额只差舍入抖动。
LIVE_FILLS = [
    ("1624.71", "264359.74661726", "162.711251"),   # NVDA BUY LONG
    ("1582.28", "257574.652488998", "162.7877509"),  # NVDA BUY LONG
    ("56.818", "50170.8777846", "882.62647"),        # ZEC SELL SHORT
    ("112.762", "99990.1789886", "886.92987"),       # ZEC SELL SHORT
]


@pytest.mark.parametrize("qty,recorded,price", LIVE_FILLS)
def test_live_notional_jitter_is_not_a_conflict(qty, recorded, price):
    fresh = exact(qty) * exact(price)
    assert not _source_notional_conflicts(recorded, fresh)


def test_large_notional_restatement_is_still_a_conflict():
    assert _source_notional_conflicts("264359.74", "261000.00")
    assert _source_notional_conflicts("100.00", "100.20")
    assert not _source_notional_conflicts("100.00", "100.00")


def test_sequence_interval_allows_one_ms_endpoint_difference():
    assert _sequence_matches_interval(1000, (999, 1001))
    assert _sequence_matches_interval(1001, (1000, 1001))
    assert not _sequence_matches_interval(1000, (999, 1002))
    assert not _sequence_matches_interval(1000, None)


def _individual_order(qty, price, timestamp):
    return {"orderTime": timestamp, "orderUpdateTime": timestamp,
            "symbol": "BTCUSDT", "side": "BUY", "positionSide": "LONG", "type": "MARKET",
            "executedQty": qty, "avgPrice": price}


async def test_notional_jitter_adopts_fresh_value_without_suspension(trading, monkeypatch, caplog):
    t = trading
    exchange = OfflineExchange()
    exchange.get_positions.return_value = []
    await prepare_event_router(t, exchange, monkeypatch)
    qty, recorded, price = LIVE_FILLS[0]
    order = _individual_order(qty, price, NOW_MS - 150_000)
    key = order_dedup_key(expand_slim_order(order))
    t.db.add(BindingSourceProgress(binding_id=t.binding.id, source_key=key,
                                   cumulative_qty=qty, cumulative_notional=recorded))
    await t.db.commit()
    caplog.set_level(logging.ERROR, logger="mirrorquant.router")
    await t.router.process_lead_events(t.db, t.lead, [order], [], current_snapshot("LONG"), [t.proxy])
    assert "Conflicting individual source fill" not in caplog.text
    progress = await t.db.get(BindingSourceProgress, (t.binding.id, key))
    assert progress.cumulative_notional == str(exact(qty) * exact(price))
    assert (await t.db.scalars(select(BindingDelivery))).all() == []


async def test_large_notional_restatement_still_suspends(trading, monkeypatch, caplog):
    t = trading
    exchange = OfflineExchange()
    exchange.get_positions.return_value = []
    await prepare_event_router(t, exchange, monkeypatch)
    order = _individual_order("100", "100", NOW_MS - 150_000)
    key = order_dedup_key(expand_slim_order(order))
    t.db.add(BindingSourceProgress(binding_id=t.binding.id, source_key=key,
                                   cumulative_qty="100", cumulative_notional="9000"))
    await t.db.commit()
    caplog.set_level(logging.ERROR, logger="mirrorquant.router")
    await t.router.process_lead_events(t.db, t.lead, [order], [], current_snapshot("LONG"), [t.proxy])
    assert "Conflicting individual source fill" in caplog.text
    progress = await t.db.get(BindingSourceProgress, (t.binding.id, key))
    assert progress.cumulative_notional == "9000"


def _delivery_payload(symbol, side, position_side, start, end):
    return {"symbol": symbol, "side": side, "positionSide": position_side,
            "qty": "1", "qtyExact": "1", "avgPrice": "100", "notional": "100",
            "startTime": start, "endTime": end, "time": end,
            "items": [{"orderTime": start, "time": end, "qty": 1, "price": 100}]}


async def _ordering(t):
    snapshot = current_snapshot("LONG")
    rows = (await t.db.scalars(select(BindingDelivery).where(
        BindingDelivery.binding_id == t.binding.id))).all()
    return await t.router._position_ordering(
        t.db, t.binding, rows, snapshot, {"BTCUSDT_LONG": Decimal("1")},
        received_at_ms=NOW_MS)


async def test_one_ms_sequence_gap_does_not_poison_position(trading):
    t = trading
    start = NOW_MS - 150_000
    payload = _delivery_payload("BTCUSDT", "BUY", "LONG", start, start + 1)
    t.db.add(BindingDelivery(binding_id=t.binding.id, source_event_id="evt-1ms",
                             source_sequence=start, payload_json=json.dumps(payload), state="APPLIED"))
    await t.db.commit()
    states = await _ordering(t)
    assert states[("BTCUSDT", "LONG")].review_reason is None


async def test_stale_review_reason_clears_when_evidence_recovers(trading):
    t = trading
    start = NOW_MS - 150_000
    t.db.add(BindingPositionProgress(
        binding_id=t.binding.id, symbol="BTCUSDT", position_side="LONG",
        source_time_ms=start, source_event_id="evt-clean", cycle_tracked=True,
        review_reason="Late or equal source event conflicts with persisted position ordering"))
    payload = _delivery_payload("BTCUSDT", "BUY", "LONG", start, start)
    t.db.add(BindingDelivery(binding_id=t.binding.id, source_event_id="evt-clean",
                             source_sequence=start, payload_json=json.dumps(payload), state="APPLIED"))
    await t.db.commit()
    states = await _ordering(t)
    assert states[("BTCUSDT", "LONG")].review_reason is None
    row = await t.db.get(BindingPositionProgress, (t.binding.id, "BTCUSDT", "LONG"))
    assert row.review_reason is None


async def test_bad_evidence_reestablishes_review_reason(trading):
    t = trading
    start = NOW_MS - 150_000
    t.db.add(BindingPositionProgress(
        binding_id=t.binding.id, symbol="BTCUSDT", position_side="LONG",
        source_time_ms=start, source_event_id="evt-watermark", cycle_tracked=True))
    payload = _delivery_payload("BTCUSDT", "SELL", "LONG", start - 5000, start - 5000)
    t.db.add(BindingDelivery(binding_id=t.binding.id, source_event_id="evt-late",
                             source_sequence=start - 5000, payload_json=json.dumps(payload), state="PENDING"))
    await t.db.commit()
    states = await _ordering(t)
    assert states[("BTCUSDT", "LONG")].review_reason == (
        "Late or equal source event conflicts with persisted position ordering")


def _old_intent(t, **overrides):
    fields = dict(id="mqdeadbeef", binding_id=t.binding.id, account_id=t.account.id,
                  inst_id="BTCUSDT", pos_side="long", source_event_id="evt-open",
                  state="UNKNOWN", requested_qty="1", filled_qty="0", avg_px="0",
                  reduce_only=False, target_usd="100", source_time_ms=NOW_MS - 600_000,
                  exchange_order_id=None,
                  created_at=datetime.now(timezone.utc) - timedelta(hours=1))
    fields.update(overrides)
    intent = OrderIntent(**fields)
    t.db.add(intent)
    return intent


async def test_unresolvable_intent_cancels_after_grace_window(trading):
    t = trading
    exchange = OfflineExchange()
    intent = _old_intent(t)
    await t.db.commit()
    assert await t.router.reconcile_pending_orders(t.db, t.account, exchange, {"BTCUSDT": t.spec})
    await t.db.refresh(intent)
    assert intent.state == "CANCELED"
    record = await t.db.scalar(select(ExecutionRecord))
    assert record.status == "CANCELED_ORDER_NOT_FOUND"


async def test_young_unresolvable_intent_keeps_waiting(trading):
    t = trading
    exchange = OfflineExchange()
    intent = _old_intent(t, created_at=datetime.now(timezone.utc))
    await t.db.commit()
    assert not await t.router.reconcile_pending_orders(t.db, t.account, exchange, {"BTCUSDT": t.spec})
    await t.db.refresh(intent)
    assert intent.state == "UNKNOWN"
    assert (await t.db.scalars(select(ExecutionRecord))).all() == []


async def test_intent_with_exchange_order_id_never_auto_cancels(trading):
    t = trading
    exchange = OfflineExchange()
    intent = _old_intent(t, exchange_order_id="999")
    await t.db.commit()
    assert not await t.router.reconcile_pending_orders(t.db, t.account, exchange, {"BTCUSDT": t.spec})
    await t.db.refresh(intent)
    assert intent.state == "UNKNOWN"


def _deterministic_cid(t, event):
    material = f"{t.account.id}:{t.binding.id}:{event}"
    return "mq" + hashlib.sha256(material.encode()).hexdigest()[:30]


async def _submit_open(t, exchange, event):
    return await t.router._submit_confirmed(
        t.db, t.binding, t.account, exchange, t.spec, source_event_id=event,
        target_usd=100, source_time_ms=NOW_MS - 150_000, observed_at_ms=0,
        inst_id="BTCUSDT", side="buy", pos_side="long", ord_type="market",
        sz="1", px="100", reduce_only=False, td_mode="cross", retry_terminal=True)


async def test_canceled_dead_intent_redrives_with_fresh_client_id(trading):
    t = trading
    exchange = OfflineExchange()
    event = "evt-open"
    t.db.add(OrderIntent(id=_deterministic_cid(t, event), binding_id=t.binding.id,
                         account_id=t.account.id, inst_id="BTCUSDT", pos_side="long",
                         source_event_id=event, state="CANCELED", requested_qty="1",
                         filled_qty="0", avg_px="0", reduce_only=False, target_usd="100",
                         source_time_ms=NOW_MS - 150_000, exchange_order_id=None))
    await t.db.commit()
    result = await _submit_open(t, exchange, event)
    assert len(exchange.orders) == 1
    assert exchange.orders[0]["cl_ord_id"] != _deterministic_cid(t, event)
    assert result.state == "filled"
    retry = await t.db.get(OrderIntent, exchange.orders[0]["cl_ord_id"])
    assert retry.delivery_event_id == event
    assert retry.source_event_id.startswith("retry:")


async def test_filled_intent_never_redrives_open(trading):
    t = trading
    exchange = OfflineExchange()
    event = "evt-open"
    t.db.add(OrderIntent(id=_deterministic_cid(t, event), binding_id=t.binding.id,
                         account_id=t.account.id, inst_id="BTCUSDT", pos_side="long",
                         source_event_id=event, state="FILLED", requested_qty="1",
                         filled_qty="1", avg_px="100", reduce_only=False, target_usd="100",
                         source_time_ms=NOW_MS - 150_000, exchange_order_id="exchange-id"))
    await t.db.commit()
    result = await _submit_open(t, exchange, event)
    assert exchange.orders == []
    assert result.state == "filled"


async def test_rejected_intent_never_redrives_open(trading):
    t = trading
    exchange = OfflineExchange()
    event = "evt-open"
    t.db.add(OrderIntent(id=_deterministic_cid(t, event), binding_id=t.binding.id,
                         account_id=t.account.id, inst_id="BTCUSDT", pos_side="long",
                         source_event_id=event, state="REJECTED", requested_qty="1",
                         filled_qty="0", avg_px="0", reduce_only=False, target_usd="100",
                         source_time_ms=NOW_MS - 150_000, exchange_order_id=None))
    await t.db.commit()
    result = await _submit_open(t, exchange, event)
    assert exchange.orders == []
    assert result.state == "rejected"


async def test_open_retry_stops_after_three_dead_attempts(trading):
    t = trading
    exchange = OfflineExchange()
    event = "evt-open"
    t.db.add(OrderIntent(id=_deterministic_cid(t, event), binding_id=t.binding.id,
                         account_id=t.account.id, inst_id="BTCUSDT", pos_side="long",
                         source_event_id=event, state="CANCELED", requested_qty="1",
                         filled_qty="0", avg_px="0", reduce_only=False, target_usd="100",
                         source_time_ms=NOW_MS - 150_000, exchange_order_id=None))
    for index in range(3):
        t.db.add(OrderIntent(id=f"mqretry{index}", binding_id=t.binding.id,
                             account_id=t.account.id, inst_id="BTCUSDT", pos_side="long",
                             source_event_id=f"retry:mqretry{index}", delivery_event_id=event,
                             state="CANCELED", requested_qty="1", filled_qty="0", avg_px="0",
                             reduce_only=False, target_usd="100",
                             source_time_ms=NOW_MS - 150_000, exchange_order_id=None))
    await t.db.commit()
    await _submit_open(t, exchange, event)
    assert exchange.orders == []


def _review_delivery(t, event, side="SELL", position_side="LONG", sequence=None):
    start = sequence or NOW_MS - 150_000
    payload = _delivery_payload("BTCUSDT", side, position_side, start, start)
    delivery = BindingDelivery(binding_id=t.binding.id, source_event_id=event,
                               source_sequence=start, payload_json=json.dumps(payload),
                               state="PENDING", exit_progress="REVIEW_REQUIRED")
    t.db.add(delivery)
    return delivery


async def _reconcile(t, exchange):
    rows = (await t.db.scalars(select(BindingDelivery).where(
        BindingDelivery.binding_id == t.binding.id))).all()
    await t.router._reconcile_review_deliveries(
        t.db, t.binding, t.lead, t.account, exchange, {"BTCUSDT": t.spec}, rows, {})


async def test_review_close_with_filled_intents_reconciles(trading):
    t = trading
    exchange = OfflineExchange()
    t.account.positions_json = json.dumps([])
    t.db.add(PositionAllocation(binding_id=t.binding.id, account_id=t.account.id,
                                inst_id="BTCUSDT", pos_side="long", owned_qty=0, entry_cost=0))
    delivery = _review_delivery(t, "evt-close")
    t.db.add(OrderIntent(id="mqfilled", binding_id=t.binding.id, account_id=t.account.id,
                         inst_id="BTCUSDT", pos_side="long", source_event_id="evt-close",
                         state="FILLED", requested_qty="3.828", filled_qty="3.828",
                         avg_px="100", reduce_only=True, target_usd="382.8",
                         source_time_ms=NOW_MS - 150_000, exchange_order_id="exchange-id"))
    await t.db.commit()
    await _reconcile(t, exchange)
    await t.db.refresh(delivery)
    assert delivery.state == "APPLIED"
    assert delivery.exit_progress == "COMPLETE"
    assert delivery.exit_target_qty == "3.828"
    assert delivery.exit_filled_qty == "3.828"
    assert delivery.exit_remaining_qty == "0"
    record = await t.db.scalar(select(ExecutionRecord))
    assert record.status == "RECONCILED_REVIEW"


async def test_review_close_without_position_reconciles_as_noop(trading):
    t = trading
    exchange = OfflineExchange()
    t.account.positions_json = json.dumps([])
    delivery = _review_delivery(t, "evt-close")
    await t.db.commit()
    await _reconcile(t, exchange)
    await t.db.refresh(delivery)
    assert delivery.state == "APPLIED"
    assert delivery.exit_target_qty == "0"
    assert delivery.exit_filled_qty == "0"
    record = await t.db.scalar(select(ExecutionRecord))
    assert record.status == "RECONCILED_REVIEW"


async def test_review_close_with_owned_quantity_stays_in_review(trading):
    t = trading
    exchange = OfflineExchange()
    t.account.positions_json = json.dumps([])
    t.db.add(PositionAllocation(binding_id=t.binding.id, account_id=t.account.id,
                                inst_id="BTCUSDT", pos_side="long", owned_qty=1, entry_cost=100))
    delivery = _review_delivery(t, "evt-close")
    await t.db.commit()
    await _reconcile(t, exchange)
    await t.db.refresh(delivery)
    assert delivery.state == "PENDING"
    assert delivery.exit_progress == "REVIEW_REQUIRED"


async def test_review_close_with_exchange_position_stays_in_review(trading):
    t = trading
    exchange = OfflineExchange()
    t.account.positions_json = json.dumps([{"instId": "BTCUSDT", "posSide": "long", "pos": "1"}])
    delivery = _review_delivery(t, "evt-close")
    await t.db.commit()
    await _reconcile(t, exchange)
    await t.db.refresh(delivery)
    assert delivery.state == "PENDING"
    assert delivery.exit_progress == "REVIEW_REQUIRED"


async def test_review_close_with_pending_intent_stays_in_review(trading):
    t = trading
    exchange = OfflineExchange()
    t.account.positions_json = json.dumps([])
    delivery = _review_delivery(t, "evt-close")
    t.db.add(OrderIntent(id="mqpending", binding_id=t.binding.id, account_id=t.account.id,
                         inst_id="BTCUSDT", pos_side="long", source_event_id="evt-close",
                         state="UNKNOWN", requested_qty="1", filled_qty="0",
                         avg_px="0", reduce_only=True, target_usd="100",
                         source_time_ms=NOW_MS - 150_000, exchange_order_id=None))
    await t.db.commit()
    await _reconcile(t, exchange)
    await t.db.refresh(delivery)
    assert delivery.state == "PENDING"
    assert delivery.exit_progress == "REVIEW_REQUIRED"


async def test_review_close_with_partial_fill_stays_in_review(trading):
    t = trading
    exchange = OfflineExchange()
    t.account.positions_json = json.dumps([])
    delivery = _review_delivery(t, "evt-close")
    t.db.add(OrderIntent(id="mqpartial", binding_id=t.binding.id, account_id=t.account.id,
                         inst_id="BTCUSDT", pos_side="long", source_event_id="evt-close",
                         state="FILLED", requested_qty="2", filled_qty="1",
                         avg_px="100", reduce_only=True, target_usd="200",
                         source_time_ms=NOW_MS - 150_000, exchange_order_id="exchange-id"))
    await t.db.commit()
    await _reconcile(t, exchange)
    await t.db.refresh(delivery)
    assert delivery.state == "PENDING"
    assert delivery.exit_progress == "REVIEW_REQUIRED"


async def test_review_open_never_reconciles(trading):
    t = trading
    exchange = OfflineExchange()
    t.account.positions_json = json.dumps([])
    delivery = _review_delivery(t, "evt-open", side="BUY", position_side="LONG")
    await t.db.commit()
    await _reconcile(t, exchange)
    await t.db.refresh(delivery)
    assert delivery.state == "PENDING"
    assert delivery.exit_progress == "REVIEW_REQUIRED"


# ---- 1.1.4：通知回执缺口不再阻断跟单 ----
# 线上实况：5 笔已成交意图的账本记录齐全，但通知回执缺失或字段不全
# （回执 event_key 为空、或根本无回执行），审计每轮失败，账户开仓被
# 永久挂起。账本证实后审计即应通过，通知缺口只留痕、不停单。

def _filled_intent_with_record(t, intent_id, order_id, qty="1", price="100", state="FILLED",
                               notification_state="unknown"):
    t.db.add(OrderIntent(id=intent_id, binding_id=t.binding.id, account_id=t.account.id,
        inst_id="BTCUSDT", pos_side="long", source_event_id="audit-case", state=state,
        requested_qty=qty, filled_qty=qty, avg_px=price, reduce_only=False,
        exchange_order_id=order_id))
    record = ExecutionRecord(binding_id=t.binding.id, account_id=t.account.id, lead_id=t.lead.id,
        account_name=t.account.name, lead_name=t.lead.name, inst_id="BTCUSDT", symbol="BTCUSDT",
        side="BUY", position_side="LONG", action="OPEN_LONG", status="EXECUTED",
        order_id=order_id, created_at=datetime.now(timezone.utc),
        notification_state=notification_state, my_qty=float(qty), my_price=float(price),
        details=f"Filled {qty} @ {price}")
    t.db.add(record)
    return record


async def test_terminal_intent_with_ledger_proven_passes_audit_despite_notification_gap(trading):
    t = trading
    _filled_intent_with_record(t, "audit-terminal", "order-audit-1")
    await t.db.commit()
    intent = await t.db.get(OrderIntent, "audit-terminal")
    assert await t.router._recover_intent_reporting(t.db, intent, t.spec, require_notifications=True)
    assert await t.router._audit_completed_intents(t.db, t.account, {"BTCUSDT": t.spec})
    assert (await t.db.get(OrderIntent, "audit-terminal")).reporting_complete is True
    assert await t.router.reconcile_pending_orders(t.db, t.account, OfflineExchange(), {"BTCUSDT": t.spec})


async def test_terminal_intent_with_legacy_outbox_receipt_passes_audit(trading):
    t = trading
    record = _filled_intent_with_record(t, "audit-legacy-receipt", "order-audit-2")
    await t.db.flush()
    t.db.add(NotificationOutbox(payload=json.dumps({"execution_id": record.id, "event_key": None,
        "channels": ["outbox"], "delivered_channels": ["outbox"]}), status="sent",
        execution_id=record.id))
    await t.db.commit()
    intent = await t.db.get(OrderIntent, "audit-legacy-receipt")
    assert await t.router._recover_intent_reporting(t.db, intent, t.spec, require_notifications=True)
    assert await t.router.reconcile_pending_orders(t.db, t.account, OfflineExchange(), {"BTCUSDT": t.spec})


async def test_nonterminal_intent_with_notification_gap_still_blocks(trading):
    t = trading
    _filled_intent_with_record(t, "audit-nonterminal", "order-audit-3", state="UNKNOWN")
    await t.db.commit()
    intent = await t.db.get(OrderIntent, "audit-nonterminal")
    assert not await t.router._recover_intent_reporting(t.db, intent, t.spec, require_notifications=True)


async def test_terminal_intent_with_ledger_shortfall_still_blocks(trading):
    t = trading
    _filled_intent_with_record(t, "audit-shortfall", "order-audit-4", qty="1", price="100")
    intent = await t.db.get(OrderIntent, "audit-shortfall")
    await t.db.flush()
    intent.filled_qty = "2"
    await t.db.commit()
    exchange = OfflineExchange()
    assert not await t.router._recover_intent_reporting(t.db, intent, t.spec, require_notifications=True)
    assert exchange.orders == []
