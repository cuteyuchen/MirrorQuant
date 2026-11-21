"""State-machine regressions using only in-memory DBs and an offline exchange."""
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from app.core.database import get_session_factory
from app.core.security import CredentialVault
from app.models.domain import (
    BindingDebt, BindingDelivery, BindingPositionProgress, CopyBinding, ExecutionRecord, LeadSource, OrderIntent,
    PositionAllocation, PositionGuardState, ProxyNode, SystemConfig, TradingAccount,
)
from app.services.adapters.base import BalanceInfo, ExchangePosition, InstrumentSpec, OrderBook, OrderRejected, OrderResult
from app.services.engine.risk import RiskEngine
from app.services.engine.router import CopyRouter, UnknownOrderOutcome, _effective_open_leverage
from app.services.engine.sizer import PositionSizer, exact, format_exact


NOW_MS = 2_000_000


@pytest.mark.parametrize("value,expected", [
    (0, "0"), (0.0, "0"), (-0.0, "0"), (1.0, "1"), ("100", "100"), ("100.000", "100"),
    (Decimal("1.2300"), "1.23"), (Decimal("1E-20"), "0.00000000000000000001"),
    (Decimal("1E+3"), "1000"),
    (Decimal("123456789012345678901234567890.123456789012345678900"),
     "123456789012345678901234567890.1234567890123456789"),
])
def test_exact_persistence_text_preserves_value_without_context_rounding(value, expected):
    # 固定点规范化不能误删整数尾零，也不能按默认 Decimal 精度舍入。
    encoded = format_exact(value)
    assert encoded == expected
    assert exact(encoded) == exact(value)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_exact_persistence_text_rejects_nonfinite_values(value):
    # 文本规范化仍必须经过财务有限数门禁，不能只靠字符串清理。
    with pytest.raises(ValueError, match="Non-finite financial input"):
        format_exact(value)


@pytest.mark.parametrize("existing,max_lvr,expected", [
    (10, 100, 10), (10.0, 100, 10), (50, 20, 20),
    (None, 100, 10), (0, 100, 10), (-5, 100, 10), (float("nan"), 100, 10),
])
def test_effective_open_leverage_keeps_existing_symbol_leverage(existing, max_lvr, expected):
    # 已有仓位沿用交易所当前杠杆（认领仓与加仓一致）；档位上限是硬约束，高于上限才降；
    # 无仓位或已有杠杆无效时回落动态计算（100/(1000*0.08) 向上取整为 2，但下限 10x，故为 10）。
    assert _effective_open_leverage(existing, target_notional=100, equity=1000,
                                    max_symbol_leverage=max_lvr) == expected


class OfflineExchange:
    def __init__(self, fill=None, state="filled", price=100):
        self.orders = []
        self.fill, self.state, self.price = fill, state, price
        self.get_max_leverage = AsyncMock(return_value=100)
        self.ensure_leverage = AsyncMock(return_value=True)
        self.get_orderbook = AsyncMock(return_value=OrderBook([[99.9, 100]], [[100, 100]]))
        self.get_positions = AsyncMock(return_value=[
            ExchangePosition("BTCUSDT", "BTCUSDT", "long", "cross", 10, 10, 100, 0, 0, 1000, 10)
        ])
        self.place_tp_sl = AsyncMock()
        self.cancel_tp_sl = AsyncMock()

    def resolve_symbol(self, symbol, instruments, symbol_map=None):
        inst_id = (symbol_map or {}).get(symbol, symbol)
        return inst_id, inst_id in instruments

    async def place_order(self, **order):
        self.orders.append(order)
        return OrderResult("exchange-id", order["cl_ord_id"])

    async def get_order(self, inst_id, ord_id=None, cl_ord_id=None):
        if not self.orders:
            return None
        order = self.orders[-1]
        return {"ordId": "exchange-id", "accFillSz": str(self.fill if self.fill is not None else order["sz"]),
                "avgPx": str(self.price), "state": self.state}


@pytest.fixture
async def trading(monkeypatch):
    monkeypatch.setattr("app.services.engine.router.time.time", lambda: NOW_MS / 1000)
    async with get_session_factory()() as db:
        lead = LeadSource(name="test lead", portfolio_id="test-portfolio")
        other_lead = LeadSource(name="other lead", portfolio_id="other-portfolio")
        account = TradingAccount(name="offline", exchange="binance", encrypted_credentials="offline-test-placeholder",
                                 api_key_masked="offline", usdt_equity=1000, avail_equity=1000, hwm_equity=1000,
                                 day_start_equity=1000)
        db.add_all([lead, other_lead, account])
        await db.flush()
        binding = CopyBinding(name="primary", lead_id=lead.id, account_id=account.id, max_instrument_leverage=100)
        other = CopyBinding(name="other", lead_id=other_lead.id, account_id=account.id)
        db.add_all([binding, other])
        await db.commit()
        spec = InstrumentSpec("BTCUSDT", Decimal("1"), Decimal("0.1"), Decimal("0.1"), Decimal("0.1"),
                              max_lvr=100, min_notional=Decimal("5"), max_sz=Decimal("1000"), market_max_sz=Decimal("1000"))
        pos = ExchangePosition("BTCUSDT", "BTCUSDT", "long", "cross", 10, 10, 100, 0, 0, 1000, 10)
        router = CopyRouter(CredentialVault())
        yield SimpleNamespace(db=db, lead=lead, account=account, binding=binding, other=other,
                              router=router, spec=spec, pos=pos)


async def seed_allocation(t, qty=1, debt=25):
    allocation = PositionAllocation(binding_id=t.binding.id, account_id=t.account.id, inst_id="BTCUSDT",
                                    pos_side="long", owned_qty=qty, entry_cost=qty * 100, opened_at_ms=100)
    other = PositionAllocation(binding_id=t.other.id, account_id=t.account.id, inst_id="BTCUSDT",
                              pos_side="long", owned_qty=9, entry_cost=900)
    t.pos.pos = t.pos.avail_pos = qty + other.owned_qty
    t.pos.notional_usd = t.pos.pos * t.pos.avg_px
    pending = BindingDebt(binding_id=t.binding.id, symbol="BTCUSDT", position_side="LONG", pending_usd=debt)
    guard = PositionGuardState(binding_id=t.binding.id, inst_id="BTCUSDT", pos_side="long", entry_px=100, peak_px=100, trough_px=100)
    t.db.add_all([allocation, other, pending, guard])
    await t.db.commit()
    return allocation, other, pending, guard


async def test_legacy_allocation_exact_fields_are_canonical_after_reload(trading):
    # 验证真实浮点旧记录升级后的落库值，不只检查内存里的 Decimal。
    t = trading
    allocation, _, debt, _ = await seed_allocation(t, qty=1.0)
    assert allocation.owned_qty_exact is None and allocation.entry_cost_exact is None
    migrated = await t.router._get_allocation(t.db, t.binding.id, t.account.id, "BTCUSDT", "long")
    t.router._set_debt(debt, Decimal("25.00"))
    await t.db.commit()
    await t.db.refresh(migrated)
    await t.db.refresh(debt)
    assert migrated.owned_qty_exact == "1" and migrated.entry_cost_exact == "100"
    assert debt.pending_usd_exact == "25"


