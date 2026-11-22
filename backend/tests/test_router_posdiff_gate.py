"""Regressions for the 2026-11-22 allocation gate and public-lead position-diff follow.

- A disabled binding's frozen ledger must not block the live binding's opens,
  closes, or adoption (exec 158 BLOCKED_ALLOCATION_MISMATCH on 2026-11-22:
  frozen ZEC 0.673 from stopped binding #3 + live ZEC 0.842 = 1.515 > physical 0.842).
- An add that is already followed must not buy twice when the same increase
  arrives through both the order stream and the position-diff fast path
  (the order stream lags the public snapshot by ~2 minutes).
- The position-diff fast path must baseline on first sight and never backfill
  a pre-existing gap (user: do not auto-fill the missed ETH add).
"""
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.database import get_session_factory
from app.core.security import CredentialVault
from app.models.domain import (
    BindingSourceProgress, CopyBinding, ExecutionRecord, LeadSource,
    PositionAllocation, TradingAccount,
)
from app.services.adapters.base import BalanceInfo, ExchangePosition, InstrumentSpec, OrderBook, OrderResult
from app.services.engine.router import CopyRouter


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
        self.get_usdt_balance = AsyncMock(return_value=BalanceInfo(
            eq=1000.0, avail_eq=1000.0, cash_bal=1000.0, upl=0.0, currency="USDT"))
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


async def seed_allocations(t, own_qty=1, other_qty=9, physical_qty=None):
    """Seed the primary allocation and the other binding's allocation; set physical."""
    own = PositionAllocation(binding_id=t.binding.id, account_id=t.account.id, inst_id="BTCUSDT",
                             pos_side="long", owned_qty=own_qty, entry_cost=own_qty * 100, opened_at_ms=100)
    other = PositionAllocation(binding_id=t.other.id, account_id=t.account.id, inst_id="BTCUSDT",
                               pos_side="long", owned_qty=other_qty, entry_cost=other_qty * 100)
    t.db.add_all([own, other])
    await t.db.commit()
    qty = own_qty + other_qty if physical_qty is None else physical_qty
    t.pos.pos = t.pos.avail_pos = qty
    t.pos.notional_usd = qty * 100
    return own, other


async def run_batch(t, exchange, side="SELL", qty=1, remaining=0, event="test-event",
                    position_side="LONG", avg_price="100", positions=None):
    batch = {"symbol": "BTCUSDT", "side": side, "positionSide": position_side, "qty": str(qty),
             "avgPrice": avg_price, "notional": str(Decimal(str(qty)) * 100), "_event_id": event,
             "startTime": NOW_MS - 150_000, "endTime": NOW_MS - 150_000}
    is_open = (side, position_side) in (("BUY", "LONG"), ("SELL", "SHORT"))
    if not is_open and not t.lead.snapshot_json:
        snapshot = {"profile": {"marginBalance": 1000}, "openPositions": (
            [{"symbol": "BTCUSDT", "positionSide": position_side, "qty": remaining, "qtyExact": str(remaining)}]
            if remaining else []),
            "openPositionsMeta": {"source": "api", "authoritative": True, "complete": True,
                                  "positionShow": True, "asOf": NOW_MS}}
        t.lead.snapshot_json = json.dumps(snapshot)
        await t.db.commit()
    await t.router._execute_batch_for_binding(
        t.db, t.binding, t.lead, t.account, exchange, batch,
        lead_margin_balance=1000, active_lead_positions={f"BTCUSDT_{position_side}": remaining}, my_equity=1000,
        positions=positions if positions is not None else ([] if is_open else [t.pos]),
        instruments={"BTCUSDT": t.spec}, global_kill=False,
        notify_cfg={}, resilience_cfg=None, risk_defaults=None,
    )
    await t.db.commit()


async def execution_statuses(t):
    rows = (await t.db.scalars(select(ExecutionRecord).where(
        ExecutionRecord.binding_id == t.binding.id))).all()
    return [row.status for row in rows]


def public_snapshot(lead_qty):
    """Authoritative fresh public-leader snapshot with one LONG BTCUSDT leg.

    Shape mirrors production: source/positionShow live in openPositionsMeta,
    NOT in the top-level meta (which only carries generatedAt/detailOk).
    A regression here caught the 2026-11-22 bug where the fast path read the
    top-level meta and never ran in production.
    """
    return {
        "meta": {"generatedAt": NOW_MS, "detailOk": True},
        "profile": {"marginBalance": 1000},
        "openPositionsMeta": {"source": "api", "positionShow": True, "asOf": NOW_MS,
                              "complete": True, "authoritative": True},
        "openPositions": [{"symbol": "BTCUSDT", "positionSide": "LONG", "qty": lead_qty,
                           "qtyExact": str(lead_qty), "markPrice": 100, "currentPrice": 100}],
    }


async def run_position_diff(t, exchange, snapshot, lead_qty):
    active = {f"BTCUSDT_LONG": Decimal(str(lead_qty))}
    await t.router._follow_position_diffs(
        t.db, t.binding, t.lead, t.account, exchange, {"BTCUSDT": t.spec}, snapshot,
        active, 1000, {}, None, None)
    await t.db.commit()


