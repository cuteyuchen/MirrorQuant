"""Currency-local risk and durable legacy quarantine, entirely synthetic/offline."""

import json
from datetime import datetime
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select, text

from app.api.accounts import _account_out, test_account_connectivity as connectivity
from app.core.database import _apply_sqlite_migrations, get_session_factory
from app.core.time_util import BEIJING_TZ
from app.models.domain import ExecutionRecord, PositionAllocation, TradingAccount
from app.services.adapters.base import BalanceInfo
from app.services.engine.equity import collateral_available, equity_baseline, observe_equity, risk_state
from app.services.engine.orchestrator import EngineOrchestrator
from app.services.engine.risk import RiskEngine
from test_router_remediations import NOW_MS, OfflineExchange, execute_batch, trading
from test_binance_usdc_balance import persisted_account


def balance(currency, equity, available=None):
    return BalanceInfo(equity, equity if available is None else available, equity, 0, currency)


async def test_alternating_currency_baselines_survive_new_session_and_day_rollover(trading):
    account = trading.account
    account.hwm_equity = account.day_start_equity = 0
    observe_equity(account, balance("USDT", 1000, 700))
    observe_equity(account, balance("USDC", 100, 20))
    observe_equity(account, balance("USDT", 900, 600))
    observe_equity(account, balance("USDC", 90, 0))
    assert (account.usdt_equity, account.avail_equity, account.hwm_equity, account.day_start_equity) == (900, 600, 1000, 1000)
    assert equity_baseline(account, "USDT") == (1000, 1000)
    assert equity_baseline(account, "USDC") == (100, 100)
    assert collateral_available(account, "USDC") == 0
    await trading.db.commit()
    async with get_session_factory()() as restarted_db:
        restarted = await restarted_db.get(TradingAccount, account.id)
        assert restarted.risk_equity_json == account.risk_equity_json
        assert equity_baseline(restarted, "USDC") == (100, 100)
        assert equity_baseline(restarted, "USDT") == (1000, 1000)
        policy = SimpleNamespace(enable_circuit_breaker=True, max_drawdown_pct=5, daily_max_loss_pct=5)
        for currency, equity in (("USDT", 900), ("USDC", 90)):
            checks = RiskEngine.loss_breaker_checks(policy, *equity_baseline(restarted, currency), equity)
            assert [check.allowed for check in checks] == [False, False]
            assert [check.value for check in checks] == [10, 10]
        state = risk_state(restarted)
        state["assets"]["USDC"]["date"] = "2000-01-01"
        restarted.risk_equity_json = json.dumps(state)
        observe_equity(restarted, balance("USDC", 80, 10))
        assert risk_state(restarted)["assets"]["USDC"]["date"] == datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
        assert equity_baseline(restarted, "USDC") == (100, 80)
        assert equity_baseline(restarted, "USDT") == (1000, 1000)
        assert restarted.day_start_equity == 1000
        await restarted_db.commit()
    await trading.db.refresh(account)
    assert equity_baseline(account, "USDC") == (100, 80)
    assert account.usdt_equity == 900
    assert _account_out(account)["usdcEquity"] == 80


async def test_breaker_checks_usdc_loss_without_polluting_usdt_after_restart(trading):
    account = trading.account
    observe_equity(account, balance("USDT", 1000))
    observe_equity(account, balance("USDC", 100))
    await trading.db.commit()
    observed = BalanceInfo(1000, 1000, 1000, 0, asset_balances={
        "USDT": {"equity": 1000, "available": 1000, "wallet": 1000, "upl": 0},
        "USDC": {"equity": 80, "available": 0, "wallet": 80, "upl": 0}})
    adapter = SimpleNamespace(get_usdt_balance=AsyncMock(return_value=observed))
    engine = EngineOrchestrator()
    assert await engine._check_account_equity(trading.db, trading.binding, account, adapter)
    assert trading.binding.circuit_breaker_tripped
    assert "Daily loss" in trading.binding.circuit_breaker_reason
    assert "[USDC]" in trading.binding.circuit_breaker_reason
    assert account.hwm_equity == 1000 and account.day_start_equity == 1000
    assert account.usdt_equity == 1000 and account.avail_equity == 1000
    assert equity_baseline(account, "USDC") == (100, 100)
    async with get_session_factory()() as restarted_db:
        restarted = await restarted_db.get(TradingAccount, account.id)
        assert equity_baseline(restarted, "USDC") == (100, 100)
        assert collateral_available(restarted, "USDC") == 0