async def execute_batch(t, exchange, side="SELL", qty=1, remaining=0, event="test-event",
                        position_side="LONG", avg_price="100", batch_fields=None, resilience_cfg=None,
                        source_proven=True, global_kill=False, risk_defaults=None):
    batch = {"symbol": "BTCUSDT", "side": side, "positionSide": position_side, "qty": str(qty),
             "avgPrice": avg_price, "notional": str(Decimal(str(qty)) * 100), "_event_id": event,
             "startTime": NOW_MS - 150_000, "endTime": NOW_MS - 150_000}
    batch.update(batch_fields or {})
    is_open = (side, position_side) in (("BUY", "LONG"), ("SELL", "SHORT"))
    if not is_open and source_proven and not t.lead.snapshot_json:
        # 仅补未提供的业务夹具证据，不能覆盖安全用例显式保存的坏快照。
        snapshot = current_snapshot(position_side)
        snapshot["openPositions"] = ([{
            "symbol": "BTCUSDT", "positionSide": position_side, "qty": remaining, "qtyExact": str(remaining),
        }] if remaining else [])
        t.lead.snapshot_json = json.dumps(snapshot)
        await t.db.commit()
    await t.router._execute_batch_for_binding(
        t.db, t.binding, t.lead, t.account, exchange, batch,
        lead_margin_balance=1000, active_lead_positions={f"BTCUSDT_{position_side}": remaining}, my_equity=1000,
        positions=[] if is_open else [t.pos], instruments={"BTCUSDT": t.spec}, global_kill=global_kill,
        notify_cfg={}, resilience_cfg=resilience_cfg, risk_defaults=risk_defaults,
    )
    await t.db.commit()


@pytest.mark.parametrize("lookup_result", (None, RuntimeError("lookup unavailable")))
async def test_ambiguous_order_is_never_resent(lookup_result):
    first = TimeoutError("acceptance response lost")
    exchange = SimpleNamespace(place_order=AsyncMock(side_effect=first), get_order=AsyncMock())
    if isinstance(lookup_result, Exception):
        exchange.get_order.side_effect = lookup_result
    else:
        exchange.get_order.return_value = lookup_result
    with pytest.raises(UnknownOrderOutcome) as caught:
        await CopyRouter._place_order_idempotent(exchange, "BTCUSDT", "buy", "long", "ioc", "1", "100", "stable", False, "cross")
    assert caught.value.__cause__ is first
    exchange.place_order.assert_awaited_once()


async def test_reconciled_placement_uses_existing_order():
    exchange = SimpleNamespace(place_order=AsyncMock(side_effect=TimeoutError()), get_order=AsyncMock(return_value={"ordId": "accepted", "accFillSz": "0", "state": "new"}))
    result = await CopyRouter._place_order_idempotent(exchange, "BTCUSDT", "buy", "long", "ioc", "1", "100", "stable", False, "cross")
    assert result.ord_id == "accepted" and result.fill_sz == 0
    exchange.place_order.assert_awaited_once()


async def test_full_close_only_consumes_this_bindings_allocation(trading):
    t = trading
    own, other, debt, guard = await seed_allocation(t)
    exchange = OfflineExchange()
    await execute_batch(t, exchange)
    assert len(exchange.orders) == 1 and Decimal(exchange.orders[0]["sz"]) == 1
    assert own.owned_qty == 0 and other.owned_qty == 9 and debt.pending_usd == 0
    assert await t.db.get(PositionGuardState, guard.id) is None
    record = await t.db.scalar(select(ExecutionRecord))
    assert record.status == "EXECUTED" and record.my_qty == 1


async def test_close_cannot_claim_manual_or_unallocated_positions(trading):
    t = trading
    exchange = OfflineExchange()
    await execute_batch(t, exchange)
    assert exchange.orders == []
    assert (await t.db.scalar(select(ExecutionRecord))).status == "SKIPPED_NO_LOCAL_POS"


async def test_small_partial_close_does_not_promote_to_minimum(trading):
    t = trading
    own, _, debt, _ = await seed_allocation(t)
    exchange = OfflineExchange()
    await execute_batch(t, exchange, qty=1, remaining=99)
    assert exchange.orders == [] and own.owned_qty == 1 and debt.pending_usd == 25
    assert (await t.db.scalar(select(ExecutionRecord))).status == "SKIPPED_BELOW_MIN"


async def test_partial_close_scales_only_owned_quantity(trading):
    t = trading
    own, other, debt, _ = await seed_allocation(t, qty=2)
    exchange = OfflineExchange()
    await execute_batch(t, exchange, qty=1, remaining=3)
    assert Decimal(exchange.orders[0]["sz"]) == Decimal("0.5")
    assert own.owned_qty == 1.5 and other.owned_qty == 9 and debt.pending_usd == 18.75


@pytest.mark.parametrize("state", ("canceled", "new"))
async def test_zero_close_fill_never_clears_debt_or_guard(trading, state):
    t = trading
    own, other, debt, guard = await seed_allocation(t)
    exchange = OfflineExchange(fill=0, state=state)
    await execute_batch(t, exchange)
    assert len(exchange.orders) == 1
    assert own.owned_qty == 1 and other.owned_qty == 9 and debt.pending_usd == 25
    assert await t.db.get(PositionGuardState, guard.id) is not None
    records = (await t.db.scalars(select(ExecutionRecord))).all()
    assert records and all(record.status != "EXECUTED" for record in records)


async def test_historical_open_is_skipped_when_lead_is_flat(trading):
    t = trading
    exchange = OfflineExchange()
    await execute_batch(t, exchange, side="BUY", remaining=0)
    assert exchange.orders == []
    assert (await t.db.scalar(select(ExecutionRecord))).status == "SKIPPED_CLOSED_CYCLE"


@pytest.mark.parametrize("side,position_side,bid,ask,status", (
    ("BUY", "LONG", 100.4, 100.6, "BLOCKED_CATCHUP_DRIFT"),
    ("SELL", "SHORT", 99.4, 99.6, "BLOCKED_CATCHUP_DRIFT"),
    ("BUY", "LONG", 99.4, 99.6, "EXECUTED"),
    ("SELL", "SHORT", 100.4, 100.6, "EXECUTED"),
    # Favorable moves are not drift: buying cheaper / selling dearer than the
    # lead enters at market instead of being blocked.
    ("BUY", "LONG", 99.3, 99.4, "EXECUTED"),
    ("SELL", "SHORT", 100.6, 100.7, "EXECUTED"),
))
async def test_catchup_uses_executable_side_and_directional_drift(trading, side, position_side, bid, ask, status):
    t = trading
    executable_price = ask if side == "BUY" else bid
    exchange = OfflineExchange(price=executable_price)
    exchange.get_orderbook.return_value = OrderBook([[bid, 100]], [[ask, 100]])
    # 本用例的边界在 0.4% 与 0.6% 之间：漂移上限现在来自生效滑点（跟单优先、
    # 未设置时回退全局），不再是 resilience 的 catchup_max_price_dev_pct，
    # 这里显式把生效滑点定为 0.5% 以保留原边界。
    await execute_batch(t, exchange, side=side, position_side=position_side, remaining=1,
                        risk_defaults={"max_slippage_pct": 0.5})
    record = await t.db.scalar(select(ExecutionRecord))
    assert record.status == status and record.my_price == executable_price
    if status == "EXECUTED":
        assert len(exchange.orders) == 1
        assert exchange.orders[0]["side"] == side.lower()
        assert exchange.orders[0]["pos_side"] == position_side.lower()
    else:
        assert exchange.orders == []
        exchange.ensure_leverage.assert_not_awaited()


