"""滑点超限改挂限价单：挂出、成交、到期撤单、带单平仓撤单与通知文案的离线回归。"""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.models.domain import BindingDebt, ExecutionRecord, OrderIntent, PositionAllocation
from app.services.adapters.base import ExchangeAPIError, OrderBook
from app.services.engine import router as router_module
from app.services.notifier.trade import trade_message
from test_router_remediations import NOW_MS, OfflineExchange, execute_batch, trading  # noqa: F401


class LimitExchange(OfflineExchange):
    """卖一价可调的离线交易所：限价单默认停在簿上，可模拟成交与撤单。"""

    def __init__(self, ask=103.0):
        super().__init__(fill=0, state="new", price=100.5)
        self.get_orderbook = AsyncMock(return_value=OrderBook([[ask - 0.1, 100]], [[ask, 100]]))
        self.canceled = []
        self.cancel_error = None

    async def cancel_order(self, inst_id, cl_ord_id):
        self.canceled.append(cl_ord_id)
        if self.cancel_error is not None:
            raise self.cancel_error
        self.state = "canceled"


async def use_limit_mode(t, ttl=120):
    t.binding.slippage_action = "limit"
    t.binding.limit_order_ttl_sec = ttl
    await t.db.commit()


async def records(t):
    rows = (await t.db.scalars(select(ExecutionRecord).where(
        ExecutionRecord.binding_id == t.binding.id).order_by(ExecutionRecord.id))).all()
    return [row.status for row in rows], rows


async def reconcile(t, exchange):
    return await t.router.reconcile_pending_orders(t.db, t.account, exchange, {"BTCUSDT": t.spec})


def capture_messages(monkeypatch):
    """记录入队的通知文本，同时保留真实入队行为。"""
    sent = []
    real = router_module.enqueue_notification

    def spy(db, cfg, title, message, **kwargs):
        sent.append(message)
        return real(db, cfg, title, message, **kwargs)

    monkeypatch.setattr(router_module, "enqueue_notification", spy)
    return sent


async def test_block_mode_keeps_rejecting_excess_drift(trading):
    # 默认仍是拦截：升级后的旧绑定行为不变。
    exchange = LimitExchange()
    await execute_batch(trading, exchange, side="BUY", remaining=1, event="open-1")
    statuses, _ = await records(trading)
    assert statuses == ["BLOCKED_CATCHUP_DRIFT"] and exchange.orders == []


async def test_excess_drift_places_resting_limit_order(trading):
    t, exchange = trading, LimitExchange()
    await use_limit_mode(t)
    await execute_batch(t, exchange, side="BUY", remaining=1, event="open-1")
    order = exchange.orders[0]
    # 限价 = 带单均价 100 × (1 + min(滑点 1%, 追单偏离 0.5%))，仓位按限价测算。
    assert (order["ord_type"], order["px"], order["sz"], order["reduce_only"]) == ("limit", "100.5", "0.9", False)
    intent = await t.db.get(OrderIntent, order["cl_ord_id"])
    assert (intent.state, intent.limit_px, intent.expires_at_ms) == ("RESTING", "100.5", NOW_MS + 120_000)
    statuses, rows = await records(t)
    assert statuses == ["LIMIT_PLACED"]
    assert rows[0].my_qty == 0 and rows[0].my_price == 100.5 and rows[0].order_id == "exchange-id"
    # 未到期的在簿限价单不算未决订单，不会挂起账户的其他开仓。
    assert await reconcile(t, exchange) is True
    assert exchange.canceled == [] and intent.state == "RESTING"


async def test_favorable_drift_is_still_blocked_in_limit_mode(trading):
    t, exchange = trading, LimitExchange(ask=97.0)
    await use_limit_mode(t)
    await execute_batch(t, exchange, side="BUY", remaining=1, event="open-1")
    statuses, _ = await records(t)
    assert statuses == ["BLOCKED_CATCHUP_DRIFT"] and exchange.orders == []


async def test_resting_limit_fill_is_accounted_and_notified(trading, monkeypatch):
    t, exchange = trading, LimitExchange()
    await use_limit_mode(t)
    await execute_batch(t, exchange, side="BUY", remaining=1, event="open-1")
    sent = capture_messages(monkeypatch)
    exchange.fill, exchange.state = None, "filled"
    assert await reconcile(t, exchange) is True
    intent = await t.db.get(OrderIntent, exchange.orders[0]["cl_ord_id"])
    allocation = await t.db.scalar(select(PositionAllocation).where(PositionAllocation.binding_id == t.binding.id))
    assert intent.state == "FILLED" and allocation.owned_qty_exact == "0.9"
    statuses, rows = await records(t)
    assert statuses == ["LIMIT_PLACED", "EXECUTED"] and rows[1].my_qty == 0.9
    assert len(sent) == 1 and "✅ 限价单已成交" in sent[0] and "已成交 0.9 / 委托 0.9" in sent[0]


