"""Offline regressions for Binance USDT + USDC equity and its consumers."""

import json
from unittest.mock import AsyncMock, Mock

import pytest

from app.api.accounts import _account_out, test_account_connectivity as account_connectivity
from app.core.database import get_session_factory
from app.core.security import CredentialVault
from app.models.domain import ProxyNode, TradingAccount
from app.services.adapters.base import BalanceInfo
from app.services.adapters.binance import BinanceAdapter
from app.services.engine.orchestrator import EngineOrchestrator


def asset(name, equity, available=None, wallet=None, upl=0):
    return {
        "asset": name,
        "marginBalance": str(equity),
        "availableBalance": str(equity if available is None else available),
        "walletBalance": str(equity if wallet is None else wallet),
        "unrealizedProfit": str(upl),
    }


def single_asset_account():
    # Binance's single-asset top-level totals report only USDT.
    return {
        "multiAssetsMargin": False,
        "totalMarginBalance": "4602.64",
        "availableBalance": "4602.64",
        "totalWalletBalance": "4602.64",
        "totalUnrealizedProfit": "0",
        "assets": [asset("USDT", 4602.64), asset("USDC", 5000)],
    }


@pytest.fixture
def adapter():
    return BinanceAdapter(credentials={"api_key": "synthetic", "secret_key": "synthetic"})


@pytest.fixture
async def persisted_account():
    async with get_session_factory()() as db:
        proxy = ProxyNode(name="synthetic", url="http://127.0.0.1:8080", is_healthy=True)
        db.add(proxy)
        await db.flush()
        account = TradingAccount(
            name="multi-collateral", exchange="binance", proxy_id=proxy.id,
            api_key_masked="synthetic", encrypted_credentials=CredentialVault().encrypt_dict({}),
        )
        db.add(account)
        await db.commit()
        yield db, account


def test_balance_info_legacy_constructor_and_independent_defaults():
    balance = BalanceInfo(eq=100, avail_eq=80, cash_bal=90, upl=10)
    other = BalanceInfo(100, 80, 90, 10, "USDT", {"legacy": True})
    assert (balance.usdt_eq, balance.usdc_eq, balance.asset_balances) == (0.0, 0.0, {})
    balance.asset_balances["USDT"] = {"equity": 100}
    assert other.asset_balances == {}
    assert other.raw == {"legacy": True}


async def test_single_asset_usdt_only_preserves_losses_and_zero_available(adapter):
    data = {"multiAssetsMargin": False, "assets": [asset("USDT", 80, available=0, wallet=100, upl=-20)]}
    adapter._request = AsyncMock(return_value=data)

    balance = await adapter.get_usdt_balance()

    assert (balance.eq, balance.avail_eq, balance.cash_bal, balance.upl) == (80, 0, 100, -20)
    assert (balance.usdt_eq, balance.usdc_eq) == (80, 0)
    assert balance.asset_balances == {"USDT": {"equity": 80, "available": 0, "wallet": 100, "upl": -20}}
    assert balance.raw is data
    adapter._request.assert_awaited_once_with("GET", "/fapi/v2/account")


@pytest.mark.parametrize("environment", ["demo", "live"])
async def test_single_asset_combines_usdt_and_usdc(environment):
    adapter = BinanceAdapter({}, environment=environment)
    adapter._request = AsyncMock(return_value=single_asset_account())

    balance = await adapter.get_usdt_balance()

    assert (balance.eq, balance.avail_eq, balance.cash_bal, balance.upl) == (9602.64, 9602.64, 9602.64, 0)
    assert (balance.usdt_eq, balance.usdc_eq, balance.currency) == (4602.64, 5000, "USDT")
    assert balance.asset_balances == {
        "USDT": {"equity": 4602.64, "available": 4602.64, "wallet": 4602.64, "upl": 0},
        "USDC": {"equity": 5000, "available": 5000, "wallet": 5000, "upl": 0},
    }


async def test_single_asset_combines_each_metric_separately(adapter):
    adapter._request = AsyncMock(return_value={"assets": [
        asset("usdt", 4602.64, available=4000, wallet=4702.64, upl=-100),
        asset("usdc", 5000, available=4500, wallet=4950, upl=50),
    ]})

    balance = await adapter.get_usdt_balance()

    assert (balance.eq, balance.avail_eq, balance.cash_bal, balance.upl) == (9602.64, 8500, 9652.64, -50)
    assert (balance.usdt_eq, balance.usdc_eq) == (4602.64, 5000)
    assert set(balance.asset_balances) == {"USDT", "USDC"}