@pytest.mark.parametrize("side,position_side", (("BUY", "LONG"), ("SELL", "SHORT")))
@pytest.mark.parametrize("price", (None, 0, -1, float("nan"), float("inf")))
async def test_catchup_missing_or_invalid_executable_quote_is_recorded(trading, side, position_side, price):
    t = trading
    exchange = OfflineExchange()
    invalid_side = [] if price is None else [[price, 100]]
    exchange.get_orderbook.return_value = (OrderBook([[100, 100]], invalid_side) if side == "BUY"
                                          else OrderBook(invalid_side, [[100, 100]]))
    await execute_batch(t, exchange, side=side, position_side=position_side, remaining=1)
    assert exchange.orders == []
    record = await t.db.scalar(select(ExecutionRecord))
    assert record.status == "BLOCKED_CATCHUP_PRICE" and record.details
    exchange.ensure_leverage.assert_not_awaited()


@pytest.mark.parametrize("side,position_side", (("BUY", "LONG"), ("SELL", "SHORT")))
async def test_catchup_empty_book_cannot_reuse_lead_price(trading, side, position_side):
    t = trading
    exchange = OfflineExchange()
    exchange.get_orderbook.return_value = OrderBook([], [])
    await execute_batch(t, exchange, side=side, position_side=position_side, remaining=1)
    assert exchange.orders == []
    assert (await t.db.scalar(select(ExecutionRecord))).status == "BLOCKED_CATCHUP_PRICE"


@pytest.mark.parametrize("side,position_side", (("BUY", "LONG"), ("SELL", "SHORT")))
@pytest.mark.parametrize("price", (None, 0, -1, float("nan"), float("inf")))
async def test_valid_executable_quote_still_requires_valid_opposite_book(trading, side, position_side, price):
    t = trading
    exchange = OfflineExchange()
    invalid_side = [] if price is None else [[price, 100]]
    exchange.get_orderbook.return_value = (OrderBook(invalid_side, [[100, 100]]) if side == "BUY"
                                          else OrderBook([[100, 100]], invalid_side))
    await execute_batch(t, exchange, side=side, position_side=position_side, remaining=1)
    assert exchange.orders == []
    assert (await t.db.scalar(select(ExecutionRecord))).status == "BLOCKED_SPREAD"


@pytest.mark.parametrize("side,position_side", (("BUY", "LONG"), ("SELL", "SHORT")))
@pytest.mark.parametrize("lead_price", (None, "0", "-1", "nan", "inf", "-inf"))
async def test_catchup_invalid_lead_price_is_recorded(trading, side, position_side, lead_price):
    t = trading
    exchange = OfflineExchange()
    await execute_batch(t, exchange, side=side, position_side=position_side, remaining=1, avg_price=lead_price)
    assert exchange.orders == []
    record = await t.db.scalar(select(ExecutionRecord))
    assert record.status == "BLOCKED_CATCHUP_PRICE" and record.details
    exchange.ensure_leverage.assert_not_awaited()


# catchup_max_price_dev_pct 已不再是漂移上限（生效滑点取而代之，见上），
# 该字段的无效值现在被忽略；仍在生效的上限字段只有 missed_order_max_age_sec。
@pytest.mark.parametrize("field", ("missed_order_max_age_sec",))
@pytest.mark.parametrize("value", (0, -1))
async def test_invalid_catchup_limits_are_recorded_before_execution(trading, field, value):
    t = trading
    exchange = OfflineExchange()
    await execute_batch(t, exchange, side="BUY", remaining=1, resilience_cfg={field: value})
    assert exchange.orders == []
    record = await t.db.scalar(select(ExecutionRecord))
    assert record.status == "BLOCKED_CATCHUP_CONFIG" and record.details
    exchange.ensure_leverage.assert_not_awaited()


@pytest.mark.parametrize("value", (0, -1))
async def test_invalid_effective_slippage_is_recorded_before_execution(trading, value):
    # 漂移上限来自生效滑点：脏值（API 层 gt=0 校验已挡正常写入，这里直接写库模拟）
    # 仍要在执行前被补单门禁拦下并记录为 BLOCKED_CATCHUP_CONFIG。
    t = trading
    t.binding.max_slippage_pct = value
    await t.db.commit()
    exchange = OfflineExchange()
    await execute_batch(t, exchange, side="BUY", remaining=1)
    assert exchange.orders == []
    record = await t.db.scalar(select(ExecutionRecord))
    assert record.status == "BLOCKED_CATCHUP_CONFIG" and record.details
    exchange.ensure_leverage.assert_not_awaited()


@pytest.mark.parametrize("timestamp", (0, -1, NOW_MS + 1))
async def test_catchup_missing_or_future_timestamp_cannot_bypass_gate(trading, timestamp):
    t = trading
    exchange = OfflineExchange()
    await execute_batch(t, exchange, side="BUY", remaining=1,
                        batch_fields={"startTime": timestamp, "endTime": timestamp})
    assert exchange.orders == []
    assert (await t.db.scalar(select(ExecutionRecord))).status == "SKIPPED_STALE_MISSED_ORDER"


async def test_catchup_rejects_before_spread_slippage_sizing_and_leverage(trading, monkeypatch):
    t = trading
    exchange = OfflineExchange()
    # This book also violates spread; catch-up drift must be the recorded reason.
    exchange.get_orderbook.return_value = OrderBook([[99, 100]], [[101, 100]])

    def unexpected(*args, **kwargs):
        pytest.fail("A later guard or sizer ran after catch-up rejected the order")

    monkeypatch.setattr(RiskEngine, "check_spread", unexpected)
    monkeypatch.setattr(RiskEngine, "check_slippage", unexpected)
    monkeypatch.setattr(PositionSizer, "calculate_target_usd", unexpected)
    monkeypatch.setattr(RiskEngine, "cap_by_leverage", unexpected)
    # 漂移 1%（ask 101 vs 带单价 100）：生效滑点定为 0.5% 才能让补单门禁先于
    # 价差/滑点/仓位/杠杆拦截；缺省全局 1.0% 时 1% 漂移不超限，会走到后面的 stale 检查。
    await execute_batch(t, exchange, side="BUY", remaining=1,
                        batch_fields={"endTime": NOW_MS - 1_000_000},
                        risk_defaults={"max_slippage_pct": 0.5})
    assert exchange.orders == []
    assert (await t.db.scalar(select(ExecutionRecord))).status == "BLOCKED_CATCHUP_DRIFT"
    exchange.ensure_leverage.assert_not_awaited()


