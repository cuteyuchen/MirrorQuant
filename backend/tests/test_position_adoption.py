"""认领已有仓位：接入时把与带单员持仓一致的已有仓位记为跟单仓位。

匹配口径与 sizing 一致：仓位名义金额 / 账户权益（敞口比率），相对差异
在 adopt_tolerance_pct 内即视为同一仓位。认领只改账、不下单；认领后
带单员减仓/平仓按归属量跟随处理。同一价格同时算两侧名义金额，价格取值
不影响匹配结论，只影响记账成本。
"""
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.domain import (
    BindingDelivery, ExecutionRecord, PositionAllocation, PositionGuardState,
)
from app.services.adapters.base import BalanceInfo, ExchangePosition
from app.services.engine.router import _exposure_ratio_matches
from app.services.notifier.trade import trade_message
from test_router_remediations import (
    NOW_MS, OfflineExchange, current_snapshot, execute_batch, prepare_event_router, trading,
)


@pytest.mark.parametrize("lead,mine,tol,expected", [
    ("2.0", "2.0", "10", True),
    ("2.0", "1.9", "10", True),
    ("2.0", "2.1", "10", True),
    ("2.0", "2.2", "10", True),   # +10% 边界含在内
    ("2.0", "1.8", "10", True),   # -10% 边界含在内
    ("2.0", "1.79", "10", False),
    ("2.0", "2.21", "10", False),
    ("2.0", "2.0", "0", True),
    ("2.0", "2.01", "0", False),
    ("0", "2.0", "10", False),
    ("2.0", "0", "10", False),
    ("2.0", "2.0", "-1", False),
    ("bad", "2.0", "10", False),
    ("2.0", None, "10", False),
])
def test_exposure_ratio_matches(lead, mine, tol, expected):
    assert _exposure_ratio_matches(lead, mine, tol) is expected


def _position(pos, avg_px=100, side="long", cur_px=0.0):
    return ExchangePosition("BTCUSDT", "BTCUSDT", side, "cross", pos, pos, avg_px, 0, 0, pos * avg_px, 10, cur_px)


def _snapshot(qty="1", side="LONG", margin=1000, extra=None):
    snapshot = current_snapshot(side)
    position = {"symbol": "BTCUSDT", "positionSide": side, "qty": float(qty), "qtyExact": str(qty)}
    if extra:
        position.update(extra)
    snapshot["openPositions"] = [position]
    snapshot["profile"]["marginBalance"] = margin
    return snapshot


def _exchange_with(positions, equity=1000):
    exchange = OfflineExchange()
    exchange.get_positions.return_value = positions
    exchange.get_usdt_balance = AsyncMock(
        return_value=BalanceInfo(eq=equity, avail_eq=equity, cash_bal=equity, upl=0))
    return exchange


async def _adopt(t, exchange, snapshot, received_at_ms=NOW_MS, margin=1000):
    return await t.router._adopt_matching_positions(
        t.db, t.binding, t.lead, t.account, exchange, {"BTCUSDT": t.spec},
        snapshot, margin, received_at_ms, {})


async def _allocation(t):
    return await t.db.scalar(select(PositionAllocation).where(
        PositionAllocation.binding_id == t.binding.id,
        PositionAllocation.inst_id == "BTCUSDT",
        PositionAllocation.pos_side == "long"))


async def test_baseline_run_adopts_matching_position(trading, monkeypatch):
    t = trading
    exchange = _exchange_with([_position(1)])
    await prepare_event_router(t, exchange, monkeypatch)
    t.binding.baseline_seeded = False
    await t.db.commit()
    await t.router.process_lead_events(t.db, t.lead, [], [], _snapshot(), [t.proxy])
    allocation = await _allocation(t)
    assert allocation.owned_qty_exact == "1" and allocation.entry_cost_exact == "100"
    assert allocation.opened_at_ms == NOW_MS and allocation.generation == 1
    guard = await t.db.scalar(select(PositionGuardState).where(
        PositionGuardState.binding_id == t.binding.id, PositionGuardState.inst_id == "BTCUSDT"))
    assert guard is not None and guard.entry_px == 100
    record = await t.db.scalar(select(ExecutionRecord))
    assert record.status == "ADOPTED_POSITION" and record.action == "ADOPT"
    assert record.details == ("Adopted existing position: my exposure 0.10x, "
                              "lead exposure 0.10x, tolerance 10.0%")
    assert record.my_qty == 1 and record.my_price == 100 and record.my_notional == 100
    await t.db.refresh(t.binding)
    assert t.binding.adoption_completed is True
    assert exchange.orders == []  # 认领只改账，不下单。