async def test_resting_limit_is_cancelled_after_ttl(trading, monkeypatch):
    t, exchange = trading, LimitExchange()
    await use_limit_mode(t, ttl=60)
    await execute_batch(t, exchange, side="BUY", remaining=1, event="open-1")
    sent = capture_messages(monkeypatch)
    monkeypatch.setattr("app.services.engine.router.time.time", lambda: (NOW_MS + 59_000) / 1000)
    assert await reconcile(t, exchange) is True and exchange.canceled == []
    monkeypatch.setattr("app.services.engine.router.time.time", lambda: (NOW_MS + 60_000) / 1000)
    assert await reconcile(t, exchange) is True
    intent = await t.db.get(OrderIntent, exchange.orders[0]["cl_ord_id"])
    assert exchange.canceled == [intent.id] and intent.state == "CANCELED"
    statuses, _ = await records(t)
    assert statuses == ["LIMIT_PLACED", "LIMIT_EXPIRED"]
    assert len(sent) == 1 and "限价单未成交，已自动撤单（超时）" in sent[0]
    # 已失效的订单不会被再次撤单或重复通知。
    assert await reconcile(t, exchange) is True
    assert len(exchange.canceled) == 1 and len(sent) == 1


async def test_partial_fill_then_expiry_keeps_fill_and_drops_remainder(trading, monkeypatch):
    t, exchange = trading, LimitExchange()
    await use_limit_mode(t)
    await execute_batch(t, exchange, side="BUY", remaining=1, event="open-1")
    sent = capture_messages(monkeypatch)
    exchange.fill, exchange.state = "0.4", "partially_filled"
    assert await reconcile(t, exchange) is True
    intent = await t.db.get(OrderIntent, exchange.orders[0]["cl_ord_id"])
    assert intent.state == "RESTING" and intent.filled_qty == "0.4"
    monkeypatch.setattr("app.services.engine.router.time.time", lambda: (NOW_MS + 121_000) / 1000)
    assert await reconcile(t, exchange) is True
    statuses, _ = await records(t)
    assert statuses == ["LIMIT_PLACED", "PARTIAL", "LIMIT_EXPIRED"]
    allocation = await t.db.scalar(select(PositionAllocation).where(PositionAllocation.binding_id == t.binding.id))
    debt = await t.db.scalar(select(BindingDebt).where(BindingDebt.binding_id == t.binding.id))
    # 已成交部分照常记账；未成交部分失效，不结转为待补金额。
    assert intent.state == "CANCELED" and allocation.owned_qty_exact == "0.4" and debt.pending_usd == 0
    assert "限价单部分成交，剩余继续挂单" in sent[0]
    assert "限价单剩余部分已自动撤单（超时）" in sent[1] and "已成交 0.4 / 委托 0.9" in sent[1]


async def test_cancel_rejected_because_order_filled_is_recorded_as_fill(trading, monkeypatch):
    # 撤单与成交竞态：撤单被拒后以查单结果为准，不能把已成交的单记成失效。
    t, exchange = trading, LimitExchange()
    await use_limit_mode(t)
    await execute_batch(t, exchange, side="BUY", remaining=1, event="open-1")
    exchange.cancel_error = ExchangeAPIError("binance", "-2011", "Unknown order sent")
    exchange.fill, exchange.state = None, "filled"
    monkeypatch.setattr("app.services.engine.router.time.time", lambda: (NOW_MS + 121_000) / 1000)
    # 先让对账看到“仍在簿”，再在撤单时发现已成交。
    states = iter(["new", "filled", "filled"])
    real_get = exchange.get_order

    async def racing_get(inst_id, ord_id=None, cl_ord_id=None):
        info = await real_get(inst_id, ord_id, cl_ord_id)
        state = next(states)
        return {**info, "state": state, "accFillSz": "0" if state == "new" else info["accFillSz"]}

    exchange.get_order = racing_get
    assert await reconcile(t, exchange) is True
    statuses, _ = await records(t)
    assert statuses == ["LIMIT_PLACED", "EXECUTED"]