async def test_old_exit_event_cannot_close_newer_allocation(trading):
    t = trading
    own, other, debt, _ = await seed_allocation(t)
    own.opened_at_ms = NOW_MS - 100_000
    await t.db.commit()
    exchange = OfflineExchange()
    await execute_batch(t, exchange)
    assert exchange.orders == [] and own.owned_qty == 1 and other.owned_qty == 9 and debt.pending_usd == 25
    assert (await t.db.scalar(select(ExecutionRecord))).status == "SKIPPED_CLOSED_CYCLE"
    exchange.get_orderbook.assert_not_awaited()


async def test_closed_cycle_open_cannot_attach_to_current_reopened_position(trading):
    t = trading
    exchange = OfflineExchange()
    await execute_batch(t, exchange, side="BUY", remaining=1, batch_fields={"_closed_cycle": True})
    assert exchange.orders == []
    assert (await t.db.scalar(select(ExecutionRecord))).status == "SKIPPED_CLOSED_CYCLE"
    exchange.get_orderbook.assert_not_awaited()


async def prepare_event_router(t, exchange, monkeypatch):
    t.proxy = ProxyNode(name="offline", url="http://proxy.invalid:8080", is_enabled=True, is_healthy=True)
    t.db.add(t.proxy)
    t.binding.baseline_seeded = True
    notifications = await t.db.get(SystemConfig, "notifications")
    notifications.value_json = json.dumps({"notify_on_execution": False, "notify_on_risk_block": False})
    await t.db.commit()
    monkeypatch.setattr(t.router.vault, "decrypt_dict", lambda value: {})
    monkeypatch.setattr("app.services.engine.router.create_exchange_adapter", lambda **kwargs: exchange)
    exchange.sync_time = AsyncMock(return_value=0)
    exchange.get_instruments = AsyncMock(return_value={"BTCUSDT": t.spec})
    exchange.get_usdt_balance = AsyncMock(return_value=BalanceInfo(eq=1000, avail_eq=1000, cash_bal=1000, upl=0))


def source_order(event_id, position_side, side, timestamp, price="100"):
    return {"orderId": event_id, "orderTime": timestamp, "orderUpdateTime": timestamp,
            "symbol": "BTCUSDT", "side": side, "positionSide": position_side, "type": "MARKET",
            "executedQty": "1", "avgPrice": price}


def current_snapshot(position_side):
    return {"profile": {"marginBalance": 1000}, "openPositions": [
        {"symbol": "BTCUSDT", "positionSide": position_side, "qty": 1, "qtyExact": "1"}],
        "openPositionsMeta": {"source": "api", "authoritative": True, "complete": True,
                              "positionShow": True, "asOf": NOW_MS}}


async def test_fresh_snapshot_routes_150_second_delayed_fill_once(trading, monkeypatch):
    t = trading
    exchange = OfflineExchange()
    exchange.get_positions.return_value = []
    await prepare_event_router(t, exchange, monkeypatch)
    orders = [source_order("delayed", "LONG", "BUY", NOW_MS - 150_000)]
    snapshot = current_snapshot("LONG")
    await t.router.process_lead_events(t.db, t.lead, orders, [], snapshot, [t.proxy])
    assert len(exchange.orders) == 1
    assert (await t.db.scalar(select(ExecutionRecord))).status == "EXECUTED"
    # The age did not suppress ingestion; replay is suppressed by source identity.
    await t.router.process_lead_events(t.db, t.lead, orders, [], snapshot, [t.proxy])
    assert len(exchange.orders) == 1
    assert len((await t.db.scalars(select(ExecutionRecord))).all()) == 1


@pytest.mark.parametrize("existing_lever,tier_max,expected", [(10, 100, 10), (50, 20, 20), (None, 100, 10)])
async def test_open_add_applies_effective_leverage(trading, existing_lever, tier_max, expected):
    # 加仓沿用已有仓位的交易所杠杆（认领仓 10x 则加仓也 10x，不擅自改成动态值）；
    # 档位上限不足时降到上限；无仓位时动态计算。
    t = trading
    exchange = OfflineExchange()
    exchange.get_max_leverage = AsyncMock(return_value=tier_max)
    positions = [] if existing_lever is None else [
        ExchangePosition("BTCUSDT", "BTCUSDT", "long", "cross", 10, 10, 100, 0, 0, 1000, existing_lever)]
    batch = {"symbol": "BTCUSDT", "side": "BUY", "positionSide": "LONG", "qty": "1",
             "avgPrice": "100", "notional": "100", "_event_id": f"leverage-{existing_lever}-{tier_max}",
             "startTime": NOW_MS - 150_000, "endTime": NOW_MS - 150_000}
    await t.router._execute_batch_for_binding(
        t.db, t.binding, t.lead, t.account, exchange, batch,
        lead_margin_balance=1000, active_lead_positions={"BTCUSDT_LONG": 1}, my_equity=1000,
        positions=positions, instruments={"BTCUSDT": t.spec}, global_kill=False,
        notify_cfg={}, resilience_cfg=None,
    )
    await t.db.commit()
    exchange.ensure_leverage.assert_awaited_once()
    assert exchange.ensure_leverage.await_args.args[1] == expected
    assert (await t.db.scalar(select(ExecutionRecord))).status == "EXECUTED"


@pytest.mark.parametrize("remaining,expected_qty", [(0, "1.0"), (1, "0.5")])
async def test_collateral_failure_then_recovery_retries_exit_once(trading, monkeypatch, remaining, expected_qty):
    t = trading
    allocation, other, debt, guard = await seed_allocation(t)
    exchange = OfflineExchange()
    await prepare_event_router(t, exchange, monkeypatch)
    exchange.get_usdt_balance.side_effect = [RuntimeError("synthetic balance unavailable"),
        BalanceInfo(1000, 1000, 1000, 0)]
    snapshot = current_snapshot("LONG")
    snapshot["openPositions"][0].update(qty=remaining, qtyExact=str(remaining))
    orders = [source_order("recover-exit", "LONG", "SELL", NOW_MS - 150_000)]
    await t.router.process_lead_events(t.db, t.lead, orders, [], snapshot, [t.proxy])
    delivery = await t.db.scalar(select(BindingDelivery))
    assert delivery.state != "APPLIED"
    assert exchange.orders == []
    assert allocation.owned_qty == 1 and other.owned_qty == 9
    assert (await t.db.scalar(select(ExecutionRecord))).status == "BLOCKED_COLLATERAL_BALANCE"
    await t.router.process_lead_events(t.db, t.lead, orders, [], snapshot, [t.proxy])
    assert len(exchange.orders) == 1
    assert Decimal(exchange.orders[0]["sz"]) == Decimal(expected_qty)
    assert exchange.orders[0]["reduce_only"] is True
    assert delivery.state == "APPLIED"
    assert Decimal(str(allocation.owned_qty)) == 1 - Decimal(expected_qty)
    assert other.owned_qty == 9
    await t.router.process_lead_events(t.db, t.lead, orders, [], snapshot, [t.proxy])
    assert len(exchange.orders) == 1
    assert len((await t.db.scalars(select(OrderIntent))).all()) == 1
    assert len((await t.db.scalars(select(ExecutionRecord).where(ExecutionRecord.my_qty > 0))).all()) == 1


