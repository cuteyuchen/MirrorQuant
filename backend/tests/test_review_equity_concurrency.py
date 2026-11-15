import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.accounts import test_account_connectivity as connectivity
from app.core.database import Base
from app.core.security import CredentialVault
from app.models.domain import CopyBinding, LeadSource, TradingAccount
from app.services.adapters.base import BalanceInfo
from app.services.engine.equity import observe_equity, risk_state
from app.services.engine.orchestrator import EngineOrchestrator


@pytest.mark.parametrize("operation", ["connectivity", "sync", "risk_check"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_interleaved_equity_writers_preserve_committed_baselines_and_restart(monkeypatch, tmp_path, operation, legacy):
    path = tmp_path / "review-equity.sqlite"
    url = f"sqlite+aiosqlite:///{path}"
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        account = TradingAccount(name="fake", exchange="binance", api_key_masked="fake",
            encrypted_credentials=CredentialVault().encrypt_dict({"api_key": "FAKE", "secret_key": "FAKE"}),
            usdt_equity=6000 if legacy else 0, hwm_equity=6000 if legacy else 0)
        observe_equity(account, BalanceInfo(100, 100, 100, 0, "USDT"))
        db.add(account)
        lead = LeadSource(name="fake", portfolio_id="fake")
        db.add(lead)
        await db.flush()
        binding = CopyBinding(name="fake", account_id=account.id, lead_id=lead.id)
        db.add(binding)
        await db.commit()
        account_id, binding_id = account.id, binding.id
        quarantine = risk_state(account).get("legacy")
    entered, release = asyncio.Event(), asyncio.Event()

    async def observation():
        entered.set()
        await release.wait()
        if operation == "connectivity":
            return {"ok": True, "usdtEquity": 90, "availEquity": 80, "usdcEquity": 9}
        return BalanceInfo(90, 80, 90, 0, "USDT", asset_balances={
            "USDT": {"equity": 90, "available": 80, "wallet": 90, "upl": 0},
            "USDC": {"equity": 9, "available": 9, "wallet": 9, "upl": 0}})

    adapter = SimpleNamespace(test_connection=AsyncMock(side_effect=observation),
        get_usdt_balance=AsyncMock(side_effect=observation), get_positions=AsyncMock(return_value=[]))
    monkeypatch.setattr("app.api.accounts.create_exchange_adapter", lambda **kwargs: adapter)
    monkeypatch.setattr("app.services.engine.orchestrator.create_exchange_adapter", lambda **kwargs: adapter)
    monkeypatch.setattr("app.api.accounts.ProxyManager.select_proxy_url", lambda *args, **kwargs: None)
    async with sessions() as stale:
        account = await stale.get(TradingAccount, account_id)
        binding = await stale.get(CopyBinding, binding_id)
        orchestrator = EngineOrchestrator()
        orchestrator.ws_hub.broadcast = AsyncMock()
        if operation == "connectivity":
            pending = asyncio.create_task(connectivity(account_id, stale, None))
        elif operation == "sync":
            pending = asyncio.create_task(orchestrator.sync_account(stale, account, proxies=[],
                resilience_cfg={"clock_sync_interval_sec": 0}))
        else:
            pending = asyncio.create_task(orchestrator._check_account_equity(stale, binding, account, adapter))
        await asyncio.wait_for(entered.wait(), 3)
        async with sessions() as concurrent:
            latest = await concurrent.get(TradingAccount, account_id)
            observe_equity(latest, BalanceInfo(200, 150, 200, 0, "USDT"))
            observe_equity(latest, BalanceInfo(50, 40, 50, 0, "USDC"))
            observe_equity(latest, BalanceInfo(7, 6, 7, 0, "EUR"))
            await concurrent.commit()
        release.set()
        await asyncio.wait_for(pending, 5)
    await engine.dispose()
    restarted = create_async_engine(url)
    try:
        async with async_sessionmaker(restarted)() as db:
            account = await db.get(TradingAccount, account_id)
            state = risk_state(account)
            assert set(state["assets"]) == {"USDT", "USDC", "EUR"}
            assert state["assets"]["USDT"]["hwm"] == 200
            assert state["assets"]["USDT"]["day_start"] == 100
            assert state["assets"]["USDC"]["hwm"] == 50
            assert state["assets"]["USDC"]["day_start"] == 50
            assert state["assets"]["EUR"]["hwm"] == 7
            assert state["legacy_unverified"] is legacy
            assert state["legacy"] == quarantine
            if legacy:
                from app.services.engine.equity import equity_baseline
                with pytest.raises(ValueError, match="Legacy collateral"):
                    equity_baseline(account, "USDT")
    finally:
        await restarted.dispose()
