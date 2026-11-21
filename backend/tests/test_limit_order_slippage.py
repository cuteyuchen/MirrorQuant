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
    """卖一价可调的离线交易所：限价单默认停在簿上，可模拟成交与撤单。

    查单按 cl_ord_id 逐单返回状态（已成功撤单的单报 canceled，未撤的报共享
    state），多笔在簿限价单的场景下才不会互相串状态：若用全局 state，
    撤掉第一笔后第二笔也会被误读成 canceled，测不出“只撤一笔”的回归。
    """

    def __init__(self, ask=103.0):
        super().__init__(fill=0, state="new", price=100.5)
        self.get_orderbook = AsyncMock(return_value=OrderBook([[ask - 0.1, 100]], [[ask, 100]]))
        self.canceled = []
        self.cancelled_ids = set()
        self.cancel_error = None

    async def cancel_order(self, inst_id, cl_ord_id):
        self.canceled.append(cl_ord_id)
        if self.cancel_error is not None:
            raise self.cancel_error
        self.cancelled_ids.add(cl_ord_id)

    async def get_order(self, inst_id, ord_id=None, cl_ord_id=None):
        if not self.orders:
            return None
        order = self.orders[-1]
        if cl_ord_id is not None:
            order = next((o for o in self.orders if o["cl_ord_id"] == cl_ord_id), order)
        if cl_ord_id is not None and cl_ord_id in self.cancelled_ids:
            state = "canceled"
        else:
            state = self.state
        return {"ordId": "exchange-id", "accFillSz": str(self.fill if self.fill is not None else order["sz"]),
                "avgPx": str(self.price), "state": state}


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
    # 限价 = 带单均价 100 × (1 + 生效滑点 1%)：跟单未设滑点时回退全局 1.0%，
    # 不再被补单漂移上限 0.5% 压住。仓位按限价测算（目标 100 美元 / 101 = 0.9 手）。
    assert (order["ord_type"], order["px"], order["sz"], order["reduce_only"]) == ("limit", "101.0", "0.9", False)
    intent = await t.db.get(OrderIntent, order["cl_ord_id"])
    assert (intent.state, intent.limit_px, intent.expires_at_ms) == ("RESTING", "101.0", NOW_MS + 120_000)
    statuses, rows = await records(t)
    assert statuses == ["LIMIT_PLACED"]
    assert rows[0].my_qty == 0 and rows[0].my_price == 101 and rows[0].order_id == "exchange-id"
    # 未到期的在簿限价单不算未决订单，不会挂起账户的其他开仓。
    assert await reconcile(t, exchange) is True
    assert exchange.canceled == [] and intent.state == "RESTING"


async def test_favorable_drift_enters_at_market_in_limit_mode(trading):
    # Favorable drift is not slippage, even in limit mode: a buy 3% below the
    # lead's price enters at market (IOC), not as a limit order and not blocked.
    t = trading
    exchange = OfflineExchange(price=97.0)
    exchange.get_orderbook.return_value = OrderBook([[96.9, 100]], [[97.0, 100]])
    await use_limit_mode(t)
    await execute_batch(t, exchange, side="BUY", remaining=1, event="open-1")
    assert len(exchange.orders) == 1
    order = exchange.orders[0]
    assert (order["ord_type"], order["px"]) == ("ioc", "97.4")
    statuses, rows = await records(t)
    assert statuses == ["EXECUTED"] and rows[0].my_price == 97.0


async def test_favorable_drift_short_enters_at_market_in_limit_mode(trading):
    # Short side mirrors the long side: a sell 2% above the lead's price
    # enters at market (IOC), not as a limit order and not blocked.
    t = trading
    exchange = OfflineExchange(price=102.0)
    exchange.get_orderbook.return_value = OrderBook([[102.0, 100]], [[102.1, 100]])
    await use_limit_mode(t)
    await execute_batch(t, exchange, side="SELL", position_side="SHORT", remaining=1, event="open-1")
    assert len(exchange.orders) == 1
    order = exchange.orders[0]
    assert (order["ord_type"], order["px"]) == ("ioc", "101.5")
    statuses, rows = await records(t)
    assert statuses == ["EXECUTED"] and rows[0].my_price == 102.0


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
    assert len(sent) == 1 and "✅ 限价单已成交" in sent[0] and "已成交 90.90 U / 委托 90.90 U" in sent[0]


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
    assert "限价单剩余部分已自动撤单（超时）" in sent[1] and "已成交 40.40 U / 委托 90.90 U" in sent[1]


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