async def test_ratio_outside_tolerance_is_not_adopted(trading, monkeypatch):
    t = trading
    exchange = _exchange_with([_position(1.2)])
    await prepare_event_router(t, exchange, monkeypatch)
    t.binding.baseline_seeded = False
    await t.db.commit()
    await t.router.process_lead_events(t.db, t.lead, [], [], _snapshot(), [t.proxy])
    assert await _allocation(t) is None
    assert (await t.db.scalars(select(ExecutionRecord))).all() == []
    await t.db.refresh(t.binding)
    assert t.binding.adoption_completed is True


async def test_tolerance_boundary_is_inclusive(trading):
    t = trading
    exchange = _exchange_with([_position(1.1)])
    adopted = await _adopt(t, exchange, _snapshot())
    assert len(adopted) == 1 and adopted[0]["qty"] == 1.1
    assert (await _allocation(t)).owned_qty_exact == "1.1"


async def test_toggle_off_skips_adoption_without_marking_complete(trading, monkeypatch):
    t = trading
    exchange = _exchange_with([_position(1)])
    await prepare_event_router(t, exchange, monkeypatch)
    t.binding.baseline_seeded = False
    t.binding.adopt_existing_positions = False
    await t.db.commit()
    await t.router.process_lead_events(t.db, t.lead, [], [], _snapshot(), [t.proxy])
    assert await _allocation(t) is None
    await t.db.refresh(t.binding)
    assert t.binding.adoption_completed is False


async def test_already_owned_position_is_not_readopted(trading):
    t = trading
    t.db.add(PositionAllocation(binding_id=t.binding.id, account_id=t.account.id, inst_id="BTCUSDT",
                                pos_side="long", owned_qty=0.5, owned_qty_exact="0.5",
                                entry_cost=50, entry_cost_exact="50", generation=1, opened_at_ms=100))
    await t.db.commit()
    exchange = _exchange_with([_position(1)])
    assert await _adopt(t, exchange, _snapshot()) == []
    assert (await _allocation(t)).owned_qty_exact == "0.5"
    assert (await t.db.scalars(select(ExecutionRecord))).all() == []


async def test_only_unclaimed_remainder_is_adopted(trading):
    t = trading
    t.db.add(PositionAllocation(binding_id=t.other.id, account_id=t.account.id, inst_id="BTCUSDT",
                                pos_side="long", owned_qty=0.6, owned_qty_exact="0.6",
                                entry_cost=60, entry_cost_exact="60", generation=1, opened_at_ms=100))
    await t.db.commit()
    exchange = _exchange_with([_position(1.6)])
    adopted = await _adopt(t, exchange, _snapshot())
    assert len(adopted) == 1 and adopted[0]["qty"] == 1.0
    assert (await _allocation(t)).owned_qty_exact == "1"


async def test_opposite_side_is_not_adopted(trading):
    t = trading
    exchange = _exchange_with([_position(1, side="short")])
    assert await _adopt(t, exchange, _snapshot()) == []
    assert await _allocation(t) is None


async def test_blacklisted_symbol_is_not_adopted(trading):
    t = trading
    t.binding.symbol_blacklist_json = json.dumps(["BTCUSDT"])
    await t.db.commit()
    exchange = _exchange_with([_position(1)])
    assert await _adopt(t, exchange, _snapshot()) == []
    assert await _allocation(t) is None


async def test_adoption_runs_once(trading, monkeypatch):
    t = trading
    exchange = _exchange_with([_position(1)])
    await prepare_event_router(t, exchange, monkeypatch)
    t.binding.baseline_seeded = False
    await t.db.commit()
    snapshot = _snapshot()
    await t.router.process_lead_events(t.db, t.lead, [], [], snapshot, [t.proxy])
    await t.router.process_lead_events(t.db, t.lead, [], [], snapshot, [t.proxy])
    records = (await t.db.scalars(select(ExecutionRecord))).all()
    assert len(records) == 1 and records[0].status == "ADOPTED_POSITION"


async def test_no_positions_marks_completed(trading):
    t = trading
    exchange = _exchange_with([])
    assert await _adopt(t, exchange, _snapshot()) == []
    await t.db.refresh(t.binding)
    assert t.binding.adoption_completed is True