@pytest.mark.parametrize("ambiguous", [False, True])
async def test_recovered_exit_partial_or_unknown_submission_never_resubmits(trading, monkeypatch, ambiguous):
    t = trading
    allocation, other, debt, guard = await seed_allocation(t)
    exchange = OfflineExchange(fill=0.2, state="partially_filled")
    await prepare_event_router(t, exchange, monkeypatch)
    exchange.get_usdt_balance.side_effect = RuntimeError("synthetic balance unavailable")
    orders = [source_order("recover-unresolved-exit", "LONG", "SELL", NOW_MS - 150_000)]
    snapshot = current_snapshot("LONG")
    snapshot["openPositions"] = []
    await t.router.process_lead_events(t.db, t.lead, orders, [], snapshot, [t.proxy])
    assert exchange.orders == []
    exchange.get_usdt_balance.side_effect = None
    if ambiguous:
        async def submit(**order):
            exchange.orders.append(order)
            raise TimeoutError("synthetic acceptance response lost")
        exchange.place_order = submit
        exchange.get_order = AsyncMock(return_value=None)
    await t.router.process_lead_events(t.db, t.lead, orders, [], snapshot, [t.proxy])
    assert len(exchange.orders) == 1
    delivery = await t.db.scalar(select(BindingDelivery))
    assert delivery.state != "APPLIED"
    assert allocation.owned_qty == (1 if ambiguous else 0.8)
    await t.router.process_lead_events(t.db, t.lead, orders, [], snapshot, [t.proxy])
    assert len(exchange.orders) == 1
    exchange.get_order = AsyncMock(return_value={"ordId": "exchange-id", "accFillSz": "1",
                                               "avgPx": "100", "state": "filled"})
    await t.router.process_lead_events(t.db, t.lead, orders, [], snapshot, [t.proxy])
    assert allocation.owned_qty == 0 and other.owned_qty == 9
    assert delivery.state == "APPLIED"
    assert len(exchange.orders) == 1
    records = (await t.db.scalars(select(ExecutionRecord).where(ExecutionRecord.my_qty > 0))).all()
    assert sum(Decimal(str(record.my_qty)) for record in records) == 1
    await t.router.process_lead_events(t.db, t.lead, orders, [], snapshot, [t.proxy])
    assert len(exchange.orders) == 1


async def test_collateral_failure_defers_mixed_exit_and_entry_in_order(trading, monkeypatch):
    t = trading
    await seed_allocation(t)
    exchange = OfflineExchange()
    await prepare_event_router(t, exchange, monkeypatch)
    exchange.get_usdt_balance.side_effect = RuntimeError("synthetic balance unavailable")
    orders = [source_order("first-close", "LONG", "SELL", NOW_MS - 150_000),
              source_order("then-open", "LONG", "BUY", NOW_MS - 149_000)]
    snapshot = current_snapshot("LONG")
    await t.router.process_lead_events(t.db, t.lead, orders, [], snapshot, [t.proxy])
    assert exchange.orders == []
    assert len((await t.db.scalars(select(BindingDelivery).where(BindingDelivery.state != "APPLIED"))).all()) == 2
    exchange.get_usdt_balance.side_effect = None
    await t.router.process_lead_events(t.db, t.lead, orders, [], snapshot, [t.proxy])
    assert [order["side"] for order in exchange.orders] == ["sell", "buy"]
    assert [order["reduce_only"] for order in exchange.orders] == [True, False]
    await t.router.process_lead_events(t.db, t.lead, orders, [], snapshot, [t.proxy])
    assert len(exchange.orders) == 2


@pytest.mark.parametrize("flip_close_first", (False, True))
async def test_flip_closes_long_before_catchup_short_for_both_legacy_flags(trading, monkeypatch, flip_close_first):
    t = trading
    own, other, _, _ = await seed_allocation(t)
    resilience = await t.db.get(SystemConfig, "resilience")
    resilience.value_json = json.dumps({**json.loads(resilience.value_json), "flip_close_first": flip_close_first})
    exchange = OfflineExchange()
    await prepare_event_router(t, exchange, monkeypatch)
    catchup = Mock(wraps=RiskEngine.check_missed_order_catchup)
    monkeypatch.setattr(RiskEngine, "check_missed_order_catchup", catchup)
    # Supply reverse input order. An old close with a distant lead price must reduce
    # first, without entering the catch-up gate or borrowing the short's book side.
    orders = [source_order("new-short", "SHORT", "SELL", NOW_MS - 150_000),
              source_order("close-long", "LONG", "SELL", NOW_MS - 151_000, price="200")]
    await t.router.process_lead_events(t.db, t.lead, orders, [], current_snapshot("SHORT"), [t.proxy])
    assert [(order["side"], order["pos_side"], order["reduce_only"]) for order in exchange.orders] == [
        ("sell", "long", True), ("sell", "short", False)]
    assert own.owned_qty == 0 and other.owned_qty == 9
    short = await t.db.scalar(select(PositionAllocation).where(
        PositionAllocation.binding_id == t.binding.id, PositionAllocation.pos_side == "short"))
    assert short.owned_qty == 1 and short.opened_at_ms == NOW_MS - 150_000
    catchup.assert_called_once()
    assert catchup.call_args.kwargs["ref_price"] == 99.9
    assert catchup.call_args.kwargs["lead_avg_price"] == 100
    assert [record.status for record in (await t.db.scalars(select(ExecutionRecord).order_by(ExecutionRecord.id))).all()] == [
        "EXECUTED", "EXECUTED"]


async def test_leverage_is_dynamic_and_quantity_uses_equity_ratio(trading):
    t = trading
    exchange = OfflineExchange()
    await execute_batch(t, exchange, side="BUY", qty=1, remaining=1)
    assert len(exchange.orders) == 1 and Decimal(exchange.orders[0]["sz"]) == 1
    assert exchange.orders[0]["ord_type"] == "ioc"
    assert Decimal(exchange.orders[0]["px"]) <= Decimal("101")
    assert exchange.ensure_leverage.await_args.args == ("BTCUSDT", 10)
    own = await t.db.scalar(select(PositionAllocation).where(PositionAllocation.binding_id == t.binding.id))
    assert own.owned_qty == 1
    exchange.place_tp_sl.assert_not_awaited()
    exchange.cancel_tp_sl.assert_not_awaited()


async def test_failed_leverage_confirmation_prevents_order(trading):
    t = trading
    exchange = OfflineExchange()
    exchange.ensure_leverage.return_value = False
    # 杠杆失败只记本笔失败，不再向账户循环冒泡。
    await execute_batch(t, exchange, side="BUY", remaining=1)
    assert exchange.orders == []
    record = await t.db.scalar(select(ExecutionRecord))
    assert record.status == "FAILED" and "leverage could not be confirmed" in record.details
    assert t.binding.consecutive_failures == 1 and t.account.status != "error"


@pytest.mark.parametrize("failure", ["tier", "leverage", "validation"])
async def test_pre_submission_failure_is_per_delivery_and_next_event_can_proceed(trading, failure):
    # 提交前错误不得冒泡终止整个账户的后续事件。
    t, exchange = trading, OfflineExchange()
    if failure == "tier":
        exchange.get_max_leverage.side_effect = RuntimeError("tier unavailable")
    elif failure == "leverage":
        exchange.ensure_leverage.side_effect = RuntimeError("leverage unavailable")
    else:
        t.spec.max_sz = Decimal("0.5")
    await execute_batch(t, exchange, side="BUY", remaining=1, event="first-failed")
    assert exchange.orders == [] and t.account.status != "error"
    assert t.binding.consecutive_failures == 1
    assert (await t.db.scalar(select(ExecutionRecord))).status == ("REJECTED" if failure == "validation" else "FAILED")
    t.spec.max_sz = Decimal("1000")
    successor = OfflineExchange()
    await execute_batch(t, successor, side="BUY", remaining=1, event="next-success")
    assert len(successor.orders) == 1


