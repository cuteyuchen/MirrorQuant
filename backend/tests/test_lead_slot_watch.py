"""Copy-slot watch: notify once on a full -> open transition, never spam.

The check piggybacks on the detail fetch that already runs every poll
round (10 s for 熬鹰资本). Edge detection compares the persisted previous
snapshot profile against the freshly published one, so no extra state is
needed: while a slot stays open every round sees previous=open and stays
silent; once it refills, the next opening is a fresh edge again.
"""
import copy
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.database import get_session_factory
from app.models.domain import LeadSource, NotificationOutbox
from app.services.engine.orchestrator import EngineOrchestrator, copy_slot_opened
from app.services.lead_collector.binance_bapi import BinanceBapiCollector
from app.services.proxy.manager import ProxyManager

AOYING_PORTFOLIO_ID = "5075281354358777856"
COPY_LINK = (
    "https://www.binance.com/zh-CN/copy-trading/lead-details/"
    f"{AOYING_PORTFOLIO_ID}?timeRange=30D&isSmartFilter=true"
)


def profile(*, current, maximum, nickname="熬鹰资本", min_copy=300.0):
    return {
        "nickname": nickname, "avatarUrl": "https://example.invalid/avatar",
        "description": "test lead", "badgeName": "MASTER", "status": "ACTIVE",
        "isPaused": False, "positionShow": False, "tags": [], "signTradFi": False,
        "startTime": 100, "lastTradeTime": 200, "aumAmount": 12345.6, "copyAum": 789,
        "marginBalance": 1234.5, "copierPnl": 42, "currentCopyCount": current,
        "maxCopyCount": maximum, "fixedAmountMinCopyUsd": min_copy,
        "totalCopyCount": 77, "favoriteCount": 6, "mockCopyCount": 5,
        "profitSharingRate": 10, "sharpRatio": 1.2, "aiSummary": None,
    }


ORDER = {
    "orderId": "test-order", "orderTime": 1000, "orderUpdateTime": 1000,
    "symbol": "BTCUSDT", "side": "BUY", "positionSide": "LONG", "type": "MARKET",
    "executedQty": "2", "avgPrice": "100",
}


@pytest.fixture
def monitor(monkeypatch):
    engine = EngineOrchestrator()
    engine.collector.fetch_detail = AsyncMock(
        return_value=copy.deepcopy(profile(current=1000, maximum=1000)))
    engine.collector.fetch_order_history = AsyncMock(return_value=[ORDER])
    engine.collector.fetch_position_history = AsyncMock(return_value={"list": [], "total": 0})
    engine.collector.fetch_holdings = AsyncMock(return_value=[])
    engine.collector.fetch_performance = AsyncMock(return_value={
        "roi": 10, "pnl": 20, "winRate": 60, "mdd": 5, "winOrders": 3, "totalOrder": 5,
    })
    engine.collector.fetch_roi_chart = AsyncMock(return_value=[{"t": 1000, "v": 10}])
    engine.collector.fetch_allocation = AsyncMock(return_value={
        "timeRange": "90D", "items": [{"symbol": "BTCUSDT", "ratio": 100}],
    })
    engine.collector.fetch_marks = AsyncMock(return_value={
        "BTCUSDT": {"markPrice": 110, "lastPrice": 111, "currentPrice": 111, "priceTime": 1000},
    })
    engine.collector.fetch_brackets = AsyncMock(return_value={})
    engine.pa_analyzer.analyze_position = AsyncMock(return_value={"ok": True})
    engine.router.process_lead_events = AsyncMock()
    engine.ws_hub.broadcast = AsyncMock()
    monkeypatch.setattr(ProxyManager, "ordered_proxy_urls", lambda *args, **kwargs: [None])
    return engine


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=time.time())
    monkeypatch.setattr("app.services.engine.orchestrator.time.time", lambda: state.now)
    return state


async def make_lead(db, clock, *, slot_watch=True, baseline=(1000, 1000), **overrides):
    current, maximum = baseline
    values = dict(
        name="熬鹰资本", portfolio_id=AOYING_PORTFOLIO_ID, status="running", is_enabled=True,
        slot_watch_enabled=slot_watch,
        last_sync_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        snapshot_json=json.dumps({"meta": {"generatedAt": int(clock.now * 1000)},
                                  "profile": profile(current=current, maximum=maximum),
                                  "openPositionsMeta": {}}),
    )
    lead = LeadSource(**{**values, **overrides})
    db.add(lead)
    await db.commit()
    return lead


async def sync_once(db, engine, lead, clock):
    clock.now += 0.01  # Each collection is a distinct attempt, without real sleeps.
    result = await engine.sync_lead(db, lead, proxies=[])
    assert result["ok"] is True and not result.get("superseded")
    return result


async def notices(db):
    rows = list((await db.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id))).all())
    return [(row, json.loads(row.payload)) for row in rows]