async def test_balance_failure_defers_adoption(trading):
    t = trading
    exchange = _exchange_with([_position(1)])
    exchange.get_usdt_balance = AsyncMock(side_effect=RuntimeError("synthetic balance unavailable"))
    assert await _adopt(t, exchange, _snapshot()) == []
    await t.db.refresh(t.binding)
    assert t.binding.adoption_completed is False
    assert (await t.db.scalars(select(ExecutionRecord))).all() == []


async def test_mark_price_fallback_when_avg_price_missing(trading):
    t = trading
    exchange = _exchange_with([_position(1, avg_px=0)])
    adopted = await _adopt(t, exchange, _snapshot(extra={"markPrice": 100}))
    assert len(adopted) == 1
    allocation = await _allocation(t)
    assert allocation.owned_qty_exact == "1" and allocation.entry_cost_exact == "100"


async def test_disabled_binding_skips_adoption(trading):
    t = trading
    t.binding.is_enabled = False
    await t.db.commit()
    exchange = _exchange_with([_position(1)])
    assert await _adopt(t, exchange, _snapshot()) == []
    await t.db.refresh(t.binding)
    assert t.binding.adoption_completed is False


async def test_auto_adoption_skipped_when_binding_has_allocations(trading, monkeypatch):
    t = trading
    t.db.add(PositionAllocation(binding_id=t.binding.id, account_id=t.account.id, inst_id="BTCUSDT",
                                pos_side="long", owned_qty=1, owned_qty_exact="1",
                                entry_cost=100, entry_cost_exact="100", generation=1, opened_at_ms=100))
    await t.db.commit()
    exchange = _exchange_with([_position(1)])
    await prepare_event_router(t, exchange, monkeypatch)
    await t.router.process_lead_events(t.db, t.lead, [], [], _snapshot(), [t.proxy])
    exchange.get_positions.assert_not_awaited()
    await t.db.refresh(t.binding)
    assert t.binding.adoption_completed is False


async def test_auto_adoption_skipped_when_binding_has_deliveries(trading, monkeypatch):
    t = trading
    t.db.add(BindingDelivery(binding_id=t.binding.id, source_event_id="old-event",
                             source_sequence=1, payload_json="{}", state="APPLIED"))
    await t.db.commit()
    exchange = _exchange_with([_position(1)])
    await prepare_event_router(t, exchange, monkeypatch)
    await t.router.process_lead_events(t.db, t.lead, [], [], _snapshot(), [t.proxy])
    exchange.get_positions.assert_not_awaited()
    assert (await t.db.scalars(select(ExecutionRecord))).all() == []


async def test_exit_follows_adopted_position(trading):
    t = trading
    t.pos.pos = t.pos.avail_pos = 1
    t.pos.notional_usd = 100
    exchange = _exchange_with([t.pos])
    adopted = await _adopt(t, exchange, _snapshot(), received_at_ms=NOW_MS - 200_000)
    assert len(adopted) == 1
    await execute_batch(t, exchange)
    assert len(exchange.orders) == 1 and Decimal(exchange.orders[0]["sz"]) == 1
    assert (await _allocation(t)).owned_qty == 0
    records = (await t.db.scalars(select(ExecutionRecord).order_by(ExecutionRecord.id))).all()
    assert [record.status for record in records] == ["ADOPTED_POSITION", "EXECUTED"]
    assert records[1].my_qty == 1


def test_adoption_notification_names_the_adoption():
    record = SimpleNamespace(symbol="BTCUSDT", position_side="LONG", side="BUY", action="ADOPT",
                             status="ADOPTED_POSITION", lead_qty=1, lead_price=100, lead_notional=100,
                             my_qty=1, my_price=100, my_notional=100, order_id=None,
                             account_id=1, binding_id=2, slippage_pct=0,
                             details="Adopted existing position: my exposure 0.10x, lead exposure 0.10x, tolerance 10.0%")
    msg = trade_message(record, SimpleNamespace(exchange="binance", environment="live"))
    lead, follower = msg.split("\n\n")
    assert "📥 认领仓位 ｜ BTCUSDT 永续 ｜ 做多" in lead
    assert "📥 已认领现有仓位为跟单仓位" in follower
    assert "📋 原因：现有仓位与带单员持仓一致" in follower
    assert "📦 数量 1 ｜ 均价 100 ｜ 金额 100.00 U" in follower
