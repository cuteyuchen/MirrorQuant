"""Offline account-sync regressions with mocked exchange, persistence and broadcasts."""

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.services.adapters.base import BalanceInfo
from app.services.adapters.binance import BinanceAdapter
from app.services.engine.orchestrator import EngineOrchestrator


NOW_MS = 1_700_000_000_000
ACCOUNT = {
    "multiAssetsMargin": False, "updateTime": NOW_MS,
    "totalMarginBalance": "6000", "availableBalance": "5734.45",
    "totalWalletBalance": "6000", "totalUnrealizedProfit": "0",
    "assets": [
        {"asset": "USDT", "marginBalance": "1000", "availableBalance": "734.45",
         "walletBalance": "1000", "unrealizedProfit": "0"},
        {"asset": "USDC", "marginBalance": "5000", "availableBalance": "5000",
         "walletBalance": "5000", "unrealizedProfit": "0"},
    ],
}
RISK_FIELDS = ("hwm_equity", "day_start_equity", "usdt_equity", "avail_equity")


@pytest.fixture(autouse=True)
def setup_test_db():
    yield


@pytest.fixture
def sync_state(monkeypatch):
    monkeypatch.setattr("app.services.adapters.binance.time.time", lambda: NOW_MS / 1000)
    account = SimpleNamespace(
        id=1, name="offline", exchange="binance", encrypted_credentials="unused",
        proxy_id=None, environment="demo", margin_mode="cross", position_mode="hedge",
        clock_offset_ms=0, hwm_equity=900, day_start_equity=800, day_start_date="2000-01-01",
        usdt_equity=850, avail_equity=700, total_notional=200, unrealized_pnl=-25,
        positions_json='[{"instId":"BTCUSDT","pos":0.02}]', last_sync_at=None,
        last_error=None, status="connected",
    )
    persisted = []
    db = AsyncMock()
    db.commit.side_effect = lambda: persisted.append(vars(account).copy())
    adapter = BinanceAdapter({})
    adapter._request = AsyncMock(side_effect=[{"multiAssetsMargin": False}, copy.deepcopy(ACCOUNT)])
    adapter.get_usdt_balance = AsyncMock(return_value=BalanceInfo(6000, 5734.45, 6000, 0))
    adapter.get_positions = AsyncMock(return_value=[])
    adapter.place_order = AsyncMock()
    monkeypatch.setattr("app.services.engine.orchestrator.create_exchange_adapter", Mock(return_value=adapter))
    monkeypatch.setattr("app.services.engine.orchestrator.ProxyManager.select_proxy_url", Mock(return_value=None))
    notify = Mock()
    monkeypatch.setattr("app.services.engine.orchestrator.enqueue_notification", notify)
    engine = EngineOrchestrator.__new__(EngineOrchestrator)
    engine.vault = SimpleNamespace(decrypt_dict=Mock(return_value={}))
    engine.ws_hub = SimpleNamespace(broadcast=AsyncMock())
    engine._last_clock_sync = {}
    return SimpleNamespace(
        engine=engine, db=db, account=account, adapter=adapter, persisted=persisted, notify=notify,
    )


@pytest.mark.parametrize("available", ["0", "734.45"])
@pytest.mark.asyncio
async def test_sync_account_persists_only_usdt_collateral(sync_state, available):
    state = sync_state
    data = copy.deepcopy(ACCOUNT)
    data["assets"][0]["availableBalance"] = available
    state.adapter._request.side_effect = [{"multiAssetsMargin": False}, data]

    result = await state.engine.sync_account(
        state.db, state.account, proxies=[], resilience_cfg={"clock_sync_interval_sec": 0},
    )

    assert result["ok"] is True
    expected = (1000, 1000, 1000, float(available))
    assert tuple(getattr(state.account, field) for field in RISK_FIELDS) == expected
    assert len(state.persisted) == 1
    assert tuple(state.persisted[0][field] for field in RISK_FIELDS) == expected
    assert (result["totalEquity"], result["usdtEquity"], result["usdcEquity"]) == (1000, 1000, 5000)
    assert result["assetBalances"]["USDC"]["equity"] == 5000
    assert state.account.status == "connected"
    state.engine.ws_hub.broadcast.assert_awaited_once_with({
        "type": "account_update", "accountId": state.account.id, "name": state.account.name,
        "totalEquity": 1000, "usdtEquity": 1000, "usdcEquity": 5000,
        "unrealizedPnl": 0, "positions": [],
    })
    state.adapter.get_usdt_balance.assert_not_awaited()
    state.adapter.place_order.assert_not_awaited()
    state.notify.assert_not_called()


@pytest.mark.parametrize("issue", [
    "mode_missing", "mode_string", "multi_assets", "account_mode_missing", "contradiction",
    "timestamp_missing", "timestamp_stale", "timestamp_future", "asset_timestamp_stale",
    "asset_missing", "asset_duplicate", "available_missing", "equity_nan", "available_negative",
])
@pytest.mark.asyncio
async def test_sync_account_invalid_collateral_preserves_persisted_risk_fields(sync_state, issue):
    state = sync_state
    data = copy.deepcopy(ACCOUNT)
    mode = {"multiAssetsMargin": False}
    if issue == "mode_missing":
        mode = {}
    elif issue == "mode_string":
        mode["multiAssetsMargin"] = "false"
    elif issue == "multi_assets":
        mode["multiAssetsMargin"] = True
    elif issue == "account_mode_missing":
        del data["multiAssetsMargin"]
    elif issue == "contradiction":
        data["multiAssetsMargin"] = True
    elif issue == "timestamp_missing":
        del data["updateTime"]
    elif issue == "timestamp_stale":
        data["updateTime"] -= 30_001
    elif issue == "timestamp_future":
        data["updateTime"] += 1
    elif issue == "asset_timestamp_stale":
        data["assets"][0]["updateTime"] = NOW_MS - 30_001
    elif issue == "asset_missing":
        data["assets"] = [data["assets"][1]]
    elif issue == "asset_duplicate":
        data["assets"].append(dict(data["assets"][0]))
    elif issue == "available_missing":
        del data["assets"][0]["availableBalance"]
    elif issue == "equity_nan":
        data["assets"][0]["marginBalance"] = "NaN"
    elif issue == "available_negative":
        data["assets"][0]["availableBalance"] = "-1"
    state.adapter._request.side_effect = [mode, data]
    previous = vars(state.account).copy()

    result = await state.engine.sync_account(
        state.db, state.account, proxies=[], resilience_cfg={"clock_sync_interval_sec": 0},
    )

    assert result["ok"] is False and result["error"]
    assert state.account.status == "error" and state.account.last_error == result["error"]
    assert len(state.persisted) == 1
    for field, value in previous.items():
        if field not in ("status", "last_error"):
            assert getattr(state.account, field) == value
            assert state.persisted[0][field] == value
    state.adapter.get_usdt_balance.assert_not_awaited()
    state.adapter.get_positions.assert_not_awaited()
    state.adapter.place_order.assert_not_awaited()
    state.engine.ws_hub.broadcast.assert_not_awaited()
    state.notify.assert_not_called()