async def test_new_entry_stacks_on_a_resting_limit(trading):
    # 2026-11-21 用户要求：加仓限价单允许多笔同时在簿。第一笔因滑点超限挂单后，
    # 第二笔同合约同方向加仓也照常挂出，不再被 BLOCKED_RESTING_LIMIT 挡掉。
    t, exchange = trading, LimitExchange()
    await use_limit_mode(t)
    await execute_batch(t, exchange, side="BUY", remaining=1, event="open-1")
    await execute_batch(t, exchange, side="BUY", remaining=2, event="open-2")
    statuses, _ = await records(t)
    assert statuses == ["LIMIT_PLACED", "LIMIT_PLACED"] and len(exchange.orders) == 2
    intents = (await t.db.scalars(select(OrderIntent).where(
        OrderIntent.binding_id == t.binding.id).order_by(OrderIntent.id))).all()
    assert [intent.state for intent in intents] == ["RESTING", "RESTING"]
    assert [order["ord_type"] for order in exchange.orders] == ["limit", "limit"]


async def test_lead_exit_cancels_all_resting_limits(trading, monkeypatch):
    # 带单员平仓时，本绑定的全部在簿限价单都要撤掉，不能只撤第一笔——
    # 残留挂单会在平仓后被动开仓。
    t, exchange = trading, LimitExchange()
    await use_limit_mode(t)
    await execute_batch(t, exchange, side="BUY", remaining=1, event="open-1")
    await execute_batch(t, exchange, side="BUY", remaining=2, event="open-2")
    intents = (await t.db.scalars(select(OrderIntent).where(
        OrderIntent.binding_id == t.binding.id).order_by(OrderIntent.id))).all()
    assert [intent.state for intent in intents] == ["RESTING", "RESTING"]
    sent = capture_messages(monkeypatch)
    await execute_batch(t, exchange, side="SELL", remaining=0, event="close-1")
    intents = (await t.db.scalars(select(OrderIntent).where(
        OrderIntent.binding_id == t.binding.id).order_by(OrderIntent.id))).all()
    assert [intent.state for intent in intents] == ["CANCELED", "CANCELED"]
    assert sorted(exchange.canceled) == sorted(intent.id for intent in intents)
    statuses, _ = await records(t)
    assert statuses.count("LIMIT_EXPIRED") == 2 and len(exchange.orders) == 2
    assert any("已自动撤单（带单已减仓/平仓）" in message for message in sent)


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
    assert "📌 限价 100.5 ｜ 金额 90.45 U ｜ 120 秒内未成交自动撤单" in text
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
    assert "⚠️ 限价单部分成交，剩余已撤单" in text and "已成交 40.20 U / 委托 90.45 U" in text
    assert Decimal("0.4") < Decimal("0.9")


# ---- 滑点层级：跟单设置优先，未设置时回退全局 risk_defaults ----

def _global_defaults(**overrides):
    from app.core.database import DEFAULT_SYSTEM_CONFIG

    rd = dict(DEFAULT_SYSTEM_CONFIG["risk_defaults"])
    rd.update(overrides)
    return rd


async def _set_binding_slippage(t, max_slippage=None, action=None, ttl=None):
    t.binding.max_slippage_pct = max_slippage
    t.binding.slippage_action = action
    t.binding.limit_order_ttl_sec = ttl
    await t.db.commit()


async def test_binding_slippage_overrides_global_for_limit_price(trading):
    # 跟单设 2.0%：限价挂在带单价 ×1.02，不再被全局 1% 卡住。
    t, exchange = trading, LimitExchange()
    await _set_binding_slippage(t, max_slippage=2.0, action="limit", ttl=120)
    await execute_batch(t, exchange, side="BUY", remaining=1, event="open-1")
    order = exchange.orders[0]
    assert (order["ord_type"], order["px"], order["sz"], order["reduce_only"]) == ("limit", "102.0", "0.9", False)
    statuses, rows = await records(t)
    assert statuses == ["LIMIT_PLACED"] and rows[0].my_price == 102.0


