"""Parallel business regressions with explicit, fresh source evidence."""
import json
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database import get_session_factory
from app.models.domain import (
    BindingDebt, ExecutionRecord, OrderIntent, PositionAllocation, PositionGuardState,
    TradingAccount,
)
from app.services.engine.equity import equity_baseline, observe_equity, risk_state
from test_equity_currency_isolation import balance
from test_router_remediations import NOW_MS, OfflineExchange, execute_batch, seed_allocation, trading


CASES = ["currency", "full", "partial", "below_min", "canceled", "new", "direct"]


def evidence_snapshot(remaining=0):
    return {
        "meta": {"generatedAt": NOW_MS, "detailOk": True},
        "openPositions": ([{"symbol": "BTCUSDT", "positionSide": "LONG",
                            "qty": remaining, "qtyExact": str(remaining)}] if remaining else []),
        "openPositionsMeta": {
            "asOf": NOW_MS, "source": "api", "complete": True, "authoritative": True,
            "positionShow": True, "partial": False, "emptyVerified": True,
        },
    }


async def ambiguous_currency(trading):
    account = trading.account
    account.risk_equity_json = None
    account.hwm_equity = account.day_start_equity = account.usdt_equity = 6000
    observe_equity(account, balance("USDC", 5000))
    observe_equity(account, balance("USDT", 1000))
    await trading.db.commit()
    with pytest.raises(ValueError, match="Legacy collateral"):
        equity_baseline(account, "USDT")
    async with get_session_factory()() as restarted:
        account = await restarted.get(TradingAccount, account.id)
        with pytest.raises(ValueError, match="Legacy collateral"):
            equity_baseline(account, "USDC")
        assert risk_state(account)["legacy"]["hwm_equity"] == 6000
    exchange = OfflineExchange()
    trading.lead.snapshot_json = json.dumps(evidence_snapshot(1))
    await trading.db.commit()
    await trading.router._execute_batch_for_binding(
        trading.db, trading.binding, trading.lead, trading.account, exchange,
        {"symbol": "BTCUSDT", "side": "BUY", "positionSide": "LONG", "qty": "1",
         "avgPrice": "100", "notional": "100", "startTime": 200, "endTime": 200,
         "_event_id": "currency-quarantined-entry"},
        1000, {"BTCUSDT_LONG": 1}, 1000, [trading.pos], {"BTCUSDT": trading.spec}, False, {})
    assert exchange.orders == []
    assert (await trading.db.scalar(select(ExecutionRecord))).status == "BLOCKED_COLLATERAL_BALANCE"
    allocation = await trading.db.scalar(select(PositionAllocation).where(
        PositionAllocation.binding_id == trading.binding.id))
    assert allocation.owned_qty == 1


async def business_case(trading, case, defect=None):
    remaining = {"partial": 3, "below_min": 99}.get(case, 0)
    owned, other, debt, guard = await seed_allocation(trading, qty=2 if case == "partial" else 1)
    if case == "currency":
        await ambiguous_currency(trading)
    snapshot = evidence_snapshot(remaining)
    if defect == "stale_snapshot":
        snapshot["openPositionsMeta"]["asOf"] = NOW_MS - 30_001
    trading.lead.snapshot_json = json.dumps(snapshot)
    await trading.db.commit()
    exchange = OfflineExchange(fill=0 if case in ("canceled", "new") else None,
                               state=case if case in ("canceled", "new") else "filled")
    fields = {"startTime": 200, "endTime": 200}
    closed = {"id": "proven-business-close", "symbol": "BTCUSDT", "side": "LONG",
              "opened": 50, "closed": 200}
    if defect == "future_source":
        fields["endTime"] = closed["closed"] = NOW_MS + 1
    if case == "direct":
        result = await trading.router._reconcile_closed_position(
            trading.db, trading.binding, trading.lead, trading.account, exchange, closed,
            [trading.pos], {"BTCUSDT": trading.spec}, {})
        assert result is (defect is None)
    else:
        await execute_batch(trading, exchange, remaining=remaining, batch_fields=fields,
                            event="proven-business-close")
    return owned, other, debt, guard, exchange


@pytest.mark.parametrize("case", CASES)
async def test_original_business_semantics_with_valid_evidence(trading, case):
    owned, other, debt, guard, exchange = await business_case(trading, case)
    assert Decimal(str(other.owned_qty)) == 9
    assert Decimal(str(other.entry_cost)) == 900
    records = (await trading.db.scalars(select(ExecutionRecord))).all()
    if case == "below_min":
        assert exchange.orders == [] and owned.owned_qty == 1 and debt.pending_usd == 25
        assert records[-1].status == "SKIPPED_BELOW_MIN"
    elif case in ("canceled", "new"):
        assert len(exchange.orders) == 1 and owned.owned_qty == 1 and debt.pending_usd == 25
        assert all(record.status != "EXECUTED" for record in records)
        intent = await trading.db.scalar(select(OrderIntent))
        assert Decimal(intent.filled_qty) == 0
        assert intent.state == ("CANCELED" if case == "canceled" else "UNKNOWN")
    elif case == "partial":
        assert len(exchange.orders) == 1 and Decimal(exchange.orders[0]["sz"]) == Decimal("0.5")
        assert owned.owned_qty == 1.5 and owned.entry_cost == 150 and debt.pending_usd == 18.75
    else:
        assert len(exchange.orders) == 1 and Decimal(exchange.orders[0]["sz"]) == 1
        assert exchange.orders[0]["reduce_only"] is True
        assert owned.owned_qty == owned.entry_cost == debt.pending_usd == 0
        assert records[-1].status == "EXECUTED" and records[-1].my_qty == 1
    assert (await trading.db.get(PositionGuardState, guard.id) is not None) is (
        case in ("below_min", "partial", "canceled", "new"))
    if case == "currency":
        assert risk_state(trading.account)["legacy_unverified"] is True
    await trading.db.commit()
    async with get_session_factory()() as restarted:
        saved_owned = await restarted.get(PositionAllocation, owned.id)
        saved_other = await restarted.get(PositionAllocation, other.id)
        saved_debt = await restarted.get(BindingDebt, debt.id)
        assert saved_owned.owned_qty == owned.owned_qty and saved_other.owned_qty == 9
        assert saved_debt.pending_usd == debt.pending_usd


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("defect", ["future_source", "stale_snapshot"])
async def test_original_business_semantics_never_override_invalid_evidence(trading, case, defect):
    owned, other, debt, guard, exchange = await business_case(trading, case, defect)
    assert exchange.orders == []
    assert owned.owned_qty == (2 if case == "partial" else 1) and other.owned_qty == 9
    assert owned.entry_cost == owned.owned_qty * 100 and other.entry_cost == 900
    assert debt.pending_usd == 25 and await trading.db.get(PositionGuardState, guard.id) is not None
    assert await trading.db.scalar(select(OrderIntent)) is None