async def test_legacy_ambiguous_currency_quarantines_entries_but_allows_owned_exit(trading):
    account = trading.account
    account.risk_equity_json = None
    account.hwm_equity = account.day_start_equity = account.usdt_equity = 6000
    observe_equity(account, balance("USDC", 5000))
    observe_equity(account, balance("USDT", 1000))
    state = risk_state(account)
    assert state["legacy_unverified"] is True
    assert state["legacy"]["hwm_equity"] == 6000
    assert state["legacy"]["day_start_equity"] == 6000
    with pytest.raises(ValueError, match="Legacy collateral"):
        equity_baseline(account, "USDT")
    await trading.db.commit()
    async with get_session_factory()() as restarted_db:
        restarted = await restarted_db.get(TradingAccount, account.id)
        with pytest.raises(ValueError, match="Legacy collateral"):
            equity_baseline(restarted, "USDC")
        assert risk_state(restarted)["legacy"]["hwm_equity"] == 6000
    exchange = OfflineExchange()
    await execute_batch(trading, exchange, side="BUY", remaining=1)
    assert exchange.orders == []
    assert (await trading.db.scalar(select(ExecutionRecord))).status == "BLOCKED_COLLATERAL_BALANCE"
    allocation = await trading.db.scalar(select(PositionAllocation).where(
        PositionAllocation.binding_id == trading.binding.id,
        PositionAllocation.inst_id == "BTCUSDT", PositionAllocation.pos_side == "long"))
    assert allocation is not None and allocation.owned_qty == 0
    allocation.owned_qty, allocation.owned_qty_exact = 1, "1"
    allocation.entry_cost, allocation.entry_cost_exact = 100, "100"
    allocation.opened_at_ms, allocation.generation = 100, 1
    other = PositionAllocation(binding_id=trading.other.id, account_id=account.id, inst_id="BTCUSDT",
                               pos_side="long", owned_qty=9, owned_qty_exact="9",
                               entry_cost=900, entry_cost_exact="900")
    trading.db.add(other)
    await trading.db.commit()
    await execute_batch(trading, exchange, side="SELL", remaining=0, event="legacy-safe-exit")
    assert len(exchange.orders) == 1 and exchange.orders[0]["reduce_only"] is True
    assert allocation.owned_qty_exact == "0" and other.owned_qty_exact == "9"
    assert risk_state(account)["legacy_unverified"] is True


def test_sqlite_migration_marks_existing_risk_as_unverified_without_erasing_it():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE trading_accounts (id INTEGER PRIMARY KEY, hwm_equity FLOAT)"))
        connection.execute(text("INSERT INTO trading_accounts VALUES (1, 6000)"))
        _apply_sqlite_migrations(connection)
        _apply_sqlite_migrations(connection)
        row = connection.execute(text("SELECT hwm_equity, risk_equity_json FROM trading_accounts")).one()
        assert tuple(row) == (6000, None)
    engine.dispose()