@pytest.mark.parametrize("reason", ["global", "binding", "account"])
async def test_entry_disable_record_names_the_real_reason(trading, reason):
    t = trading
    if reason == "binding":
        t.binding.is_enabled = False
    elif reason == "account":
        t.account.is_enabled = False
    await t.db.commit()
    await execute_batch(t, OfflineExchange(), side="BUY", remaining=1, global_kill=reason == "global")
    record = await t.db.scalar(select(ExecutionRecord))
    assert record.status == "BLOCKED_KILL_SWITCH"
    expected = {"global": "Global kill switch is active", "binding": "Binding is disabled", "account": "Account is disabled"}
    assert record.details == expected[reason]


async def test_restart_reconciles_durable_intent_without_second_post(trading):
    t = trading
    exchange = OfflineExchange(fill=0, state="new")
    order = dict(source_event_id="restart-event", target_usd=100, inst_id="BTCUSDT", side="buy", pos_side="long",
                 ord_type="ioc", sz="1", px="100", reduce_only=False, td_mode="cross")
    with pytest.raises(UnknownOrderOutcome):
        await t.router._submit_confirmed(t.db, t.binding, t.account, exchange, t.spec, **order)
    intent = await t.db.scalar(select(OrderIntent))
    assert intent.state == "UNKNOWN" and len(exchange.orders) == 1
    # New session and router stand in for process restart; the venue then reveals its fill.
    exchange.fill, exchange.state = 1, "filled"
    async with get_session_factory()() as db:
        binding = await db.get(CopyBinding, t.binding.id)
        account = await db.get(TradingAccount, t.account.id)
        result = await CopyRouter(CredentialVault())._submit_confirmed(db, binding, account, exchange, t.spec, **order)
        assert result.fill_sz == 1 and len(exchange.orders) == 1
        own = await db.scalar(select(PositionAllocation).where(PositionAllocation.binding_id == binding.id))
        assert own.owned_qty == 1
        # A replay of the completed delivery is also read-only and does not double count.
        replay = await CopyRouter(CredentialVault())._submit_confirmed(db, binding, account, exchange, t.spec, **order)
        assert replay.fill_sz == 0 and own.owned_qty == 1 and len(exchange.orders) == 1


async def test_old_closed_cycle_cannot_close_reopened_allocation(trading):
    t = trading
    own, _, _, _ = await seed_allocation(t)
    own.opened_at_ms = 200
    await t.db.commit()
    exchange = OfflineExchange()
    await t.router._reconcile_closed_position(t.db, t.binding, t.lead, t.account, exchange,
        {"id": "old-cycle", "symbol": "BTCUSDT", "side": "LONG", "closed": 150},
        [t.pos], {"BTCUSDT": t.spec}, {})
    assert exchange.orders == [] and own.owned_qty == 1


async def test_closed_history_reconciliation_is_binding_scoped(trading):
    t = trading
    own, other, _, _ = await seed_allocation(t)
    # 闭仓区间包住当前 allocation 的开仓时间，且观察时带单员已空仓。
    snapshot = current_snapshot("LONG")
    snapshot["openPositions"] = []
    t.lead.snapshot_json = json.dumps(snapshot)
    await t.db.commit()
    exchange = OfflineExchange()
    await t.router._reconcile_closed_position(t.db, t.binding, t.lead, t.account, exchange,
        {"id": "closed-cycle", "symbol": "BTCUSDT", "side": "LONG", "opened": 100, "closed": 200},
        [t.pos], {"BTCUSDT": t.spec}, {})
    assert Decimal(exchange.orders[0]["sz"]) == 1 and own.owned_qty == 0 and other.owned_qty == 9


@pytest.mark.parametrize("source_state", ["missing", "stale", "future", "invalid_json", "unopened"])
async def test_unproven_exit_is_deferred_without_breaker_and_retries(trading, source_state):
    # 证据不足不等价于下单失败，也不能提前消费投递。
    t = trading
    own, _, _, _ = await seed_allocation(t)
    t.binding.consecutive_failures = 4
    # 用真实持久化投递验证延期不会被 APPLIED 状态吞掉。
    delivery = BindingDelivery(
        binding_id=t.binding.id, source_event_id="test-event", source_sequence=NOW_MS - 150_000,
        payload_json=json.dumps({
            "symbol": "BTCUSDT", "side": "SELL", "positionSide": "LONG", "qty": "1",
            "avgPrice": "100", "notional": "100", "_event_id": "test-event",
            "startTime": NOW_MS - 150_000, "endTime": NOW_MS - 150_000,
        }), state="PENDING",
    )
    t.db.add(delivery)
    t.db.info["delivery"] = delivery
    if source_state in ("stale", "future"):
        snapshot = current_snapshot("LONG")
        snapshot["openPositions"] = []
        snapshot["openPositionsMeta"]["asOf"] = NOW_MS - 30_001 if source_state == "stale" else NOW_MS + 1
        t.lead.snapshot_json = json.dumps(snapshot)
    elif source_state == "invalid_json":
        t.lead.snapshot_json = "{"
    elif source_state == "unopened":
        own.opened_at_ms = 0
        snapshot = current_snapshot("LONG")
        snapshot["openPositions"] = []
        t.lead.snapshot_json = json.dumps(snapshot)
    await t.db.commit()
    exchange = OfflineExchange()
    await execute_batch(t, exchange, source_proven=False)
    assert exchange.orders == [] and own.owned_qty == 1
    record = await t.db.scalar(select(ExecutionRecord))
    assert record.status == "DEFERRED_SOURCE_UNPROVEN"
    assert t.binding.consecutive_failures == 4 and not t.binding.circuit_breaker_tripped
    assert delivery.state == "PENDING" and delivery.exit_target_qty is None
    assert (await t.db.scalars(select(OrderIntent))).all() == []
    own.opened_at_ms = 100
    # 恢复必须来自显式的新观察，不能靠通用夹具覆盖缺陷。
    snapshot = current_snapshot("LONG")
    snapshot["openPositions"] = []
    t.lead.snapshot_json = json.dumps(snapshot)
    await t.db.commit()
    await execute_batch(t, exchange, source_proven=False)
    assert len(exchange.orders) == 1 and own.owned_qty == 0
    assert delivery.state == "APPLIED" and delivery.exit_remaining_qty == "0"


