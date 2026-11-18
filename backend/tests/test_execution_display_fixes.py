"""Execution display data: realized slippage, measured spread, sizing-priced debt.

Fill records used to store slippage 0.0 (only guard blocks populated the field),
never stored the orderbook spread, and settled carry-forward debt against the
fill average price -- so a favourably priced fill invented phantom debt.
"""
import json

import pytest
from sqlalchemy import select

from app.models.domain import BindingDebt, BindingDelivery, ExecutionRecord, OrderIntent, SystemConfig
from test_router_remediations import trading, OfflineExchange, NOW_MS  # noqa: F401


async def _source_delivery(t, event):
    payload = {"symbol": "BTCUSDT", "side": "BUY", "positionSide": "LONG", "qty": "1",
               "avgPrice": "100", "notional": "100", "startTime": NOW_MS - 1000}
    delivery = BindingDelivery(binding_id=t.binding.id, source_event_id=event,
                               source_sequence=1, payload_json=json.dumps(payload))
    t.db.add(delivery)
    notifications = await t.db.get(SystemConfig, "notifications")
    notifications.value_json = json.dumps({"outbox_enabled": True, "notify_on_execution": True})
    await t.db.commit()
    t.db.info["delivery"] = delivery
    return delivery


def _order(event, target_usd, sz):
    return dict(source_event_id=event, target_usd=target_usd, inst_id="BTCUSDT", side="buy",
                pos_side="long", ord_type="ioc", sz=sz, px="100", reduce_only=False, td_mode="cross")


async def test_fill_records_realized_slippage_spread_and_sizing_price(trading):
    t = trading
    await _source_delivery(t, "display-fill")
    exchange = OfflineExchange(fill=1, state="filled", price=99)
    await t.router._submit_confirmed(t.db, t.binding, t.account, exchange, t.spec,
                                     spread_pct=0.3812, sizing_px=100, **_order("display-fill", 100, "1"))
    record = await t.db.scalar(select(ExecutionRecord))
    assert record.status == "EXECUTED"
    assert record.slippage_pct == pytest.approx(1.0)
    assert record.spread_pct == pytest.approx(0.3812)
    intent = await t.db.scalar(select(OrderIntent))
    assert intent.sizing_px == "100"


async def test_favourable_fill_creates_no_phantom_debt(trading):
    t = trading
    await _source_delivery(t, "no-phantom-debt")
    exchange = OfflineExchange(fill=10, state="filled", price=99)
    await t.router._submit_confirmed(t.db, t.binding, t.account, exchange, t.spec,
                                     sizing_px=100, **_order("no-phantom-debt", 1000, "10"))
    debt = await t.db.scalar(select(BindingDebt))
    assert debt.pending_usd == 0
    assert debt.pending_usd_exact == "0"


async def test_legacy_intent_without_sizing_price_keeps_fill_price_residue(trading):
    t = trading
    await _source_delivery(t, "legacy-debt")
    exchange = OfflineExchange(fill=10, state="filled", price=99)
    await t.router._submit_confirmed(t.db, t.binding, t.account, exchange, t.spec,
                                     **_order("legacy-debt", 1000, "10"))
    debt = await t.db.scalar(select(BindingDebt))
    assert debt.pending_usd == pytest.approx(10.0)


async def test_partial_fill_debt_uses_sizing_price(trading):
    t = trading
    await _source_delivery(t, "partial-debt")
    exchange = OfflineExchange(fill=6, state="filled", price=99)
    await t.router._submit_confirmed(t.db, t.binding, t.account, exchange, t.spec,
                                     sizing_px=100, **_order("partial-debt", 1000, "10"))
    debt = await t.db.scalar(select(BindingDebt))
    assert debt.pending_usd == pytest.approx(400.0)


async def test_unfilled_order_records_no_slippage(trading):
    t = trading
    await _source_delivery(t, "unfilled")
    exchange = OfflineExchange(fill=0, state="canceled", price=99)
    await t.router._submit_confirmed(t.db, t.binding, t.account, exchange, t.spec,
                                     spread_pct=0.2, **_order("unfilled", 100, "1"))
    record = await t.db.scalar(select(ExecutionRecord))
    assert record.status == "UNFILLED"
    assert record.slippage_pct == 0.0
    assert record.spread_pct == pytest.approx(0.2)
