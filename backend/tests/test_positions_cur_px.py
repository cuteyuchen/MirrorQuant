"""Current-price serialization, exchange parsing, and legacy API snapshot regressions."""
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.core.database import get_session_factory
from app.models.domain import CopyBinding, LeadSource, TradingAccount
from app.services.adapters.base import ExchangePosition
from app.services.adapters.binance import BinanceAdapter
from app.services.adapters.bitget import BitgetAdapter
from app.services.adapters.bybit import BybitAdapter
from app.services.adapters.okx import OKXAdapter
from app.services.positions import with_current_price


PRICE_KEYS = ("curPx", "markPx", "currentPrice", "markPrice")


def test_position_current_price_is_optional_and_serializes_all_aliases():
    position = ExchangePosition("BTCUSDT", "BTCUSDT", "long", "cross", 2, 2, 100, 10, .05, 210, 5)
    assert position.cur_px == 0.0
    assert all(position.to_dict()[key] == 0.0 for key in PRICE_KEYS)
    position.cur_px = 105.125
    assert all(position.to_dict()[key] == 105.125 for key in PRICE_KEYS)
    assert position.to_dict()["avgPx"] == 100


CASES = [
    (OKXAdapter, {"instId": "BTC-USDT-SWAP", "pos": "2", "avgPx": "100", "markPx": "105.125", "last": "106", "upl": "10.25"}, 105.125),
    (OKXAdapter, {"instId": "BTC-USDT-SWAP", "pos": "2", "avgPx": "100", "last": "106"}, 106),
    (OKXAdapter, {"instId": "BTC-USDT-SWAP", "pos": "2", "avgPx": "100", "idxPx": "107"}, 107),
    (BinanceAdapter, {"symbol": "BTCUSDT", "positionAmt": "-2", "entryPrice": "100", "markPrice": "105.125", "unRealizedProfit": "-10.25"}, 105.125),
    (BybitAdapter, {"symbol": "BTCUSDT", "size": "2", "side": "Buy", "avgPrice": "100", "markPrice": "105.125", "lastPrice": "106", "unrealisedPnl": "10.25"}, 105.125),
    (BybitAdapter, {"symbol": "BTCUSDT", "size": "2", "side": "Buy", "avgPrice": "100", "lastPrice": "106"}, 106),
    (BitgetAdapter, {"symbol": "BTCUSDT", "total": "2", "holdSide": "long", "openPriceAvg": "100", "markPrice": "105.125", "unrealizedPL": "10.25"}, 105.125),
]


@pytest.mark.parametrize("kind,row,expected", CASES)
async def test_exchange_positions_include_current_price(kind, row, expected):
    venue = kind({"api_key": "test", "secret_key": "test", "passphrase": "test"})
    response = {"list": [row]} if kind is BybitAdapter else [row]
    venue._request = AsyncMock(return_value=response)
    positions = await venue.get_positions()
    assert len(positions) == 1
    assert positions[0].cur_px == expected
    assert positions[0].avg_px == 100
    assert all(positions[0].to_dict()[key] == expected for key in PRICE_KEYS)
    if kind in (BinanceAdapter, BybitAdapter, BitgetAdapter):
        assert positions[0].notional_usd == pytest.approx(2 * expected)


@pytest.mark.parametrize("kind,row", [
    (OKXAdapter, {"instId": "BTC-USDT-SWAP", "pos": "2", "avgPx": "100"}),
    (BinanceAdapter, {"symbol": "BTCUSDT", "positionAmt": "2", "entryPrice": "100"}),
    (BybitAdapter, {"symbol": "BTCUSDT", "size": "2", "side": "Buy", "avgPrice": "100"}),
    (BitgetAdapter, {"symbol": "BTCUSDT", "total": "2", "holdSide": "long", "openPriceAvg": "100"}),
])
async def test_exchange_price_falls_back_to_entry_when_missing(kind, row):
    venue = kind({"api_key": "test", "secret_key": "test", "passphrase": "test"})
    venue._request = AsyncMock(return_value={"list": [row]} if kind is BybitAdapter else [row])
    assert (await venue.get_positions())[0].cur_px == 100