async def prepare_tracked_exit(t, exchange, monkeypatch):
    # 持久化已跟随的开仓水位，覆盖真实循环里的 WAITING_SOURCE 门禁。
    allocation, _, _, _ = await seed_allocation(t)
    allocation.opened_at_ms = NOW_MS - 300_000
    await prepare_event_router(t, exchange, monkeypatch)
    exchange.get_positions.return_value = [t.pos]
    snapshot = current_snapshot("LONG")
    snapshot["openPositions"] = []
    t.lead.snapshot_json = json.dumps(snapshot)
    t.db.add(BindingPositionProgress(
        binding_id=t.binding.id, symbol="BTCUSDT", position_side="LONG",
        source_time_ms=NOW_MS - 300_000, source_event_id="prior-open",
        source_remaining_qty="1", cycle_tracked=True,
    ))
    await t.db.commit()
    return snapshot, [source_order("tracked-exit", "LONG", "SELL", NOW_MS - 150_000)]


@pytest.mark.parametrize("gate", ["source_recheck", "admission_recheck"])
async def test_unsubmitted_source_exit_retries_after_restart_in_real_delivery_loop(trading, monkeypatch, gate):
    # 意图已落库但未 POST 的延期，在新路由器中仍须能重试同一冻结目标。
    t, exchange = trading, OfflineExchange()
    snapshot, orders = await prepare_tracked_exit(t, exchange, monkeypatch)
    t.binding.consecutive_failures = 4
    await t.db.commit()
    commit = t.db.commit
    validate = t.router._validate_exit_source
    changed = False

    async def expire_snapshot():
        nonlocal changed
        stale = current_snapshot("LONG")
        stale["openPositions"] = []
        stale["openPositionsMeta"]["asOf"] = NOW_MS - 30_001
        t.lead.snapshot_json = json.dumps(stale)
        changed = True
        await commit()

    async def commit_with_expiry():
        submitting = any(isinstance(row, OrderIntent) and row.state == "SUBMITTING" for row in t.db.new)
        await commit()
        if submitting and not changed:
            await expire_snapshot()

    async def validate_with_expiry(*args, **kwargs):
        result = await validate(*args, **kwargs)
        intent = await t.db.scalar(select(OrderIntent).where(OrderIntent.state == "SUBMITTING"))
        if intent is not None and not changed:
            await expire_snapshot()
        return result

    if gate == "source_recheck":
        monkeypatch.setattr(t.db, "commit", commit_with_expiry)
    else:
        monkeypatch.setattr(t.router, "_validate_exit_source", validate_with_expiry)
    await t.router.process_lead_events(t.db, t.lead, orders, [], snapshot, [t.proxy])
    monkeypatch.setattr(t.db, "commit", commit)
    monkeypatch.setattr(t.router, "_validate_exit_source", validate)
    assert changed and exchange.orders == []
    intent = await t.db.scalar(select(OrderIntent))
    delivery = await t.db.scalar(select(BindingDelivery).where(BindingDelivery.binding_id == t.binding.id))
    assert intent.state == "REJECTED" and intent.filled_qty == "0" and intent.exchange_order_id is None
    assert delivery.state == "PENDING" and delivery.exit_progress == "SOURCE_UNPROVEN"
    assert delivery.exit_target_qty == delivery.exit_remaining_qty == "1"
    assert (await t.db.scalar(select(ExecutionRecord))).status == "DEFERRED_SOURCE_UNPROVEN"
    assert t.binding.consecutive_failures == 4 and not t.binding.circuit_breaker_tripped
    t.lead.snapshot_json = json.dumps(snapshot)
    await t.db.commit()
    async with get_session_factory()() as restarted:
        router = CopyRouter(t.router.vault)
        lead = await restarted.get(LeadSource, t.lead.id)
        await router.process_lead_events(restarted, lead, orders, [], snapshot, [t.proxy])
        await router.process_lead_events(restarted, lead, orders, [], snapshot, [t.proxy])
    await t.db.refresh(delivery)
    assert len(exchange.orders) == 1 and exchange.orders[0]["reduce_only"] is True
    assert delivery.state == "APPLIED" and delivery.exit_remaining_qty == "0"


@pytest.mark.parametrize("outcome", ["terminal_zero", "terminal_zero_mislabeled", "partial", "unknown", "review"])
async def test_source_retry_marker_never_bypasses_prior_order_or_review_gates(trading, monkeypatch, outcome):
    # 真实 POST 的零成交、部分成交、UNKNOWN 和人工复核不能获未提交重试特权。
    t = trading
    exchange = OfflineExchange(fill=0.5 if outcome == "partial" else 0,
                               state="new" if outcome == "unknown" else "canceled")
    snapshot, orders = await prepare_tracked_exit(t, exchange, monkeypatch)
    await t.router.process_lead_events(t.db, t.lead, orders, [], snapshot, [t.proxy])
    assert len(exchange.orders) == 1
    delivery = await t.db.scalar(select(BindingDelivery).where(BindingDelivery.binding_id == t.binding.id))
    if outcome != "terminal_zero":
        delivery.exit_progress = "SOURCE_UNPROVEN"
    if outcome == "review":
        state = await t.db.get(BindingPositionProgress, (t.binding.id, "BTCUSDT", "LONG"))
        # 审查结论是派生 verdict：用超前的顺序水位作证据让重算立起结论，
        # 而不是直接种一个无证据的结论。
        state.source_time_ms = NOW_MS + 4000
        state.source_event_id = "seeded-watermark"
    await t.db.commit()
    await t.router.process_lead_events(t.db, t.lead, orders, [], snapshot, [t.proxy])
    assert len(exchange.orders) == 1 and delivery.state == "PENDING"
    if outcome == "unknown":
        assert (await t.db.scalar(select(OrderIntent))).state == "UNKNOWN"
    else:
        assert delivery.exit_progress == ("REVIEW_REQUIRED" if outcome == "review" else "WAITING_SOURCE")


async def test_cumulative_fill_delta_does_not_reuse_cumulative_realized_pnl(trading, monkeypatch):
    # 数量可以按累计值差分，未存收益基线时不能把整单收益记到后续增量。
    t, exchange = trading, OfflineExchange()
    await seed_allocation(t, qty=2)
    await prepare_event_router(t, exchange, monkeypatch)
    exchange.get_positions.return_value = [t.pos]
    order = source_order("cumulative-pnl", "LONG", "SELL", NOW_MS - 150_000)
    order.update(totalPnl=10, pnlKnown=True)
    snapshot = current_snapshot("LONG")
    t.lead.snapshot_json = json.dumps(snapshot)
    await t.db.commit()
    await t.router.process_lead_events(t.db, t.lead, [order], [], snapshot, [t.proxy])
    order = {**order, "executedQty": "2", "orderUpdateTime": NOW_MS - 50_000, "totalPnl": 20}
    snapshot["openPositions"] = []
    t.lead.snapshot_json = json.dumps(snapshot)
    await t.db.commit()
    await t.router.process_lead_events(t.db, t.lead, [order], [], snapshot, [t.proxy])
    deliveries = (await t.db.scalars(select(BindingDelivery).where(
        BindingDelivery.binding_id == t.binding.id).order_by(BindingDelivery.source_sequence))).all()
    first, delta = [json.loads(delivery.payload_json) for delivery in deliveries]
    assert len(exchange.orders) == 2 and first["pnlKnown"] is True and first["totalPnl"] == 10
    assert delta["qty"] == "1" and delta["pnlKnown"] is False
    assert delta["pnl"] is None and delta["totalPnl"] is None