async def test_unconfirmed_cancel_keeps_order_pending(trading, monkeypatch):
    # 撤单后仍查到在簿：保持未决，下一轮再撤，不臆断为已失效。
    t, exchange = trading, LimitExchange()
    await use_limit_mode(t)
    await execute_batch(t, exchange, side="BUY", remaining=1, event="open-1")
    exchange.cancel_error = ExchangeAPIError("binance", "-1001", "Internal error")
    monkeypatch.setattr("app.services.engine.router.time.time", lambda: (NOW_MS + 121_000) / 1000)
    monkeypatch.setattr("app.services.engine.router.asyncio.sleep", AsyncMock())
    assert await reconcile(t, exchange) is False
    intent = await t.db.get(OrderIntent, exchange.orders[0]["cl_ord_id"])
    statuses, _ = await records(t)
    assert intent.state == "RESTING" and statuses == ["LIMIT_PLACED"]


async def test_new_entry_is_not_stacked_on_a_resting_limit(trading):
    t, exchange = trading, LimitExchange()
    await use_limit_mode(t)
    await execute_batch(t, exchange, side="BUY", remaining=1, event="open-1")
    await execute_batch(t, exchange, side="BUY", remaining=2, event="open-2")
    statuses, _ = await records(t)
    assert statuses == ["LIMIT_PLACED", "BLOCKED_RESTING_LIMIT"] and len(exchange.orders) == 1


async def test_lead_exit_cancels_resting_limit(trading, monkeypatch):
    t, exchange = trading, LimitExchange()
    await use_limit_mode(t)
    await execute_batch(t, exchange, side="BUY", remaining=1, event="open-1")
    sent = capture_messages(monkeypatch)
    await execute_batch(t, exchange, side="SELL", remaining=0, event="close-1")
    intent = await t.db.get(OrderIntent, exchange.orders[0]["cl_ord_id"])
    assert exchange.canceled == [intent.id] and intent.state == "CANCELED"
    statuses, _ = await records(t)
    assert "LIMIT_EXPIRED" in statuses and len(exchange.orders) == 1
    assert any("已自动撤单（带单已减仓/平仓）" in message for message in sent)


def _record(status, **fields):
    base = dict(symbol="BTCUSDT", inst_id="BTCUSDT", side="BUY", position_side="LONG", status=status,
                account_id=1, binding_id=2, lead_qty=1, lead_price=100, lead_notional=100,
                my_qty=0, my_price=100.5, my_notional=0, slippage_pct=3.0, order_id="x",
                created_at=datetime.fromtimestamp(1_700_000_042, timezone.utc).replace(tzinfo=None))
    return SimpleNamespace(**{**base, **fields})


ACCOUNT = SimpleNamespace(exchange="binance", environment="live")
SOURCE = {"symbol": "BTCUSDT", "side": "BUY", "positionSide": "LONG", "qty": "1", "avgPrice": "100",
          "notional": "100", "endTime": 1_700_000_000_000}


def test_limit_placed_message_states_price_quantity_and_expiry():
    text = trade_message(_record("LIMIT_PLACED"), ACCOUNT, SOURCE, 1000,
                         limit={"px": "100.5", "requested": "0.9", "filled": "0", "ttl_sec": 120})
    assert "⏳ 滑点超限，已改挂限价单（尚未成交）" in text
    assert "📌 限价 100.5 ｜ 数量 0.9 ｜ 120 秒内未成交自动撤单" in text
    assert "📉 当前价偏离带单均价 3.00%" in text
    assert "⏱ 距带单成交 42 秒" in text and "LIMIT_PLACED" in text


def test_limit_messages_without_detail_still_name_the_outcome():
    # 补发路径拿不到限价明细时，结论行仍要明确。
    assert "已改挂限价单" in trade_message(_record("LIMIT_PLACED"), ACCOUNT, SOURCE)
    assert "限价单未成交，已自动撤单" in trade_message(_record("LIMIT_EXPIRED"), ACCOUNT, SOURCE)


def test_ordinary_fill_message_is_unchanged_apart_from_latency_line():
    text = trade_message(_record("EXECUTED", my_qty=0.9, my_price=100, my_notional=90), ACCOUNT, SOURCE)
    assert "✅ 已成交" in text and "限价" not in text
    # 没有带单成交时间就不显示时延，不估算。
    quiet = trade_message(_record("EXECUTED", my_qty=0.9, my_price=100, my_notional=90), ACCOUNT,
                          {k: v for k, v in SOURCE.items() if k != "endTime"})
    assert "⏱" not in quiet


def test_limit_fill_shortfall_is_reported_as_partial_with_remainder_cancelled():
    text = trade_message(_record("EXECUTED", my_qty=0.4, my_price=100.5, my_notional=40.2), ACCOUNT, SOURCE,
                         limit={"px": "100.5", "requested": "0.9", "filled": "0.4"})
    assert "⚠️ 限价单部分成交，剩余已撤单" in text and "已成交 0.4 / 委托 0.9" in text
    assert Decimal("0.4") < Decimal("0.9")
