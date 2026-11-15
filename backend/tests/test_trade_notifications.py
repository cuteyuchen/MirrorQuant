"""Source-first messages and DingTalk receipts, entirely synthetic and offline."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.core.database import get_session_factory
from app.models.domain import BindingDelivery, ExecutionRecord, NotificationOutbox, OrderIntent, PositionAllocation
from app.services.notifier.outbox import NotificationOutboxDrainer
from app.services.notifier.service import NotificationService
from app.services.notifier.trade import trade_message
from test_notification_outbox import configure_sink, record_execution
from test_routing_deliveries import make_workflow, order, route
from test_auto_follow_hidden_positions import fixed_clock, hidden_snapshot, opening_order, workflow
from app.services.adapters.base import OrderBook


def message_record():
    return SimpleNamespace(symbol="BTCUSDT", position_side="LONG", side="BUY", action="OPEN_LONG",
        status="EXECUTED", lead_qty=2, lead_price=100, lead_notional=200,
        my_qty=1, my_price=101, my_notional=101, order_id="synthetic-order",
        account_id=1, binding_id=2)


@pytest.mark.parametrize("side,direction,label", [
    ("BUY", "LONG", "买入开多/加多"), ("SELL", "SHORT", "卖出开空/加空"),
    ("SELL", "LONG", "卖出平多/减多"), ("BUY", "SHORT", "买入平空/减空"),
])
def test_independent_lead_leg_precedes_different_follower_fill(side, direction, label):
    rec = message_record()
    source = dict(symbol="ZECUSDT", side=side, positionSide=direction, qtyExact="0.123456789123456789",
                  avgPrice="123.45", notionalExact="15.24148161729148160205")
    msg = trade_message(rec, SimpleNamespace(exchange="binance", environment="live"), source)
    lead, follower = msg.split("\n\n")
    assert label in lead and "ZECUSDT" in lead and direction in lead
    assert "0.123456789123456789" in lead and "123.45" in lead
    assert "15.24148161729148160205" in lead
    assert "✅ 已成交" in follower and "账户 #1 / 绑定 #2" in follower
    assert "跟单确认数量: 1 | 成交均价: 101 | 名义金额: 101" in follower
    assert "张" not in msg  # Lead units and follower contracts are not interchangeable.


@pytest.mark.parametrize("status,icon", [
    ("EXECUTED", "✅"), ("SKIPPED_UNSUPPORTED", "⏭️"), ("SKIPPED_NO_LOCAL_POS", "⏭️"),
    ("SKIPPED_CLOSED_CYCLE", "⏭️"), ("SKIPPED_BELOW_MIN", "⏭️"),
    ("BLOCKED_CATCHUP_DRIFT", "🛑"), ("BLOCKED_SPREAD", "🛑"), ("BLOCKED_SLIPPAGE", "🛑"),
    ("FAILED", "❌"), ("UNFILLED", "⚪"), ("UNKNOWN", "⚠️"), ("PARTIAL", "⚠️"),
    ("ACCUMULATED_DEBT", "⏳"),
])
async def test_account_outcomes_are_enqueued_without_sending(monkeypatch, status, icon):
    dispatch = AsyncMock()
    monkeypatch.setattr(NotificationService, "dispatch", dispatch)
    async with get_session_factory()() as db:
        router, binding, lead, account = await record_execution(db)
        rec = await router._record_execution(db, binding, lead, account, "BTCUSDT", "BTC-USDT-SWAP",
            "BUY", "LONG", "ADD_LONG", status, 2, 100, 200,
            my_qty=1 if status in ("EXECUTED", "PARTIAL") else 0,
            my_price=101, my_notional=101, order_id="synthetic-order",
            details="synthetic private diagnostic", notify_cfg={"dingtalk_enabled": True})
        row = await db.scalar(select(NotificationOutbox))
        payload = json.loads(row.payload)
        assert payload["execution_id"] == rec.id and payload["event_key"] == f"execution:{rec.id}"
        assert icon in payload["message"] and status in payload["message"]
        if status != "EXECUTED":
            assert "✅" not in payload["message"] and "已同步" not in payload["message"]
        assert "synthetic private diagnostic" not in row.payload
    dispatch.assert_not_awaited()


@pytest.mark.parametrize("qty,order_id", [(0, "synthetic-order"), (1, None), (-1, "synthetic-order"),
                                          (float("nan"), "synthetic-order")])
def test_execution_label_without_confirmed_order_quantity_never_claims_success(qty, order_id):
    rec = message_record()
    rec.my_qty, rec.order_id = qty, order_id
    msg = trade_message(rec, SimpleNamespace(exchange="okx", environment="live"))
    assert "✅" not in msg and "⚠️ 成交未确认" in msg


def test_unknown_lead_values_and_autonomous_follower_exit_are_not_fabricated():
    rec = message_record()
    rec.action, rec.lead_qty, rec.lead_price, rec.lead_notional = "TP_CLOSE", 0, 0, 0
    msg = trade_message(rec, SimpleNamespace(exchange="okx", environment="live"))
    assert "带单动作: 未知" in msg
    assert "带单成交均价: 未知 | 数量: 未知 | 名义金额: 未知" in msg
    header, follower = msg.split("\n\n")
    assert "BTCUSDT" not in header and "方向 未知" in header
    assert "BTCUSDT" in follower and "跟单账户" in follower
    assert "✅ 已成交" in msg  # The follower's confirmed fill remains accurately reported.
    source = dict(symbol="ZECUSDT", side="SELL", positionSide="LONG", qty="2", avgPrice=None)
    lead = trade_message(rec, SimpleNamespace(exchange="okx", environment="live"), source).split("\n\n")[0]
    assert "卖出平多/减多" in lead and "均价: 未知 | 数量: 2 | 名义金额: 未知" in lead


def test_historical_close_does_not_use_peak_quantity_or_follower_amount():
    source = {"_closed": {"symbol": "ZECUSDT", "side": "SHORT", "avgClosePrice": "123.45", "maxQty": 99}}
    lead = trade_message(message_record(), SimpleNamespace(exchange="okx", environment="live"), source).split("\n\n")[0]
    assert "平仓（历史持仓记录）" in lead and "方向 SHORT" in lead
    assert "均价: 123.45 | 数量: 未知 | 名义金额: 未知" in lead


def test_trade_payload_excludes_names_errors_urls_and_order_ids():
    rec = message_record()
    rec.lead_name = rec.account_name = rec.details = rec.order_id = "synthetic-sensitive-marker"
    msg = trade_message(rec, SimpleNamespace(exchange="synthetic-sensitive-marker", environment="private"))
    assert "synthetic-sensitive-marker" not in msg and "private" not in msg
    assert "环境未知" in msg and "模拟盘未上线" not in msg


@pytest.mark.parametrize("independent_fills", [1, 2])
async def test_fanout_keeps_source_identity_and_does_not_duplicate_messages(monkeypatch, independent_fills):
    async with get_session_factory()() as db:
        lead, bindings, _, calls, router = await make_workflow(db, monkeypatch, (True, True, True))
        await configure_sink(db, dingtalk_enabled=True)
        outcomes = ["EXECUTED", "BLOCKED_SPREAD", "SKIPPED_UNSUPPORTED"]

        async def record(db, binding, lead, account, adapter, batch, *args):
            index = next(i for i, pair in enumerate(bindings) if pair[0].id == binding.id)
            # A mapped follower close must not replace the independent lead's opening action.
            await router._record_execution(db, binding, lead, account, "MAPPEDUSDT", "MAPPEDUSDT",
                "SELL", "LONG", "FULL_CLOSE", outcomes[index], 0, 0, 0,
                my_qty=1 if index == 0 else 0, my_price=102, my_notional=102,
                order_id="synthetic-order" if index == 0 else None,
                notify_cfg={"dingtalk_enabled": True})

        monkeypatch.setattr(router, "_execute_batch_for_binding", record)
        orders = [order(orderId=f"independent-{i}") for i in range(independent_fills)]
        await route(db, router, lead, orders)
        await route(db, router, lead, orders)
        rows = (await db.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id))).all()
        assert len(rows) == 3 * independent_fills
        assert len((await db.scalars(select(ExecutionRecord))).all()) == 3 * independent_fills
        assert all(d.state == "APPLIED" for d in (await db.scalars(select(BindingDelivery))).all())
        headers = [json.loads(r.payload)["message"].split("\n\n")[0] for r in rows]
        assert len(set(headers)) == 1 and "买入开多/加多" in headers[0] and "BTCUSDT" in headers[0]
        assert "数量: 0.1 | 名义金额: 10.0" in headers[0]
        assert [json.loads(r.payload)["event_type"] for r in rows] == [o for o in outcomes for _ in range(independent_fills)]


@pytest.mark.parametrize("outcome", ["EXECUTED", "UNFILLED", "BLOCKED_CATCHUP_DRIFT", "BLOCKED_SPREAD", "SKIPPED_UNSUPPORTED"])
async def test_actual_filled_unfilled_and_risk_paths_keep_terminal_states(workflow, outcome):
    t = workflow
    await configure_sink(t.db, dingtalk_enabled=True)
    if outcome == "BLOCKED_CATCHUP_DRIFT":
        t.adapter.get_orderbook.return_value = OrderBook([[100.9, 100]], [[101, 100]])
    elif outcome == "BLOCKED_SPREAD":
        t.adapter.get_orderbook.return_value = OrderBook([[99.5, 100]], [[100, 100]])
    elif outcome == "SKIPPED_UNSUPPORTED":
        t.adapter.get_instruments.return_value = {}
    elif outcome == "UNFILLED":
        t.adapter.get_order.side_effect = None
        t.adapter.get_order.return_value = {"ordId": "synthetic-order", "accFillSz": "0", "avgPx": "0", "state": "canceled"}
    snapshot = hidden_snapshot()
    t.lead.snapshot_json = json.dumps(snapshot)
    await t.db.commit()
    await t.router.process_lead_events(t.db, t.lead, [opening_order()], [], snapshot, [t.proxy])
    await t.router.process_lead_events(t.db, t.lead, [opening_order()], [], snapshot, [t.proxy])
    rec = await t.db.scalar(select(ExecutionRecord))
    assert rec.status == outcome
    rows = (await t.db.scalars(select(NotificationOutbox))).all()
    assert len(rows) == 1
    msg = json.loads(rows[0].payload)["message"]
    assert "买入开多/加多" in msg and "带单成交均价: 100 | 数量: 1 | 名义金额: 100" in msg
    assert ("✅" in msg) is (outcome == "EXECUTED")
    assert all(d.state == "APPLIED" for d in (await t.db.scalars(select(BindingDelivery))).all())
    if outcome.startswith(("BLOCKED", "SKIPPED")):
        t.adapter.place_order.assert_not_awaited()
    else:
        t.adapter.place_order.assert_awaited_once()


async def test_partial_fill_and_later_failure_never_claim_full_success(workflow):
    t = workflow
    await configure_sink(t.db, dingtalk_enabled=True)
    t.adapter.get_order.side_effect = None
    t.adapter.get_order.return_value = {"ordId": "synthetic-order", "accFillSz": "0.5", "avgPx": "100", "state": "partially_filled"}
    snapshot = hidden_snapshot()
    t.lead.snapshot_json = json.dumps(snapshot)
    await t.db.commit()
    await t.router.process_lead_events(t.db, t.lead, [opening_order()], [], snapshot, [t.proxy])
    await t.router.process_lead_events(t.db, t.lead, [opening_order()], [], snapshot, [t.proxy])
    rows = (await t.db.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id))).all()
    assert [json.loads(r.payload)["event_type"] for r in rows] == ["PARTIAL", "FAILED"]
    partial = json.loads(rows[0].payload)["message"]
    assert "⚠️ 部分成交" in partial and "跟单确认数量: 0.5" in partial
    assert all("✅" not in json.loads(r.payload)["message"] for r in rows)
    assert (await t.db.scalar(select(BindingDelivery))).state == "PENDING"
    assert (await t.db.scalar(select(OrderIntent))).state == "UNKNOWN"
    assert (await t.db.scalar(select(PositionAllocation))).owned_qty == 0.5
    t.adapter.place_order.assert_awaited_once()


@pytest.mark.parametrize("status", ["EXECUTED", "UNFILLED", "PARTIAL", "UNKNOWN", "BLOCKED_SPREAD", "SKIPPED_UNSUPPORTED", "ACCUMULATED_DEBT"])
async def test_disabled_notification_preferences_remain_respected(status):
    async with get_session_factory()() as db:
        router, binding, lead, account = await record_execution(db)
        await router._record_execution(db, binding, lead, account, "BTCUSDT", "BTCUSDT", "BUY", "LONG",
            "OPEN_LONG", status, 1, 100, 100,
            notify_cfg={"dingtalk_enabled": True, "notify_on_execution": False, "notify_on_risk_block": False})
        assert not (await db.scalars(select(NotificationOutbox))).all()


@pytest.mark.parametrize("http_status,body,ok", [
    (200, {"errcode": 0}, True), (201, {"errcode": 0}, True),
    (200, {"errcode": 310000, "errmsg": "synthetic-sensitive-marker"}, False),
    (200, {}, False), (200, {"errcode": "0"}, False), (200, {"errcode": False}, False),
    (200, [], False), (302, {"errcode": 0}, False), (500, {"errcode": 0}, False),
    (200, "invalid-json", False),
])
async def test_dingtalk_requires_http_and_business_success(monkeypatch, http_status, body, ok):
    real_client = httpx.AsyncClient

    def respond(request):
        assert json.loads(request.content)["text"]["content"] == "[MirrorQuant]\n📣 synthetic lead\n\n🛑 synthetic block"
        return httpx.Response(http_status, text=body) if isinstance(body, str) else httpx.Response(http_status, json=body)

    monkeypatch.setattr("app.services.notifier.service.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    result = await NotificationService.dispatch({"dingtalk_enabled": True,
        "dingtalk_webhook_url": "https://example.test/synthetic"}, "MirrorQuant", "📣 synthetic lead\n\n🛑 synthetic block")
    assert result["dingtalk"]["ok"] is ok
    assert "synthetic-sensitive-marker" not in json.dumps(result)


@pytest.mark.parametrize("eventual_success", [True, False])
async def test_dingtalk_business_failure_retries_without_resending_successful_channels(monkeypatch, eventual_success):
    from app.services.notifier.outbox import enqueue_notification

    real_client, requests = httpx.AsyncClient, []

    def respond(request):
        requests.append(request.url.path)
        if request.url.path == "/ding":
            attempts = requests.count("/ding")
            return httpx.Response(200, json={"errcode": 0 if attempts > 1 and eventual_success else 310000,
                                          "errmsg": "synthetic-sensitive-marker"})
        return httpx.Response(200)

    monkeypatch.setattr("app.services.notifier.service.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    async with get_session_factory()() as db:
        await configure_sink(db, dingtalk_enabled=True, dingtalk_webhook_url="https://example.test/ding")
        row = enqueue_notification(db, {"webhook_enabled": True, "dingtalk_enabled": True}, "title", "body")
        await db.commit()
        row_id = row.id
    drainer = NotificationOutboxDrainer(max_attempts=2)
    assert await drainer.drain_once() == 1
    async with get_session_factory()() as db:
        row = await db.get(NotificationOutbox, row_id)
        assert row.status == "pending" and json.loads(row.payload)["delivered_channels"] == ["webhook"]
        assert row.last_error == "Delivery failed for channels: dingtalk"
    assert await drainer.drain_once(ignore_backoff=True) == 1
    assert await drainer.drain_once() == 0
    async with get_session_factory()() as db:
        row = await db.get(NotificationOutbox, row_id)
        assert row.status == ("sent" if eventual_success else "failed") and row.attempts == 2
        assert json.loads(row.payload)["delivered_channels"] == (["dingtalk", "webhook"] if eventual_success else ["webhook"])
    assert requests.count("/notify") == 1 and requests.count("/ding") == 2