async def test_connection_preserves_legacy_quarantine_and_correct_usdt_display(persisted_account, monkeypatch):
    db, account = persisted_account
    account.risk_equity_json = None
    account.hwm_equity = account.day_start_equity = account.usdt_equity = 6000
    await db.commit()
    adapter = SimpleNamespace(test_connection=AsyncMock(return_value={"ok": True,
        "totalEquity": 6000, "usdtEquity": 1000, "usdcEquity": 5000, "availEquity": 0}))
    monkeypatch.setattr("app.api.accounts.create_exchange_adapter", lambda **kwargs: adapter)
    result = await connectivity(account.id, db=db, _user=None)
    assert result["totalEquity"] == 1000
    assert account.usdt_equity == 1000 and account.avail_equity == 0
    assert account.hwm_equity == 6000 and account.day_start_equity == 6000
    assert risk_state(account)["legacy_unverified"] is True
    assert risk_state(account)["legacy"]["usdt_equity"] == 6000
    await db.refresh(account)
    assert _account_out(account)["totalEquity"] == 1000
    assert _account_out(account)["usdcEquity"] == 5000
    with pytest.raises(ValueError, match="Legacy collateral"):
        equity_baseline(account, "USDT")


async def test_usdc_observation_never_supplies_usdt_entry_margin(trading):
    observe_equity(trading.account, balance("USDT", 1000, 0))
    observe_equity(trading.account, balance("USDC", 5000, 5000))
    exchange = OfflineExchange()
    await execute_batch(trading, exchange, side="BUY", remaining=1)
    assert exchange.orders == []
    assert (await trading.db.scalar(select(ExecutionRecord))).status == "BLOCKED_INSUFFICIENT_MARGIN"
    assert trading.account.avail_equity == 0


@pytest.mark.parametrize("equity,available,expected", [(80, 80, "BLOCKED_DAILY_LOSS"),
                                                        (100, 0, "BLOCKED_INSUFFICIENT_MARGIN")])
async def test_usdc_entry_uses_only_usdc_loss_baselines_and_available_margin(trading, equity, available, expected):
    observe_equity(trading.account, balance("USDT", 1000, 1000))
    observe_equity(trading.account, balance("USDC", 100, 100))
    observe_equity(trading.account, balance("USDC", equity, available))
    spec = replace(trading.spec, inst_id="BTCUSDC", settle_ccy="USDC", quote_ccy="USDC", base_ccy="BTC")
    exchange = OfflineExchange()
    await trading.router._execute_batch_for_binding(
        trading.db, trading.binding, trading.lead, trading.account, exchange,
        {"symbol": "BTCUSDC", "side": "BUY", "positionSide": "LONG", "qty": "1",
         "avgPrice": "100", "notional": "100", "_event_id": "usdc-entry",
         "startTime": NOW_MS - 150_000, "endTime": NOW_MS - 150_000},
        lead_margin_balance=1000, active_lead_positions={"BTCUSDC_LONG": 1}, my_equity=equity,
        positions=[], instruments={spec.inst_id: spec}, global_kill=False, notify_cfg={})
    assert exchange.orders == []
    record = await trading.db.scalar(select(ExecutionRecord))
    assert record.status == expected
    if equity == 80:
        assert "20.00%" in record.details
    assert trading.account.usdt_equity == 1000
    assert trading.account.avail_equity == 1000


async def test_missing_previously_observed_usdc_fails_closed_in_breaker(trading):
    observe_equity(trading.account, balance("USDT", 1000))
    observe_equity(trading.account, balance("USDC", 100))
    await trading.db.commit()
    adapter = SimpleNamespace(get_usdt_balance=AsyncMock(return_value=balance("USDT", 1000)))
    assert not await EngineOrchestrator()._check_account_equity(trading.db, trading.binding, trading.account, adapter)
    assert trading.binding.circuit_breaker_tripped
    assert trading.account.status == "error"
    assert "missing" in trading.account.last_error


def test_zero_legacy_baselines_can_initialize_without_guessing_currency():
    account = SimpleNamespace(risk_equity_json=None, usdt_equity=0, avail_equity=0,
                              hwm_equity=0, day_start_equity=0, day_start_date=None)
    observe_equity(account, balance("USDC", 100))
    observe_equity(account, balance("USDT", 1000))
    assert equity_baseline(account, "USDT") == (1000, 1000)
    assert equity_baseline(account, "USDC") == (100, 100)
    assert risk_state(account)["legacy_unverified"] is False
