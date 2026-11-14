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
    BindingDebt, CopyBinding, ExecutionRecord, LeadSource, OrderIntent,
    PositionAllocation, PositionGuardState, ProxyNode, SystemConfig, TradingAccount,
)
from app.services.adapters.base import BalanceInfo, ExchangePosition, InstrumentSpec, OrderBook, OrderResult
from app.services.engine.risk import RiskEngine
from app.services.engine.router import CopyRouter, UnknownOrderOutcome
from app.services.engine.sizer import PositionSizer


NOW_MS = 2_000_000


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
    pending = BindingDebt(binding_id=t.binding.id, symbol="BTCUSDT", position_side="LONG", pending_usd=debt)
    guard = PositionGuardState(binding_id=t.binding.id, inst_id="BTCUSDT", pos_side="long", entry_px=100, peak_px=100, trough_px=100)
    t.db.add_all([allocation, other, pending, guard])
    await t.db.commit()
    return allocation, other, pending, guard


async def execute_batch(t, exchange, side="SELL", qty=1, remaining=0, event="test-event",
                        position_side="LONG", avg_price="100", batch_fields=None, resilience_cfg=None):
    batch = {"symbol": "BTCUSDT", "side": side, "positionSide": position_side, "qty": str(qty),
             "avgPrice": avg_price, "notional": str(Decimal(str(qty)) * 100), "_event_id": event,
             "startTime": NOW_MS - 150_000, "endTime": NOW_MS - 150_000}
    batch.update(batch_fields or {})
    is_open = (side, position_side) in (("BUY", "LONG"), ("SELL", "SHORT"))
    await t.router._execute_batch_for_binding(
        t.db, t.binding, t.lead, t.account, exchange, batch,
        lead_margin_balance=1000, active_lead_positions={f"BTCUSDT_{position_side}": remaining}, my_equity=1000,
        positions=[] if is_open else [t.pos], instruments={"BTCUSDT": t.spec}, global_kill=False,
        notify_cfg={}, resilience_cfg=resilience_cfg,
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
    # Favorable moves also leave the lead's entry window and remain blocked.
    ("BUY", "LONG", 99.3, 99.4, "BLOCKED_CATCHUP_DRIFT"),
    ("SELL", "SHORT", 100.6, 100.7, "BLOCKED_CATCHUP_DRIFT"),
))
async def test_catchup_uses_executable_side_and_symmetric_drift(trading, side, position_side, bid, ask, status):
    t = trading
    executable_price = ask if side == "BUY" else bid
    exchange = OfflineExchange(price=executable_price)
    exchange.get_orderbook.return_value = OrderBook([[bid, 100]], [[ask, 100]])
    await execute_batch(t, exchange, side=side, position_side=position_side, remaining=1)
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


@pytest.mark.parametrize("field", ("missed_order_max_age_sec", "catchup_max_price_dev_pct"))
@pytest.mark.parametrize("value", (0, -1))
async def test_invalid_catchup_limits_are_recorded_before_execution(trading, field, value):
    t = trading
    exchange = OfflineExchange()
    await execute_batch(t, exchange, side="BUY", remaining=1, resilience_cfg={field: value})
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
    await execute_batch(t, exchange, side="BUY", remaining=1,
                        batch_fields={"endTime": NOW_MS - 1_000_000})
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
    assert exchange.ensure_leverage.await_args.args == ("BTCUSDT", 2)
    own = await t.db.scalar(select(PositionAllocation).where(PositionAllocation.binding_id == t.binding.id))
    assert own.owned_qty == 1
    exchange.place_tp_sl.assert_not_awaited()
    exchange.cancel_tp_sl.assert_not_awaited()


async def test_failed_leverage_confirmation_prevents_order(trading):
    t = trading
    exchange = OfflineExchange()
    exchange.ensure_leverage.return_value = False
    with pytest.raises(RuntimeError, match="leverage could not be confirmed"):
        await execute_batch(t, exchange, side="BUY", remaining=1)
    assert exchange.orders == []


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
    exchange = OfflineExchange()
    await t.router._reconcile_closed_position(t.db, t.binding, t.lead, t.account, exchange,
        {"id": "closed-cycle", "symbol": "BTCUSDT", "side": "LONG", "closed": 200},
        [t.pos], {"BTCUSDT": t.spec}, {})
    assert Decimal(exchange.orders[0]["sz"]) == 1 and own.owned_qty == 0 and other.owned_qty == 9


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
