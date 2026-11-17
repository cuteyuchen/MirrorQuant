"""Source-first messages and DingTalk receipts, entirely synthetic and offline."""
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import func, select

from app.core.database import get_session_factory
from app.models.domain import BindingDelivery, ExecutionRecord, NotificationOutbox, OrderIntent, PositionAllocation
from app.services.notifier.outbox import NotificationOutboxDrainer
from app.services.notifier.service import NotificationService
from app.services.notifier.trade import trade_message
from app.services.engine.router import lead_position_context
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
    ("BUY", "LONG", "🟢 开多/加多"), ("SELL", "SHORT", "🔴 开空/加空"),
    ("SELL", "LONG", "🟢 平多/减多"), ("BUY", "SHORT", "🔴 平空/减空"),
])
def test_independent_lead_leg_precedes_different_follower_fill(side, direction, label):
    rec = message_record()
    source = dict(symbol="ZECUSDT", side=side, positionSide=direction, qtyExact="0.123456789123456789",
                  avgPrice="123.45", notionalExact="15.24148161729148160205")
    msg = trade_message(rec, SimpleNamespace(exchange="binance", environment="live"), source)
    lead, follower = msg.split("\n\n")
    assert label in lead and "ZECUSDT" in lead and {"LONG": "做多", "SHORT": "做空"}[direction] in lead
    assert "0.123456789123456789" in lead and "123.45" in lead
    assert "金额 15.24 U" in lead  # 金额统一两位小数，价格与数量保留原始精度。
    assert "1000U" not in lead  # 没有带单员保证金余额时不估算折算仓位。
    assert "✅ 已成交" in follower and "账户 #1 / 绑定 #2" in follower
    assert "📦 数量 1 ｜ 均价 101 ｜ 金额 101.00 U" in follower
    assert "张" not in msg  # Lead units and follower contracts are not interchangeable.