async def test_multi_assets_uses_account_totals_without_double_counting(adapter):
    data = single_asset_account()
    data.update(multiAssetsMargin=True, totalMarginBalance="10002.64", availableBalance="8000",
                totalWalletBalance="9902.64", totalUnrealizedProfit="100")
    data["assets"].append(asset("BTC", 0.01))
    adapter._request = AsyncMock(return_value=data)

    balance = await adapter.get_usdt_balance()

    assert (balance.eq, balance.avail_eq, balance.cash_bal, balance.upl) == (10002.64, 8000, 9902.64, 100)
    assert (balance.usdt_eq, balance.usdc_eq) == (4602.64, 5000)
    assert balance.asset_balances["BTC"]["equity"] == 0.01


@pytest.mark.parametrize("totals", [
    {}, {"totalMarginBalance": "0"}, {"totalMarginBalance": 0},
    {"totalMarginBalance": None}, {"totalMarginBalance": ""}, {"totalMarginBalance": "-1"},
])
async def test_multi_assets_empty_or_nonpositive_totals_fall_back_to_stablecoins(adapter, totals):
    data = single_asset_account()
    data.pop("totalMarginBalance")
    data.update(multiAssetsMargin=True, availableBalance="999999", totalWalletBalance="999999", **totals)
    adapter._request = AsyncMock(return_value=data)

    balance = await adapter.get_usdt_balance()

    assert (balance.eq, balance.avail_eq, balance.cash_bal, balance.upl) == (9602.64, 9602.64, 9602.64, 0)
    assert (balance.usdt_eq, balance.usdc_eq) == (4602.64, 5000)


async def test_multi_assets_missing_other_totals_remain_zero(adapter):
    adapter._request = AsyncMock(return_value={
        "multiAssetsMargin": True, "totalMarginBalance": "9602.64",
        "assets": single_asset_account()["assets"],
    })

    balance = await adapter.get_usdt_balance()

    assert (balance.eq, balance.avail_eq, balance.cash_bal, balance.upl) == (9602.64, 0, 0, 0)


async def test_asset_breakdown_includes_wallet_only_assets_without_adding_nonstable_equity(adapter):
    adapter._request = AsyncMock(return_value={"assets": [
        asset("USDC", 5000), asset("btc", 0, wallet=0.01),
        asset("ETH", -1, wallet=2), asset("BNB", 0), asset("SOL", -2, wallet=-1),
    ]})

    balance = await adapter.get_usdt_balance()

    assert (balance.eq, balance.usdt_eq, balance.usdc_eq) == (5000, 0, 5000)
    assert set(balance.asset_balances) == {"USDC", "BTC", "ETH"}
    assert balance.asset_balances["BTC"]["wallet"] == 0.01
    assert balance.asset_balances["ETH"]["equity"] == -1


async def test_missing_and_null_metrics_default_to_zero(adapter):
    adapter._request = AsyncMock(return_value={"assets": [
        {"asset": "USDT", "marginBalance": None, "walletBalance": "100", "unrealizedProfit": "-100"},
        {"asset": "USDC", "marginBalance": "", "availableBalance": None, "walletBalance": None},
    ]})

    balance = await adapter.get_usdt_balance()

    assert (balance.eq, balance.avail_eq, balance.cash_bal, balance.upl) == (0, 0, 100, -100)
    assert balance.asset_balances == {"USDT": {"equity": 0, "available": 0, "wallet": 100, "upl": -100}}


@pytest.mark.parametrize("data", [{}, {"assets": None}, {"assets": [asset("USDT", 0), asset("USDC", 0)]}])
async def test_empty_account_returns_zero_balance_with_raw_response(adapter, data):
    adapter._request = AsyncMock(return_value=data)

    balance = await adapter.get_usdt_balance()

    assert balance == BalanceInfo(eq=0, avail_eq=0, cash_bal=0, upl=0, raw=data)