def test_copy_slot_opened_edge_cases():
    full = {"currentCopyCount": 1000, "maxCopyCount": 1000}
    open_ = {"currentCopyCount": 999, "maxCopyCount": 1000}
    assert copy_slot_opened(full, open_) == (999, 1000)
    assert copy_slot_opened(open_, open_) is None
    assert copy_slot_opened(open_, full) is None
    assert copy_slot_opened(full, full) is None
    # Unknown counts never fire.
    assert copy_slot_opened({}, open_) is None
    assert copy_slot_opened(full, {"currentCopyCount": 999}) is None
    assert copy_slot_opened(full, {"currentCopyCount": None, "maxCopyCount": 1000}) is None
    # String counts from the wire are accepted; booleans are not counts.
    assert copy_slot_opened({"currentCopyCount": "1000", "maxCopyCount": "1000"}, open_) == (999, 1000)
    assert copy_slot_opened({"currentCopyCount": True, "maxCopyCount": 1000}, open_) is None
    # A raised cap is also an opening (1000/1000 -> 1000/1200).
    assert copy_slot_opened(full, {"currentCopyCount": 1000, "maxCopyCount": 1200}) == (1000, 1200)
    assert copy_slot_opened({"currentCopyCount": 0, "maxCopyCount": 0}, open_) is None


@pytest.mark.asyncio
async def test_full_to_open_notifies_once(monitor, clock):
    async with get_session_factory()() as db:
        lead = await make_lead(db, clock)
        monitor.collector.fetch_detail = AsyncMock(
            return_value=copy.deepcopy(profile(current=999, maximum=1000)))
        await sync_once(db, monitor, lead, clock)
        rows = await notices(db)
        assert len(rows) == 1
        _, payload = rows[0]
        assert payload["event_type"] == "LEAD_SLOT_OPEN"
        assert payload["lead_id"] == lead.id
        assert "熬鹰资本" in payload["message"]
        assert "999/1000" in payload["message"]
        assert "300" in payload["message"]
        assert COPY_LINK in payload["message"]


@pytest.mark.asyncio
async def test_open_slot_does_not_repeat(monitor, clock):
    async with get_session_factory()() as db:
        lead = await make_lead(db, clock)
        monitor.collector.fetch_detail = AsyncMock(
            return_value=copy.deepcopy(profile(current=999, maximum=1000)))
        await sync_once(db, monitor, lead, clock)
        await sync_once(db, monitor, lead, clock)
        await sync_once(db, monitor, lead, clock)
        assert len(await notices(db)) == 1


@pytest.mark.asyncio
async def test_refill_resets_and_reopen_notifies_again(monitor, clock):
    async with get_session_factory()() as db:
        lead = await make_lead(db, clock)
        monitor.collector.fetch_detail = AsyncMock(side_effect=[
            copy.deepcopy(profile(current=999, maximum=1000)),
            copy.deepcopy(profile(current=1000, maximum=1000)),
            copy.deepcopy(profile(current=998, maximum=1000)),
        ])
        await sync_once(db, monitor, lead, clock)
        await sync_once(db, monitor, lead, clock)
        assert len(await notices(db)) == 1  # refill itself is silent
        await sync_once(db, monitor, lead, clock)
        rows = await notices(db)
        assert len(rows) == 2
        assert "998/1000" in rows[1][1]["message"]


@pytest.mark.asyncio
async def test_slot_watch_off_never_notifies(monitor, clock):
    async with get_session_factory()() as db:
        lead = await make_lead(db, clock, slot_watch=False)
        monitor.collector.fetch_detail = AsyncMock(
            return_value=copy.deepcopy(profile(current=999, maximum=1000)))
        await sync_once(db, monitor, lead, clock)
        assert await notices(db) == []


@pytest.mark.asyncio
async def test_missing_counts_never_notifies(monitor, clock):
    async with get_session_factory()() as db:
        lead = await make_lead(db, clock)
        open_no_counts = profile(current=999, maximum=1000)
        open_no_counts.pop("currentCopyCount")
        open_no_counts.pop("maxCopyCount")
        monitor.collector.fetch_detail = AsyncMock(return_value=copy.deepcopy(open_no_counts))
        await sync_once(db, monitor, lead, clock)
        assert await notices(db) == []


@pytest.mark.asyncio
async def test_lead_own_notification_channel_is_used(monitor, clock):
    async with get_session_factory()() as db:
        lead = await make_lead(
            db, clock,
            notification_config_json=json.dumps({
                "dingtalk_enabled": True,
                "dingtalk_webhook_url": "https://example.invalid/hook",
            }),
        )
        monitor.collector.fetch_detail = AsyncMock(
            return_value=copy.deepcopy(profile(current=999, maximum=1000)))
        await sync_once(db, monitor, lead, clock)
        rows = await notices(db)
        assert len(rows) == 1
        assert "dingtalk" in rows[0][1]["channels"]


@pytest.mark.asyncio
async def test_notification_failure_does_not_break_sync(monitor, clock, monkeypatch):
    async with get_session_factory()() as db:
        lead = await make_lead(db, clock)
        monitor.collector.fetch_detail = AsyncMock(
            return_value=copy.deepcopy(profile(current=999, maximum=1000)))
        monkeypatch.setattr(
            "app.services.engine.orchestrator.enqueue_notification",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("outbox down")),
        )
        result = await sync_once(db, monitor, lead, clock)
        assert result["ok"] is True
        assert await notices(db) == []