async def test_binding_slippage_tighter_than_global(trading):
    # 跟单设 0.5%：限价挂在带单价 ×1.005，比全局更紧。
    t, exchange = trading, LimitExchange()
    await _set_binding_slippage(t, max_slippage=0.5, action="limit", ttl=120)
    await execute_batch(t, exchange, side="BUY", remaining=1, event="open-1")
    order = exchange.orders[0]
    assert (order["ord_type"], order["px"], order["sz"], order["reduce_only"]) == ("limit", "100.5", "0.9", False)
    statuses, rows = await records(t)
    assert statuses == ["LIMIT_PLACED"] and rows[0].my_price == 100.5


async def test_unset_binding_slippage_falls_back_to_global(trading):
    # 跟单未设滑点：全局 2.0% 生效，限价挂在带单价 ×1.02。
    t, exchange = trading, LimitExchange()
    await _set_binding_slippage(t, max_slippage=None, action="limit", ttl=120)
    await execute_batch(t, exchange, side="BUY", remaining=1, event="open-1",
                        risk_defaults=_global_defaults(max_slippage_pct=2.0))
    order = exchange.orders[0]
    assert (order["ord_type"], order["px"], order["sz"], order["reduce_only"]) == ("limit", "102.0", "0.9", False)
    statuses, rows = await records(t)
    assert statuses == ["LIMIT_PLACED"] and rows[0].my_price == 102.0


async def test_unset_binding_slippage_action_falls_back_to_global(trading):
    # 跟单未设动作：全局 limit 生效，超限改挂限价单而不是拦截。
    t, exchange = trading, LimitExchange()
    await _set_binding_slippage(t, max_slippage=None, action=None, ttl=120)
    await execute_batch(t, exchange, side="BUY", remaining=1, event="open-1",
                        risk_defaults=_global_defaults(slippage_action="limit"))
    assert len(exchange.orders) == 1 and exchange.orders[0]["ord_type"] == "limit"
    statuses, _ = await records(t)
    assert statuses == ["LIMIT_PLACED"]


async def test_binding_slippage_action_overrides_global(trading):
    # 跟单显式 block：全局即使是 limit 也照样拦截。
    t, exchange = trading, LimitExchange()
    await _set_binding_slippage(t, max_slippage=None, action="block", ttl=120)
    await execute_batch(t, exchange, side="BUY", remaining=1, event="open-1",
                        risk_defaults=_global_defaults(slippage_action="limit"))
    statuses, _ = await records(t)
    assert statuses == ["BLOCKED_CATCHUP_DRIFT"] and exchange.orders == []


async def test_unset_binding_ttl_falls_back_to_global(trading):
    # 跟单未设 TTL：全局 600 秒生效。
    t, exchange = trading, LimitExchange()
    await _set_binding_slippage(t, max_slippage=None, action="limit", ttl=None)
    await execute_batch(t, exchange, side="BUY", remaining=1, event="open-1",
                        risk_defaults=_global_defaults(limit_order_ttl_sec=600))
    intent = await t.db.get(OrderIntent, exchange.orders[0]["cl_ord_id"])
    assert (intent.state, intent.expires_at_ms) == ("RESTING", NOW_MS + 600_000)


async def test_binding_ttl_overrides_global(trading):
    # 跟单设 300 秒：全局 600 秒不生效。
    t, exchange = trading, LimitExchange()
    await _set_binding_slippage(t, max_slippage=None, action="limit", ttl=300)
    await execute_batch(t, exchange, side="BUY", remaining=1, event="open-1",
                        risk_defaults=_global_defaults(limit_order_ttl_sec=600))
    intent = await t.db.get(OrderIntent, exchange.orders[0]["cl_ord_id"])
    assert (intent.state, intent.expires_at_ms) == ("RESTING", NOW_MS + 300_000)