@pytest.mark.parametrize("snapshot,exchange,expected,source", [
    ({"curPx": 105, "currentPrice": 106}, "binance", 105, "exchange"),
    ({"curPx": "0", "currentPrice": "105"}, "binance", 105, "exchange"),
    ({"markPx": 105}, "binance", 105, "exchange"),
    ({"markPrice": 105}, "binance", 105, "exchange"),
    ({"pos": "-2", "notionalUsd": "-210", "avgPx": 100}, "binance", 105, "estimated"),
    ({"pos": 0, "notionalUsd": 210, "avgPx": 100}, "bybit", 100, "entry"),
    ({"curPx": "NaN", "markPx": "Infinity", "avgPx": 100}, "bitget", 100, "entry"),
    ({"pos": 2, "notionalUsd": 210, "avgPx": 100}, "okx", 100, "entry"),
    ({"pos": 2, "notionalUsd": 210, "ctVal": .01, "avgPx": 100}, "okx", 10500, "estimated"),
    ({}, "binance", 0, "unavailable"),
])
def test_legacy_snapshot_price_fallback(snapshot, exchange, expected, source):
    original = dict(snapshot)
    result = with_current_price(snapshot, exchange)
    assert snapshot == original
    assert all(result[key] == expected for key in PRICE_KEYS)
    assert result["priceSource"] == source


async def test_overview_and_accounts_expose_current_price_and_lead_health(async_client):
    setup = await async_client.post("/api/auth/setup", json={"username": "admin", "password": "SecurePassword2026!"})
    assert setup.status_code == 201
    headers = {"Authorization": f"Bearer {setup.json()['token']}"}
    now = datetime.now(timezone.utc)
    positions = [
        {"instId": "SOLUSDT", "symbol": "SOLUSDT", "posSide": "long", "pos": 2, "avgPx": 100, "notionalUsd": 210, "upl": 10, "uplRatio": .05},
        {"instId": "BTCUSDT", "pos": 1, "avgPx": 90, "curPx": 0, "currentPrice": 100},
    ]
    async with get_session_factory()() as db:
        lead = LeadSource(name="Alpha", portfolio_id="alpha", platform="binance_bapi", is_enabled=True,
                          status="running", last_sync_at=now, snapshot_json=json.dumps({
                              "meta": {"generatedAt": int(now.timestamp() * 1000)},
                              "openPositions": [{"symbol": "SOLUSDT", "positionSide": "LONG", "qty": 2}],
                          }))
        idle = LeadSource(name="Pending", portfolio_id="pending", is_enabled=False)
        corrupt = LeadSource(name="Corrupt", portfolio_id="corrupt", snapshot_json="{invalid", status="error",
                             last_error="failed https://user:password@example.com/private")
        account = TradingAccount(name="Live account", exchange="binance", environment="live",
                                 encrypted_credentials="test", api_key_masked="test", status="connected",
                                 last_sync_at=now, positions_json=json.dumps(positions))
        db.add_all([lead, idle, corrupt, account])
        await db.flush()
        db.add(CopyBinding(name="copy", lead_id=lead.id, account_id=account.id))
        await db.commit()
        lead_id, account_id = lead.id, account.id

    response = await async_client.get("/api/dashboard/overview", headers=headers)
    assert response.status_code == 200
    overview = response.json()
    follower_positions = overview["followerPositions"]
    assert len(follower_positions) == 2
    assert all(follower_positions[0][key] == 105 for key in PRICE_KEYS)
    assert all(follower_positions[1][key] == 100 for key in PRICE_KEYS)
    assert follower_positions[0]["upl"] == 10
    assert follower_positions[0]["accountId"] == account_id
    health = overview["leadsStatus"][0]
    assert {"id", "name", "sourceType", "isEnabled", "status", "lastSyncAt", "lastError", "openPositionsCount"} <= health.keys()
    assert health["id"] == lead_id
    assert health["openPositionsCount"] == 1
    assert health["positionsKnown"] is True and health["stale"] is False
    assert health["boundAccountIds"] == [account_id]
    assert overview["leadsStatus"][1]["openPositionsCount"] == 0
    assert overview["leadsStatus"][1]["positionsKnown"] is False
    assert overview["leadsStatus"][2]["positionsKnown"] is False
    assert "password" not in response.text
    assert overview["accountsStatus"][0]["status"] == "connected"
    assert overview["bindingsStatus"][0]["leadId"] == lead_id

    response = await async_client.get("/api/accounts", headers=headers)
    assert response.status_code == 200
    assert response.json()["items"][0]["positions"][0]["curPx"] == 105
    assert response.json()["items"][0]["positions"][1]["markPrice"] == 100
    # Reading legacy data must not rewrite its saved snapshot.
    async with get_session_factory()() as db:
        assert json.loads((await db.get(TradingAccount, account_id)).positions_json) == positions