@pytest.mark.parametrize("status,icon", [
    ("EXECUTED", "✅"), ("SKIPPED_UNSUPPORTED", "⏭️"), ("SKIPPED_NO_LOCAL_POS", "⏭️"),
    ("SKIPPED_CLOSED_CYCLE", "⏭️"), ("SKIPPED_BELOW_MIN", "⏭️"),
    ("BLOCKED_CATCHUP_DRIFT", "🛑"), ("BLOCKED_SPREAD", "🛑"), ("BLOCKED_SLIPPAGE", "🛑"),
    ("FAILED", "❌"), ("UNFILLED", "⚪"), ("PARTIAL", "⚠️"),
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
    assert "❔ 未知 ｜ 未知 永续 ｜ 方向未知" in msg
    assert "均价 未知 ｜ 数量 未知 ｜ 金额 未知" in msg
    header, follower = msg.split("\n\n")
    assert "BTCUSDT" not in header and "方向未知" in header and "1000U" not in header
    assert "BTCUSDT" in follower and "跟单账户 #1" in follower
    assert "✅ 已成交" in msg  # The follower's confirmed fill remains accurately reported.
    source = dict(symbol="ZECUSDT", side="SELL", positionSide="LONG", qty="2", avgPrice=None)
    lead = trade_message(rec, SimpleNamespace(exchange="okx", environment="live"), source).split("\n\n")[0]
    assert "平多/减多" in lead and "均价 未知 ｜ 数量 2 ｜ 金额 未知" in lead


@pytest.mark.parametrize("side,direction,margin,expected", [
    ("BUY", "LONG", 4000, "🧮 1000U 本金 ≈ 开 50.00 U ｜ 建议杠杆 2x"),  # 200/4000，即 5% 仓位
    ("SELL", "SHORT", 100, "🧮 1000U 本金 ≈ 开 2,000.00 U ｜ 建议杠杆 40x"),  # 2 倍名义对应 40x
    ("BUY", "LONG", 10, "建议杠杆 50x"),  # 杠杆上限 50x
    ("SELL", "LONG", 4000, None), ("BUY", "SHORT", 4000, None),  # 平仓不给折算与杠杆
    ("BUY", "LONG", 0, None), ("BUY", "LONG", None, None), ("BUY", "LONG", float("nan"), None),
])
def test_reference_capital_line_only_for_opening_legs_with_known_margin(side, direction, margin, expected):
    source = dict(symbol="BTCUSDT", side=side, positionSide=direction, qty="2", avgPrice="100", notional="200")
    msg = trade_message(message_record(), SimpleNamespace(exchange="okx", environment="live"), source, margin)
    lead = msg.split("\n\n")[0]
    if expected is None:
        assert "1000U" not in lead and "杠杆" not in lead
    else:
        assert expected in lead


def test_historical_close_does_not_use_peak_quantity_or_follower_amount():
    source = {"_closed": {"symbol": "ZECUSDT", "side": "SHORT", "avgClosePrice": "123.45", "maxQty": 99}}
    lead = trade_message(message_record(), SimpleNamespace(exchange="okx", environment="live"), source).split("\n\n")[0]
    assert "平空（历史持仓记录）" in lead and "🔴" in lead and "做空" in lead
    assert "均价 123.45 ｜ 数量 未知 ｜ 金额 未知" in lead


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
        assert len(set(headers)) == 1 and "加多" in headers[0] and "BTCUSDT" in headers[0]
        assert "【原仓位】 9.9 → 【现仓位】 10" in headers[0]
        assert "数量 0.1 ｜ 金额 10.00 U" in headers[0]
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
    assert "开多" in msg and "均价 100 ｜ 数量 1 ｜ 金额 100.00 U" in msg
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
    assert "⚠️ 部分成交" in partial and "📦 数量 0.5" in partial
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


@pytest.mark.parametrize("secret", ["SYNTHETIC_SIGNING_SECRET", "", None])
async def test_dingtalk_signs_only_when_a_secret_is_configured(monkeypatch, secret):
    """加签密钥只参与签名，绝不出现在请求地址、正文或返回结果里。"""
    import base64, hashlib, hmac
    from urllib.parse import parse_qs, urlsplit
    real_client = httpx.AsyncClient
    seen = []

    def respond(request):
        seen.append(str(request.url))
        assert "SYNTHETIC_SIGNING_SECRET" not in str(request.url) + request.content.decode()
        return httpx.Response(200, json={"errcode": 0})

    monkeypatch.setattr("app.services.notifier.service.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    result = await NotificationService.dispatch({"dingtalk_enabled": True, "dingtalk_secret": secret,
        "dingtalk_webhook_url": "https://example.test/robot/send?access_token=synthetic"}, "MirrorQuant", "synthetic")
    assert result["dingtalk"] == {"ok": True, "status": 200, "errcode": 0}
    query = parse_qs(urlsplit(seen[0]).query)
    assert query["access_token"] == ["synthetic"]
    if not secret:
        assert "sign" not in query and "timestamp" not in query
        return
    timestamp = query["timestamp"][0]
    expected = base64.b64encode(hmac.new(secret.encode(), f"{timestamp}\n{secret}".encode(), hashlib.sha256).digest()).decode()
    assert query["sign"] == [expected] and abs(int(timestamp) - __import__("time").time() * 1000) < 60_000


async def test_wecom_reports_the_platform_receipt_code(monkeypatch):
    real_client = httpx.AsyncClient
    monkeypatch.setattr("app.services.notifier.service.httpx.AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"errcode": 93000, "errmsg": "synthetic-sensitive-marker"})), **kwargs))
    result = await NotificationService.dispatch({"wecom_enabled": True,
        "wecom_webhook_url": "https://example.test/wecom"}, "MirrorQuant", "synthetic")
    assert result["wecom"] == {"ok": False, "status": 200, "errcode": 93000}
    assert "synthetic-sensitive-marker" not in json.dumps(result)



def test_lead_header_and_precise_action_use_position_context():
    rec = message_record()
    rec.lead_name = "熬鹰资本"
    source = dict(symbol="NVDAUSDT", side="BUY", positionSide="LONG", qtyExact="604.12",
                  avgPrice="160.51", notionalExact="96967.00", endTime=1794843319000)
    lead = SimpleNamespace(name="熬鹰资本", platform="binance_bapi")
    position = {"previous_qty": Decimal("3803.17"), "current_qty": Decimal("4407.29"), "open_count": 1}
    msg = trade_message(rec, SimpleNamespace(exchange="binance", environment="live"), source,
                        355911.79, lead=lead, position=position)
    assert msg.split("\n")[0] == "【熬鹰资本 · 币安】"
    assert "🟢 加多 ｜ NVDAUSDT 永续 ｜ 做多" in msg
    assert "【原仓位】 3803.17 → 【现仓位】 4407.29" in msg
    assert "🕐 带单时间 2026/11/16 23:35:19（北京时间）" in msg
    assert "【持仓快照】 当前持仓 1 个 ｜ 账户保证金 355,911.79 U" in msg


@pytest.mark.parametrize("previous,current,side,direction,expected", [
    (0, 604.12, "BUY", "LONG", "开多"),
    (3803.17, 4407.29, "BUY", "LONG", "加多"),
    (4407.29, 3803.17, "SELL", "LONG", "减多"),
    (604.12, 0, "SELL", "LONG", "平多"),
    (0, 10, "SELL", "SHORT", "开空"),
    (10, 20, "SELL", "SHORT", "加空"),
    (20, 10, "BUY", "SHORT", "减空"),
    (10, 0, "BUY", "SHORT", "平空"),
])
def test_precise_action_labels(previous, current, side, direction, expected):
    source = dict(symbol="NVDAUSDT", side=side, positionSide=direction, qtyExact="1",
                  avgPrice="100", notionalExact="100")
    position = {"previous_qty": previous, "current_qty": current, "open_count": 1}
    msg = trade_message(message_record(), SimpleNamespace(exchange="binance", environment="live"),
                        source, position=position)
    assert expected in msg.split("\n")[1]


def test_lead_name_is_sanitized_to_a_single_line():
    lead = SimpleNamespace(name="evil\n【伪造】\x00lead-name-that-is-way-too-long-to-show", platform="unknown_platform")
    msg = trade_message(message_record(), SimpleNamespace(exchange="binance", environment="live"),
                        dict(symbol="BTCUSDT", side="BUY", positionSide="LONG", qtyExact="1",
                             avgPrice="100", notionalExact="100"), lead=lead)
    header = msg.split("\n")[0]
    assert "\n" not in header and "\x00" not in header
    assert header.startswith("【") and header.endswith("】")
    assert "币安" not in header
    assert len(header) <= 26


def test_lead_position_context_recovers_inventory_from_snapshot():
    payload = {"symbol": "NVDAUSDT", "positionSide": "LONG", "side": "BUY", "qtyExact": "604.12"}
    snapshot = {"openPositions": [
        {"symbol": "NVDAUSDT", "positionSide": "LONG", "qtyExact": "4407.29"},
        {"symbol": "BTCUSDT", "positionSide": "SHORT", "qtyExact": "0.5"},
    ]}
    db = SimpleNamespace(info={"delivery": SimpleNamespace(payload_json=json.dumps(payload)),
                               "snapshot_admission": (None, snapshot)})
    context = lead_position_context(db)
    assert context == {"previous_qty": Decimal("3803.17"), "current_qty": Decimal("4407.29"), "open_count": 2}


def test_lead_position_context_close_and_guards():
    payload = {"symbol": "NVDAUSDT", "positionSide": "LONG", "side": "SELL", "qtyExact": "604.12"}
    snapshot = {"openPositions": [{"symbol": "NVDAUSDT", "positionSide": "LONG", "qtyExact": "3803.17"}]}
    db = SimpleNamespace(info={"delivery": SimpleNamespace(payload_json=json.dumps(payload)),
                               "snapshot_admission": (None, snapshot)})
    context = lead_position_context(db)
    assert context["previous_qty"] == Decimal("4407.29") and context["current_qty"] == Decimal("3803.17")
    closed = SimpleNamespace(info={"delivery": SimpleNamespace(payload_json=json.dumps({"_closed": payload})),
                                   "snapshot_admission": (None, snapshot)})
    assert lead_position_context(closed) is None
    assert lead_position_context(SimpleNamespace(info={})) is None
    open_payload = {"symbol": "NVDAUSDT", "positionSide": "LONG", "side": "BUY", "qtyExact": "604.12"}
    empty = SimpleNamespace(info={"delivery": SimpleNamespace(payload_json=json.dumps(open_payload)),
                                  "snapshot_admission": (None, {"openPositions": []})})
    assert lead_position_context(empty) is None


async def test_unknown_outcome_is_recorded_without_notification(monkeypatch):
    # UNKNOWN 是未定结论：记账但不推送，下一轮评估出确定结论后再推，避免重启刷屏。
    dispatch = AsyncMock()
    monkeypatch.setattr(NotificationService, "dispatch", dispatch)
    async with get_session_factory()() as db:
        router, binding, lead, account = await record_execution(db)
        rec = await router._record_execution(db, binding, lead, account, "BTCUSDT", "BTC-USDT-SWAP",
            "BUY", "LONG", "ADD_LONG", "UNKNOWN", 2, 100, 200,
            my_qty=0, my_price=101, my_notional=0, order_id=None,
            details="Account has unresolved orders; increases suspended",
            notify_cfg={"dingtalk_enabled": True})
        assert rec.notification_state == "not_required"
        assert await db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0
    dispatch.assert_not_awaited()


async def _delivery_for(db, binding, event_id):
    delivery = BindingDelivery(
        binding_id=binding.id, source_event_id=event_id, source_sequence=1,
        payload_json=json.dumps({"symbol": "NVDAUSDT", "side": "BUY", "positionSide": "LONG",
                                 "qty": "604.12", "avgPrice": "160.5070381",
                                 "notional": "96965.51", "_event_id": event_id,
                                 "startTime": 1, "endTime": 1}),
        state="PENDING")
    db.add(delivery)
    await db.flush()
    db.info["delivery"] = delivery
    return delivery


async def _guard_record(router, db, binding, lead, account, status, details):
    return await router._record_execution(db, binding, lead, account, "NVDAUSDT", "NVDAUSDT",
        "BUY", "LONG", "ADD_LONG", status, 604.12, 160.5070381, 96965.51,
        my_qty=0, my_price=157.0, my_notional=0, order_id=None,
        details=details, notify_cfg={"dingtalk_enabled": True})


async def test_guard_outcome_notifies_once_per_delivery_event(monkeypatch):
    # 复现线上：同一陈旧投递在 1.1.3 周期判 UNKNOWN、1.1.4 周期判 BLOCKED，
    # 两次重启各推一条内容几乎相同的消息。确定结论只应推送一次。
    dispatch = AsyncMock()
    monkeypatch.setattr(NotificationService, "dispatch", dispatch)
    async with get_session_factory()() as db:
        router, binding, lead, account = await record_execution(db)
        await _delivery_for(db, binding, "evt-drift-1")
        first = await _guard_record(router, db, binding, lead, account, "BLOCKED_SLIPPAGE",
                                    "Slippage 1.72% > max 1.00%")
        second = await _guard_record(router, db, binding, lead, account, "BLOCKED_CATCHUP_DRIFT",
                                     "Catch-up price drift 1.72% > max 0.50% (age 39149s)")
        assert first.notification_state == "pending" and second.notification_state == "not_required"
        assert await db.scalar(select(func.count()).select_from(NotificationOutbox)) == 1
        assert await db.scalar(select(func.count()).select_from(ExecutionRecord)) == 2
    dispatch.assert_not_awaited()


async def test_guard_dedup_does_not_block_later_execution_notice(monkeypatch):
    dispatch = AsyncMock()
    monkeypatch.setattr(NotificationService, "dispatch", dispatch)
    async with get_session_factory()() as db:
        router, binding, lead, account = await record_execution(db)
        await _delivery_for(db, binding, "evt-drift-2")
        await _guard_record(router, db, binding, lead, account, "BLOCKED_CATCHUP_DRIFT",
                            "Catch-up price drift 1.72% > max 0.50% (age 39149s)")
        filled = await router._record_execution(db, binding, lead, account, "NVDAUSDT", "NVDAUSDT",
            "BUY", "LONG", "ADD_LONG", "EXECUTED", 604.12, 160.5070381, 96965.51,
            my_qty=16.19, my_price=157.0, my_notional=2541.83, order_id="synthetic-order",
            details="filled", notify_cfg={"dingtalk_enabled": True})
        assert filled.notification_state == "pending"
        assert await db.scalar(select(func.count()).select_from(NotificationOutbox)) == 2
    dispatch.assert_not_awaited()


async def test_guard_dedup_is_scoped_per_delivery_event(monkeypatch):
    dispatch = AsyncMock()
    monkeypatch.setattr(NotificationService, "dispatch", dispatch)
    async with get_session_factory()() as db:
        router, binding, lead, account = await record_execution(db)
        await _delivery_for(db, binding, "evt-a")
        await _guard_record(router, db, binding, lead, account, "BLOCKED_SLIPPAGE", "Slippage 2.00% > max 1.00%")
        await _delivery_for(db, binding, "evt-b")
        await _guard_record(router, db, binding, lead, account, "BLOCKED_SLIPPAGE", "Slippage 2.00% > max 1.00%")
        assert await db.scalar(select(func.count()).select_from(NotificationOutbox)) == 2
    dispatch.assert_not_awaited()


def test_block_reason_line_is_bilingual_with_parsed_numbers():
    rec = message_record()
    rec.status, rec.details = "BLOCKED_CATCHUP_DRIFT", "Catch-up price drift 1.72% > max 0.50% (age 39149s)"
    rec.my_qty, rec.order_id = 0, None
    msg = trade_message(rec, SimpleNamespace(exchange="binance", environment="demo"))
    assert "📋 原因：补跟价格漂移 1.72% 超过上限 0.50%（信号已过去 10 小时 52 分），未追单" in msg
    assert "Catch-up price drift 1.72% exceeded the 0.50% limit (signal age 10h 52m); order not placed" in msg
    assert "Catch-up price drift 1.72% > max 0.50% (age 39149s)" not in msg


def test_block_reason_falls_back_without_numbers_when_details_unparseable():
    rec = message_record()
    rec.status, rec.details = "BLOCKED_CATCHUP_DRIFT", "something unexpected; do not leak"
    rec.my_qty, rec.order_id = 0, None
    msg = trade_message(rec, SimpleNamespace(exchange="binance", environment="demo"))
    assert "补跟价格漂移超过上限，未追单" in msg and "something unexpected" not in msg


def test_stale_missed_order_reason_is_bilingual():
    rec = message_record()
    rec.status, rec.details = "SKIPPED_STALE_MISSED_ORDER", "Missed order stale: age 3600s > max 900s"
    rec.my_qty, rec.order_id = 0, None
    msg = trade_message(rec, SimpleNamespace(exchange="binance", environment="demo"))
    assert "欠单已过期，信号距今 1 小时 0 分，超过上限 15 分 0 秒" in msg
    assert "Missed order stale: signal age 1h 0m exceeded the 15m 0s limit" in msg


def test_slippage_block_reason_includes_measured_value():
    rec = message_record()
    rec.status, rec.details, rec.slippage_pct = "BLOCKED_SLIPPAGE", "Slippage 1.25% > max 1.00%", 1.25
    rec.my_qty, rec.order_id = 0, None
    msg = trade_message(rec, SimpleNamespace(exchange="binance", environment="demo"))
    assert "滑点超出上限（实测 1.25%）" in msg and "Slippage exceeded the limit (measured 1.25%)" in msg


def test_static_block_reason_and_no_reason_for_executions():
    rec = message_record()
    rec.status, rec.details = "BLOCKED_KILL_SWITCH", "kill switch engaged"
    rec.my_qty, rec.order_id = 0, None
    msg = trade_message(rec, SimpleNamespace(exchange="binance", environment="demo"))
    assert "📋 原因：急停开关已开启 / Kill switch engaged" in msg
    executed = trade_message(message_record(), SimpleNamespace(exchange="binance", environment="demo"))
    assert "📋 原因" not in executed