async def test_disabled_binding_frozen_allocation_does_not_block_open(trading):
    # 停用绑定的冻结账本（9 张）不在实盘上：实盘 0 张，主绑定也 0 张。
    # 修之前 claimed=9 > physical=0 会 BLOCKED_ALLOCATION_MISMATCH。
    # 主绑定 0 张是为了让已跟足保护不抢先跳过（目标 1 张、缺口 1 张）。
    t = trading
    await seed_allocations(t, own_qty=0, other_qty=9, physical_qty=0)
    t.other.is_enabled = False
    await t.db.commit()
    exchange = OfflineExchange()
    await run_batch(t, exchange, side="BUY", qty=1, remaining=1, positions=[t.pos])
    assert "BLOCKED_ALLOCATION_MISMATCH" not in await execution_statuses(t)
    assert len(exchange.orders) == 1


async def test_enabled_binding_frozen_allocation_still_blocks_open(trading):
    # 对照：对方绑定仍启用时，账实不符仍应拦截（门本身没被拆掉）。
    t = trading
    await seed_allocations(t, own_qty=0, other_qty=9, physical_qty=0)
    exchange = OfflineExchange()
    await run_batch(t, exchange, side="BUY", qty=1, remaining=1, positions=[t.pos])
    assert "BLOCKED_ALLOCATION_MISMATCH" in await execution_statuses(t)
    assert len(exchange.orders) == 0


async def test_disabled_binding_frozen_allocation_does_not_block_close(trading):
    # 平仓 reserved 同样只算启用中绑定：实盘 1 张只够主绑定的 1 张，
    # 停用绑定的 9 张冻结账本不再占用预留。
    t = trading
    await seed_allocations(t, own_qty=1, other_qty=9, physical_qty=1)
    t.other.is_enabled = False
    await t.db.commit()
    exchange = OfflineExchange()
    await run_batch(t, exchange, side="SELL", qty=1, remaining=0)
    assert "BLOCKED_ALLOCATION_DEFICIT" not in await execution_statuses(t)
    assert len(exchange.orders) == 1


async def test_enabled_binding_frozen_allocation_still_blocks_close(trading):
    # 对照：对方绑定仍启用时，平仓预留不足仍应拦截。
    t = trading
    await seed_allocations(t, own_qty=1, other_qty=9, physical_qty=1)
    exchange = OfflineExchange()
    await run_batch(t, exchange, side="SELL", qty=1, remaining=0)
    assert "BLOCKED_ALLOCATION_DEFICIT" in await execution_statuses(t)
    assert len(exchange.orders) == 0


async def test_already_followed_add_is_skipped(trading):
    # 账上已跟足目标量（带单 1 张、我方 1 张），同一加仓再到时直接跳过，不重复买。
    t = trading
    await seed_allocations(t, own_qty=1, other_qty=0, physical_qty=1)
    exchange = OfflineExchange()
    await run_batch(t, exchange, side="BUY", qty=1, remaining=1, positions=[t.pos])
    assert "SKIPPED_ALREADY_FOLLOWED" in await execution_statuses(t)
    assert len(exchange.orders) == 0


async def test_partial_add_is_capped_to_residual(trading):
    # 带单 1 张、我方已有 0.5 张：加仓事件按 1 张算，但只补差额 0.5 张。
    t = trading
    await seed_allocations(t, own_qty=Decimal("0.5"), other_qty=0, physical_qty=Decimal("0.5"))
    exchange = OfflineExchange()
    await run_batch(t, exchange, side="BUY", qty=1, remaining=1, positions=[t.pos])
    assert len(exchange.orders) == 1
    assert Decimal(str(exchange.orders[0]["sz"])) == Decimal("0.5")


async def test_position_diff_first_run_baselines_without_trading(trading):
    # 快照快路首次见到品种只建基线、不交易（不上线前已存在的缺口）。
    t = trading
    exchange = OfflineExchange()
    await run_position_diff(t, exchange, public_snapshot(1), 1)
    assert len(exchange.orders) == 0
    progress = await t.db.get(BindingSourceProgress, (t.binding.id, "posdiff:BTCUSDT:LONG"))
    assert progress is not None and progress.cumulative_qty == "1"


async def test_position_diff_does_not_backfill_existing_gap(trading):
    # 基线已建（带单 1 张），我方 0 张：带单没动之前快路不补旧缺口。
    t = trading
    exchange = OfflineExchange()
    await run_position_diff(t, exchange, public_snapshot(1), 1)
    await run_position_diff(t, exchange, public_snapshot(1), 1)
    assert len(exchange.orders) == 0


async def test_position_diff_increase_trades_delta_and_late_order_event_is_skipped(trading):
    # 带单 1→2 张：快路按增量买 1 张；滞后到达的订单流事件（同一次加仓）
    # 因账上已跟足而跳过，不重复买入。
    t = trading
    await seed_allocations(t, own_qty=1, other_qty=0, physical_qty=1)
    exchange = OfflineExchange()
    await run_position_diff(t, exchange, public_snapshot(1), 1)
    assert len(exchange.orders) == 0
    await run_position_diff(t, exchange, public_snapshot(2), 2)
    assert len(exchange.orders) == 1
    assert Decimal(str(exchange.orders[0]["sz"])) == Decimal("1")
    # 快路成交后实盘也涨到 2 张，否则账实门会先拦住后面的订单流事件。
    t.pos.pos = t.pos.avail_pos = 2
    t.pos.notional_usd = 200
    await run_batch(t, exchange, side="BUY", qty=1, remaining=2, positions=[t.pos],
                    event="late-order-event")
    assert len(exchange.orders) == 1
    assert "SKIPPED_ALREADY_FOLLOWED" in await execution_statuses(t)
