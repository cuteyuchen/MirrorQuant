"""State-machine regressions using only in-memory DBs and an offline exchange."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.database import get_session_factory
from app.core.security import CredentialVault
from app.models.domain import (
    BindingDebt, CopyBinding, ExecutionRecord, LeadSource, OrderIntent,
    PositionAllocation, PositionGuardState, TradingAccount,
)
from app.services.adapters.base import ExchangePosition, InstrumentSpec, OrderBook, OrderResult
from app.services.engine.router import CopyRouter, UnknownOrderOutcome


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
async def trading():
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


async def execute_batch(t, exchange, side="SELL", qty=1, remaining=0, event="test-event"):
    await t.router._execute_batch_for_binding(
        t.db, t.binding, t.lead, t.account, exchange,
        {"symbol": "BTCUSDT", "side": side, "positionSide": "LONG", "qty": str(qty), "avgPrice": "100",
         "notional": str(Decimal(str(qty)) * 100), "_event_id": event},
        lead_margin_balance=1000, active_lead_positions={"BTCUSDT_LONG": remaining}, my_equity=1000,
        positions=[t.pos] if side == "SELL" else [], instruments={"BTCUSDT": t.spec}, global_kill=False,
        notify_cfg={},
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