@pytest.mark.parametrize("dual,mode", [(True, "hedge"), (False, "one_way")])
async def test_connection_reports_combined_equity_and_breakdown(adapter, dual, mode):
    data = single_asset_account()
    data["canTrade"] = False
    adapter._request = AsyncMock(side_effect=[data, {"dualSidePosition": dual}, data])
    adapter.get_positions = AsyncMock(return_value=[Mock()])

    result = await adapter.test_connection()

    assert result == {
        "ok": True, "exchange": "binance", "environment": "demo", "posMode": mode,
        "canTrade": False, "totalEquity": 9602.64, "usdtEquity": 4602.64, "usdcEquity": 5000,
        "availEquity": 9602.64, "openPositions": 1,
        "assetBalances": {
            "USDT": {"equity": 4602.64, "available": 4602.64, "wallet": 4602.64, "upl": 0},
            "USDC": {"equity": 5000, "available": 5000, "wallet": 5000, "upl": 0},
        },
    }


@pytest.mark.parametrize("total", [None, 0, 9602.64])
async def test_connectivity_persists_total_equity_with_legacy_fallback(persisted_account, monkeypatch, total):
    db, account = persisted_account
    result = {"ok": True, "usdtEquity": 4602.64, "availEquity": 4000}
    if total is not None:
        result["totalEquity"] = total
    adapter = Mock(test_connection=AsyncMock(return_value=result))
    monkeypatch.setattr("app.api.accounts.create_exchange_adapter", Mock(return_value=adapter))

    assert await account_connectivity(account.id, db=db, _user=None) == result
    await db.refresh(account)

    expected = total or 4602.64
    assert (account.usdt_equity, account.avail_equity, account.hwm_equity) == (expected, 4000, expected)
    assert account.status == "connected"
    assert _account_out(account)["totalEquity"] == round(expected, 2)


async def test_connectivity_http_endpoint_and_account_listing(async_client, persisted_account, adapter, monkeypatch):
    db, account = persisted_account
    data = single_asset_account()
    adapter._request = AsyncMock(side_effect=[data, {"dualSidePosition": True}, data])
    adapter.get_positions = AsyncMock(return_value=[])
    monkeypatch.setattr("app.api.accounts.create_exchange_adapter", Mock(return_value=adapter))
    setup = await async_client.post("/api/auth/setup", json={
        "username": "balance-admin", "password": "SecurePassword2026!",
    })
    assert setup.status_code == 201
    headers = {"Authorization": f"Bearer {setup.json()['token']}"}

    response = await async_client.post(f"/api/accounts/{account.id}/test", headers=headers)

    assert response.status_code == 200
    result = response.json()
    assert result["ok"] is True
    assert (result["totalEquity"], result["usdtEquity"], result["usdcEquity"]) == (9602.64, 4602.64, 5000)
    assert result["assetBalances"]["USDC"]["equity"] == 5000
    await db.refresh(account)
    assert account.usdt_equity == 9602.64
    listing = await async_client.get("/api/accounts", headers=headers)
    assert listing.status_code == 200
    assert listing.json()["items"][0]["totalEquity"] == 9602.64


async def test_sync_account_persists_and_broadcasts_combined_equity(persisted_account, adapter, monkeypatch):
    db, account = persisted_account
    adapter._request = AsyncMock(return_value=single_asset_account())
    adapter.sync_time = AsyncMock(return_value=12)
    position = Mock(notional_usd=1200, upl=-25)
    position.to_dict.return_value = {"instId": "BTCUSDT", "pos": 0.02, "upl": -25}
    adapter.get_positions = AsyncMock(return_value=[position])
    monkeypatch.setattr("app.services.engine.orchestrator.create_exchange_adapter", Mock(return_value=adapter))
    engine = EngineOrchestrator()
    engine.ws_hub.broadcast = AsyncMock()

    result = await engine.sync_account(db, account)
    await db.refresh(account)

    assert result["ok"] is True
    assert (result["totalEquity"], result["usdtEquity"], result["usdcEquity"]) == (9602.64, 4602.64, 5000)
    assert result["assetBalances"]["USDC"]["equity"] == 5000
    assert result["positions"] == [position.to_dict.return_value]
    assert (account.usdt_equity, account.avail_equity, account.hwm_equity, account.day_start_equity) == (
        9602.64, 9602.64, 9602.64, 9602.64,
    )
    assert (account.total_notional, account.unrealized_pnl, account.clock_offset_ms) == (1200, -25, 12)
    assert json.loads(account.positions_json) == result["positions"]
    assert account.status == "connected" and account.last_sync_at is not None
    engine.ws_hub.broadcast.assert_awaited_once_with({
        "type": "account_update", "accountId": account.id, "name": account.name,
        "totalEquity": 9602.64, "usdtEquity": 4602.64, "usdcEquity": 5000,
        "unrealizedPnl": -25, "positions": result["positions"],
    })