async def test_local_close_validation_rejection_creates_no_unknown_intent(trading, monkeypatch):
    # 本地过滤器拒绝发生在提交前，不查单、不产生未决意图。
    t, exchange = trading, OfflineExchange()
    await seed_allocation(t)
    monkeypatch.setattr(t.spec, "validate_order", Mock(side_effect=ValueError("invalid market quantity")))
    exchange.get_order = AsyncMock()
    await execute_batch(t, exchange)
    assert exchange.orders == []
    exchange.get_order.assert_not_awaited()
    assert (await t.db.scalars(select(OrderIntent))).all() == []
    assert (await t.db.scalar(select(ExecutionRecord))).status == "REJECTED"
    assert t.binding.consecutive_failures == 1


async def test_definite_rejection_is_terminal_and_does_not_block_next_delivery(trading):
    # 拒单无查单、无 UNKNOWN 意图，独立下一笔仍可执行。
    t = trading
    exchange = OfflineExchange()
    exchange.place_order = AsyncMock(side_effect=OrderRejected("binance", "-2019", "Insufficient margin"))
    exchange.get_order = AsyncMock()
    await execute_batch(t, exchange, side="BUY", remaining=1, event="rejected")
    assert (await t.db.scalar(select(OrderIntent))).state == "REJECTED"
    assert (await t.db.scalar(select(ExecutionRecord))).status == "REJECTED"
    exchange.get_order.assert_not_awaited()
    successor = OfflineExchange()
    await execute_batch(t, successor, side="BUY", remaining=1, event="successor")
    assert len(successor.orders) == 1 and t.binding.consecutive_failures == 0


async def test_orphan_close_is_binding_scoped(trading):
    t = trading
    own, other, _, _ = await seed_allocation(t)
    t.lead.snapshot_json = json.dumps({
        "meta": {"detailOk": True}, "openPositions": [],
        "openPositionsMeta": {"source": "api", "authoritative": True, "complete": True,
                              "positionShow": True, "partial": False, "emptyVerified": True, "asOf": NOW_MS},
        "closed": [{"symbol": "BTCUSDT", "side": "LONG", "opened": 100, "closed": 200}],
    })
    await t.db.commit()
    exchange = OfflineExchange()
    actions = await t.router.reconcile_orphan_positions(t.db, t.lead, t.account, t.binding, exchange, [], [t.pos],
        {"BTCUSDT": t.spec}, resilience_cfg={"auto_close_orphan_positions": "auto_close"},
        notify_cfg={"notify_on_execution": False, "notify_on_risk_block": False})
    assert actions == ["closed:BTCUSDT:long"]
    assert Decimal(exchange.orders[0]["sz"]) == 1 and own.owned_qty == 0 and other.owned_qty == 9


async def test_protective_stop_only_closes_binding_allocation(trading):
    t = trading
    own, other, _, _ = await seed_allocation(t)
    t.binding.tp_sl_mode = "independent"
    t.binding.enable_stop_loss = True
    t.binding.stop_loss_pct = 5
    await t.db.commit()
    exchange = OfflineExchange(price=90)
    exchange.get_orderbook.return_value = OrderBook([[90, 100]], [[90.1, 100]])
    actions = await t.router.check_position_guards(t.db, t.lead, t.account, t.binding, exchange, [t.pos], {"BTCUSDT": t.spec},
        notify_cfg={"notify_on_execution": False, "notify_on_risk_block": False})
    assert actions == ["BTCUSDT:long"]
    assert Decimal(exchange.orders[0]["sz"]) == 1 and own.owned_qty == 0 and other.owned_qty == 9


async def test_missing_protective_quote_blocks_without_faking_entry_price(trading):
    t = trading
    own, _, _, guard = await seed_allocation(t)
    t.binding.tp_sl_mode = "independent"
    t.binding.enable_stop_loss = True
    await t.db.commit()
    exchange = OfflineExchange()
    exchange.get_orderbook.side_effect = TimeoutError("offline")
    assert await t.router.check_position_guards(t.db, t.lead, t.account, t.binding, exchange, [t.pos], {"BTCUSDT": t.spec}) == []
    assert exchange.orders == [] and own.owned_qty == 1
    assert t.binding.circuit_breaker_tripped is True and t.account.status == "error"
    assert await t.db.get(PositionGuardState, guard.id) is not None


async def test_partial_ioc_then_terminal_does_not_send_market_fallback(trading):
    t = trading
    exchange = OfflineExchange()
    exchange.get_order = AsyncMock(side_effect=[
        {"ordId": "one", "state": "new", "accFillSz": "0.4", "avgPx": "100"},
        {"ordId": "one", "state": "canceled", "accFillSz": "0.4", "avgPx": "100"},
    ])
    await execute_batch(t, exchange, side="BUY", qty=1, remaining=1)
    own = await t.db.scalar(select(PositionAllocation).where(PositionAllocation.binding_id == t.binding.id))
    assert len(exchange.orders) == 1 and own.owned_qty_exact == "0.4"
    assert (await t.db.scalar(select(OrderIntent))).state == "CANCELED"
    assert await t.db.scalar(select(PositionGuardState)) is not None


async def test_missing_cumulative_field_can_use_confirmed_submission_fill(trading):
    t = trading
    exchange = OfflineExchange()
    exchange.place_order = AsyncMock(return_value=OrderResult("accepted", "cid", fill_sz=1, avg_px=100, state="filled"))
    exchange.get_order = AsyncMock(return_value={"ordId": "accepted", "state": "filled"})
    result = await t.router._submit_confirmed(t.db, t.binding, t.account, exchange, t.spec,
        source_event_id="confirmed-result", target_usd=100, inst_id="BTCUSDT", side="buy", pos_side="long",
        ord_type="ioc", sz="1", px="100", reduce_only=False, td_mode="cross")
    assert result.fill_sz == 1
    own = await t.db.scalar(select(PositionAllocation).where(PositionAllocation.binding_id == t.binding.id))
    assert own.owned_qty == 1


async def test_crash_after_exchange_fill_before_commit_recovers_without_resend(trading, monkeypatch):
    t = trading
    exchange = OfflineExchange()
    original_commit = t.db.commit
    count = 0

    async def interrupted_commit():
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("simulated commit failure after fill")
        await original_commit()

    binding_id, account_id = t.binding.id, t.account.id
    order = dict(source_event_id="crash-after-fill", target_usd=100, inst_id="BTCUSDT", side="buy", pos_side="long",
                 ord_type="ioc", sz="1", px="100", reduce_only=False, td_mode="cross")
    with monkeypatch.context() as patch:
        patch.setattr(t.db, "commit", interrupted_commit)
        with pytest.raises(RuntimeError, match="simulated commit failure"):
            await t.router._submit_confirmed(t.db, t.binding, t.account, exchange, t.spec, **order)
    await t.db.rollback()
    async with get_session_factory()() as db:
        binding = await db.get(CopyBinding, binding_id)
        account = await db.get(TradingAccount, account_id)
        result = await CopyRouter(CredentialVault())._submit_confirmed(db, binding, account, exchange, t.spec, **order)
        assert result.fill_sz == 1 and len(exchange.orders) == 1
        allocation = await db.scalar(select(PositionAllocation).where(PositionAllocation.binding_id == binding_id))
        assert allocation.owned_qty == 1
