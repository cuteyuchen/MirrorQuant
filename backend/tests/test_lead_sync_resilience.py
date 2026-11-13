"""Detail degradation, durable sync incidents and bounded state-change logging."""
import copy
import asyncio
import importlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import get_session_factory
from app.models.domain import LeadSource, NotificationOutbox
from app.services.engine.orchestrator import (
    EngineOrchestrator, SYNC_FAILURE_ERROR_INTERVAL, SYNC_FAILURE_FIELDS, SYNC_FAILURE_WARN_INTERVAL,
)
from app.services.lead_collector.binance_bapi import (
    BUSY_CODE, DETAIL_DEGRADED_WARN_INTERVAL, HISTORY_RETRY_BUDGET,
    BinanceBapiCollector, describe_exception, is_authoritative_snapshot,
)
from app.services.lead_collector.freshness import lead_freshness
from app.services.notifier.outbox import enqueue_notification
from app.services.proxy.manager import ProxyManager


PROFILE = {
    "nickname": "previous nickname", "avatarUrl": "https://example.invalid/avatar",
    "description": "previous description", "badgeName": "previous badge", "status": "ONGOING",
    "isPaused": False, "positionShow": False, "tags": ["previous tag"], "signTradFi": True,
    "startTime": 100, "lastTradeTime": 200, "aumAmount": 12345.6, "copyAum": 789,
    "marginBalance": 1234.5, "copierPnl": 42, "currentCopyCount": 7,
    "maxCopyCount": 99, "totalCopyCount": 77, "favoriteCount": 6, "mockCopyCount": 5,
    "profitSharingRate": 10, "sharpRatio": 1.2, "aiSummary": {"text": "old summary"},
}
ORDER = {
    "orderId": "test-order", "orderTime": 1000, "orderUpdateTime": 1000,
    "symbol": "BTCUSDT", "side": "BUY", "positionSide": "LONG", "type": "MARKET",
    "executedQty": "2", "avgPrice": "100",
}


@pytest.fixture
def monitor(monkeypatch):
    engine = EngineOrchestrator()
    engine.collector.fetch_detail = AsyncMock(return_value=copy.deepcopy(PROFILE))
    engine.collector.fetch_order_history = AsyncMock(return_value=[ORDER])
    engine.collector.fetch_position_history = AsyncMock(return_value={"list": [], "total": 0})
    engine.collector.fetch_holdings = AsyncMock(return_value=[{
        "symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": "2",
        "entryPrice": "100", "markPrice": "110",
    }])
    engine.router.process_lead_events = AsyncMock()
    engine.ws_hub.broadcast = AsyncMock()
    monkeypatch.setattr(ProxyManager, "ordered_proxy_urls", lambda *args, **kwargs: [None])
    return engine


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=time.time())
    monkeypatch.setattr("app.services.engine.orchestrator.time.time", lambda: state.now)
    return state


@pytest.fixture
def log_clock(monkeypatch):
    state = SimpleNamespace(now=0.0)
    # Isolate the log clock from asyncio's timeout/event-loop clock.
    for name in ("app.services.engine.orchestrator", "app.services.lead_collector.binance_bapi"):
        monkeypatch.setattr(importlib.import_module(name), "time", SimpleNamespace(
            time=time.time, perf_counter=time.perf_counter, monotonic=lambda: state.now,
        ))
    return state


async def make_lead(db, clock, **overrides):
    values = dict(
        name="resilience test", portfolio_id="test-portfolio", status="running", is_enabled=True,
        last_sync_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        snapshot_json=json.dumps({"meta": {"generatedAt": int(clock.now * 1000)},
                                  "profile": PROFILE, "openPositionsMeta": {}}),
    )
    lead = LeadSource(**{**values, **overrides})
    db.add(lead)
    await db.commit()
    return lead


async def run_syncs(db, engine, lead, clock, count, *, ok):
    results = []
    for _ in range(count):
        clock.now += 0.01  # Each collection is a distinct attempt, without real sleeps.
        result = await engine.sync_lead(db, lead, proxies=[])
        assert result["ok"] is ok and not result.get("superseded")
        results.append(result)
    return results


async def notices(db):
    return list((await db.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id))).all())


def mock_http(monkeypatch, outcomes):
    calls = []
    remaining = list(outcomes)

    def respond(request):
        calls.append(request)
        value = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        if isinstance(value, Exception):
            raise value
        return value

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(ProxyManager, "build_client", lambda **kwargs: httpx.AsyncClient(transport=transport))
    sleep = AsyncMock()
    monkeypatch.setattr("app.services.lead_collector.binance_bapi.asyncio.sleep", sleep)
    return calls, sleep


@pytest.mark.parametrize("exception", [httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ConnectError])
def test_empty_exception_description_has_type_and_context(exception):
    exc = exception("")
    assert str(exc) == ""
    assert describe_exception(exc) == f"{exception.__name__} status=None reqid=None (empty message)"


def test_http_exception_description_keeps_status_request_id_and_message():
    response = httpx.Response(503, headers={"x-mbx-request-id": "test-request-503"})
    exc = httpx.HTTPStatusError("upstream unavailable", request=None, response=response)
    text = describe_exception(exc)
    assert "HTTPStatusError" in text and "status=503" in text
    assert "reqid=test-request-503" in text and "upstream unavailable" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["_http_json", "_fapi_get"])
async def test_request_wrappers_describe_empty_transport_errors(monkeypatch, method):
    calls, _ = mock_http(monkeypatch, [httpx.RemoteProtocolError("")])
    with pytest.raises(RuntimeError) as caught:
        await getattr(BinanceBapiCollector(), method)("https://example.invalid", retries=1)
    message = str(caught.value)
    assert len(calls) == 1 and "RemoteProtocolError" in message
    assert message.partition("): ")[2].strip()
    assert "status=None reqid=None (empty message)" in message


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["_http_json", "_fapi_get"])
@pytest.mark.parametrize("status,attempts", [(400, 1), (403, 1), (429, 3), (503, 3)])
async def test_request_wrappers_retry_only_transient_http_statuses(monkeypatch, method, status, attempts):
    calls, sleep = mock_http(monkeypatch, [httpx.Response(status, headers={"x-request-id": "test-id"})])
    with pytest.raises(RuntimeError) as caught:
        await getattr(BinanceBapiCollector(), method)("https://example.invalid", retries=3)
    assert len(calls) == attempts and sleep.await_count == attempts - 1
    assert f"status={status}" in str(caught.value) and "reqid=test-id" in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    httpx.ReadTimeout(""), httpx.ConnectError(""), httpx.RemoteProtocolError(""),
    httpx.Response(503), httpx.Response(429),
    httpx.Response(200, json={"code": BUSY_CODE}),
])
async def test_detail_retries_independently_with_exponential_backoff(monkeypatch, failure):
    calls, sleep = mock_http(monkeypatch, [failure, failure, httpx.Response(
        200, json={"code": "000000", "data": PROFILE},
    )])
    assert await BinanceBapiCollector().fetch_detail("test-portfolio") == PROFILE
    assert len(calls) == 3
    assert [call.args[0] for call in sleep.await_args_list] == [0.5, 1.0]


@pytest.mark.asyncio
async def test_detail_does_not_retry_permanent_http_failure(monkeypatch):
    calls, sleep = mock_http(monkeypatch, [httpx.Response(404)])
    with pytest.raises(RuntimeError, match="404"):
        await BinanceBapiCollector().fetch_detail("test-portfolio")
    assert len(calls) == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["fetch_order_history", "fetch_position_history"])
@pytest.mark.parametrize("code", [BUSY_CODE, int(BUSY_CODE)])
async def test_history_busy_twice_then_success(monkeypatch, endpoint, code):
    data = {"list": [ORDER], "total": 1}
    calls, sleep = mock_http(monkeypatch, [
        httpx.Response(200, json={"code": code}),
        httpx.Response(200, json={"code": code}),
        httpx.Response(200, json={"code": "000000", "data": data}),
    ])
    result = await getattr(BinanceBapiCollector(), endpoint)("test-portfolio")
    assert result == (data["list"] if endpoint == "fetch_order_history" else data)
    assert len(calls) == 3
    assert [call.args[0] for call in sleep.await_args_list] == [0.8, 1.6]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["fetch_order_history", "fetch_position_history"])
@pytest.mark.parametrize("failure", [
    httpx.ReadTimeout(""), httpx.ConnectError(""), httpx.RemoteProtocolError(""),
    httpx.Response(429), httpx.Response(503), httpx.Response(200, json={"code": BUSY_CODE}),
])
async def test_history_has_four_attempts_and_capped_exponential_backoff(monkeypatch, endpoint, failure):
    calls, sleep = mock_http(monkeypatch, [failure])
    with pytest.raises(RuntimeError):
        await getattr(BinanceBapiCollector(), endpoint)("test-portfolio")
    assert len(calls) == 4
    assert [call.args[0] for call in sleep.await_args_list] == [0.8, 1.6, 2.4]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["fetch_order_history", "fetch_position_history"])
@pytest.mark.parametrize("failure", [
    httpx.Response(400), httpx.Response(403), httpx.Response(404),
    httpx.Response(200, json={"code": "permanent", "message": "invalid portfolio"}),
])
async def test_history_permanent_failure_does_not_retry(monkeypatch, endpoint, failure):
    calls, sleep = mock_http(monkeypatch, [failure])
    with pytest.raises(RuntimeError):
        await getattr(BinanceBapiCollector(), endpoint)("test-portfolio")
    assert len(calls) == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["fetch_order_history", "fetch_position_history"])
async def test_history_total_retry_budget_cancels_slow_request(monkeypatch, endpoint):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def respond(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(ProxyManager, "build_client", lambda **kwargs: httpx.AsyncClient(transport=transport))
    monkeypatch.setattr("app.services.lead_collector.binance_bapi.HISTORY_RETRY_BUDGET", 0.02)
    with pytest.raises(RuntimeError, match="TimeoutError") as caught:
        await getattr(BinanceBapiCollector(), endpoint)("test-portfolio")
    assert isinstance(caught.value.__cause__, TimeoutError)
    assert entered.is_set() and cancelled.is_set()
    assert HISTORY_RETRY_BUDGET == 10.0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [httpx.RemoteProtocolError(""), httpx.Response(
    503, headers={"x-mbx-request-id": "test-exhausted-request"},
)])
async def test_exhausted_detail_retries_degrade_with_diagnostic_context(monkeypatch, monitor, failure):
    monkeypatch.delattr(monitor.collector, "fetch_detail")  # Exercise the real retry wrapper.
    calls, sleep = mock_http(monkeypatch, [failure])
    snapshot = await monitor.collector.sync_snapshot(
        "test-portfolio", {}, [], with_analysis=False, previous_snapshot={"profile": PROFILE},
    )
    assert len(calls) == 3 and sleep.await_count == 2
    assert snapshot["meta"]["detailOk"] is False and snapshot["openPositionsMeta"]["partial"] is True
    assert snapshot["profile"] == PROFILE
    reason = snapshot["meta"]["detailError"]
    if isinstance(failure, Exception):
        assert "RemoteProtocolError" in reason and "(empty message)" in reason
    else:
        assert "status=503" in reason and "reqid=test-exhausted-request" in reason


@pytest.mark.asyncio
@pytest.mark.parametrize("position_show", [False, True])
async def test_detail_failure_preserves_entire_previous_profile(monitor, position_show, caplog):
    caplog.set_level(logging.WARNING)
    previous = {"profile": {**copy.deepcopy(PROFILE), "positionShow": position_show}}
    original = copy.deepcopy(previous)
    monitor.collector.fetch_detail.side_effect = httpx.RemoteProtocolError("")
    snapshot = await monitor.collector.sync_snapshot(
        "test-portfolio", {}, [], with_analysis=False, previous_snapshot=previous,
    )
    assert snapshot["meta"]["detailOk"] is False
    assert snapshot["openPositionsMeta"]["partial"] is True
    assert snapshot["profile"] == original["profile"] and previous == original
    assert snapshot["openPositionsSummary"]["marginBalance"] == PROFILE["marginBalance"]
    assert not is_authoritative_snapshot(snapshot)
    assert monitor.collector.fetch_holdings.await_count == int(position_show)
    assert "test-portfolio detail degraded" in caplog.text and "RemoteProtocolError" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", [None, {"profile": {}}, {"profile": {"positionShow": None}}])
async def test_detail_failure_without_previous_equity_keeps_derived_equity_unknown(monitor, previous):
    monitor.collector.fetch_detail.side_effect = httpx.ReadTimeout("")
    snapshot = await monitor.collector.sync_snapshot(
        "test-portfolio", {}, [], with_analysis=False, previous_snapshot=previous,
    )
    assert snapshot["profile"]["positionShow"] is False
    assert snapshot["profile"].get("marginBalance") is None
    monitor.collector.fetch_holdings.assert_not_awaited()
    summary = snapshot["openPositionsSummary"]
    for field in ("marginBalance", "availableMargin", "totalNotionalLeverage", "entryLeverage",
                  "maxPositionLeverage", "marginCoveragePct", "estMaintenanceMarginRatioPct",
                  "totalMarginRoePct", "adverseMoveBufferPct"):
        assert summary[field] is None
    assert snapshot["openPositions"]
    for position in snapshot["openPositions"]:
        for field in ("notionalLeverage", "entryLeverage", "uPnLRoePct", "maintMarginRoePct"):
            assert position[field] is None


@pytest.mark.asyncio
async def test_ten_detail_failures_publish_without_alerts_or_repeated_unverified_warning(
    monitor, clock, async_client, caplog, monkeypatch,
):
    caplog.set_level(logging.DEBUG, logger="mirrorquant.orchestrator")
    caplog.set_level(logging.DEBUG, logger="mirrorquant.binance_bapi")
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main.settings, "enable_background_engine", True)
    monkeypatch.setattr(main, "orchestrator", SimpleNamespace(heartbeat_age_sec=lambda: 0.1, is_running=True))
    monitor.collector.fetch_detail.side_effect = httpx.RemoteProtocolError("")
    async with get_session_factory()() as db:
        lead = await make_lead(db, clock)
        last_full_sync = lead.last_sync_at
        results = []
        for _ in range(10):
            results.extend(await run_syncs(db, monitor, lead, clock, 1, ok=True))
            assert lead.last_error.startswith("degraded: ") and "RemoteProtocolError" in lead.last_error
        assert not await notices(db)
        assert lead.status == "running" and lead.last_error.startswith("degraded: ")
        assert "RemoteProtocolError" in lead.last_error
        assert lead.last_sync_at.replace(tzinfo=timezone.utc) == last_full_sync.replace(tzinfo=timezone.utc)
        for result in results:
            assert result["snapshot"]["profile"] == PROFILE
            assert result["snapshot"]["meta"]["detailOk"] is False
            assert result["snapshot"]["openPositionsMeta"]["partial"] is True
            assert not result["snapshot"]["openPositionsMeta"].get("consecutiveSyncFailures")
        assert lead_freshness(lead, results[-1]["snapshot"])["stale"] is False
        assert (await async_client.get("/api/ready")).status_code == 200
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING
                and "snapshot is unverified" in record.message]
    assert len(warnings) == 1
    detail_logs = [record for record in caplog.records if "detail degraded" in record.message]
    assert [record.levelno for record in detail_logs] == [logging.WARNING] + [logging.DEBUG] * 9
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_ten_real_failures_alert_once_and_bound_error_logs(monitor, clock, caplog):
    caplog.set_level(logging.DEBUG, logger="mirrorquant.orchestrator")
    monitor.collector.sync_snapshot = AsyncMock(side_effect=httpx.RemoteProtocolError(""))
    async with get_session_factory()() as db:
        lead = await make_lead(db, clock)
        await run_syncs(db, monitor, lead, clock, 10, ok=False)
        rows = await notices(db)
        assert len(rows) == 1
        assert json.loads(rows[0].payload)["event_key"] == f"lead-sync-failure:{lead.id}"
        assert lead.status == "error" and "RemoteProtocolError" in lead.last_error
        assert json.loads(lead.snapshot_json)["openPositionsMeta"]["consecutiveSyncFailures"] == 10
    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert "consecutiveFailures=1" in errors[0].message
    failure_logs = [record for record in caplog.records if "sync failed" in record.message]
    assert len(failure_logs) == 10
    assert [record.levelno for record in failure_logs] == [logging.ERROR] + [logging.DEBUG] * 9
    assert f"consecutiveFailures={get_settings().lead_sync_failure_limit}" in failure_logs[
        get_settings().lead_sync_failure_limit - 1
    ].message


@pytest.mark.asyncio
@pytest.mark.parametrize("recover_between_failures", [False, True])
async def test_twenty_failures_in_ten_minutes_bound_all_error_and_warning_logs(
    monitor, clock, log_clock, caplog, recover_between_failures,
):
    caplog.set_level(logging.DEBUG, logger="mirrorquant.orchestrator")
    async with get_session_factory()() as db:
        lead = await make_lead(db, clock)
        for attempt in range(20):
            elapsed = attempt * SYNC_FAILURE_ERROR_INTERVAL / 19
            log_clock.now = elapsed
            clock.now += SYNC_FAILURE_ERROR_INTERVAL / 19
            if attempt and recover_between_failures:
                monitor.collector.fetch_order_history.side_effect = None
                await run_syncs(db, monitor, lead, clock, 1, ok=True)
            monitor.collector.fetch_order_history.side_effect = RuntimeError("intermittent busy")
            await run_syncs(db, monitor, lead, clock, 1, ok=False)
            assert lead.status == "error" and "intermittent busy" in lead.last_error
        if not recover_between_failures:
            assert len(await notices(db)) == 1
            assert json.loads(lead.snapshot_json)["openPositionsMeta"]["consecutiveSyncFailures"] == 20
    errors = [record for record in caplog.records if record.levelno == logging.ERROR and "sync failed" in record.message]
    assert len(errors) == 2
    assert len([record for record in caplog.records if record.levelno == logging.WARNING]) <= 3
    assert SYNC_FAILURE_WARN_INTERVAL == 300.0


@pytest.mark.asyncio
async def test_error_budget_slides_across_recoveries_and_closed_window_boundary(
    monitor, clock, log_clock, caplog,
):
    caplog.set_level(logging.DEBUG, logger="mirrorquant.orchestrator")
    error_times = []
    async with get_session_factory()() as db:
        lead = await make_lead(db, clock)
        for elapsed, should_error in [(0, True), (1, True), (2, False), (600, False),
                                      (600.001, True), (601, False), (601.001, True)]:
            log_clock.now = elapsed
            monitor.collector.fetch_order_history.side_effect = RuntimeError("busy")
            before = len([record for record in caplog.records if record.levelno == logging.ERROR])
            await run_syncs(db, monitor, lead, clock, 1, ok=False)
            after = len([record for record in caplog.records if record.levelno == logging.ERROR])
            assert after - before == int(should_error)
            if should_error:
                error_times.append(elapsed)
            assert len([at for at in error_times if elapsed - at <= SYNC_FAILURE_ERROR_INTERVAL]) <= 2
            monitor.collector.fetch_order_history.side_effect = None
            await run_syncs(db, monitor, lead, clock, 1, ok=True)


@pytest.mark.asyncio
async def test_detail_degraded_warning_is_per_portfolio_and_time_bounded(monitor, log_clock, caplog):
    caplog.set_level(logging.DEBUG, logger="mirrorquant.binance_bapi")
    monitor.collector.fetch_detail.side_effect = httpx.ReadTimeout("")
    for portfolio, elapsed in [("a", 0), ("a", 1), ("b", 1), ("a", DETAIL_DEGRADED_WARN_INTERVAL - 0.001),
                               ("a", DETAIL_DEGRADED_WARN_INTERVAL)]:
        log_clock.now = elapsed
        snapshot = await monitor.collector.sync_snapshot(
            portfolio, {}, [], with_analysis=False, previous_snapshot={"profile": PROFILE},
        )
        assert snapshot["meta"]["detailOk"] is False
        assert snapshot["openPositionsMeta"]["partial"] is True
    assert [record.levelno for record in caplog.records if "detail degraded" in record.message] == [
        logging.WARNING, logging.DEBUG, logging.WARNING, logging.DEBUG, logging.WARNING,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["fetch_order_history", "fetch_position_history"])
async def test_history_failure_is_not_hidden_by_detail_degradation(monitor, clock, endpoint, caplog):
    caplog.set_level(logging.ERROR)
    monitor.collector.fetch_detail.side_effect = httpx.ReadTimeout("")
    getattr(monitor.collector, endpoint).side_effect = RuntimeError(f"{endpoint} unavailable")
    async with get_session_factory()() as db:
        lead = await make_lead(db, clock)
        await run_syncs(db, monitor, lead, clock, 10, ok=False)
        assert len(await notices(db)) == 1
        assert lead.status == "error" and endpoint in lead.last_error
        assert endpoint in caplog.text


@pytest.mark.asyncio
async def test_partial_publications_preserve_incident_and_do_not_rearm_alert(monitor, clock):
    async with get_session_factory()() as db:
        lead = await make_lead(db, clock)
        monitor.collector.fetch_order_history.side_effect = RuntimeError("order-history unavailable")
        await run_syncs(db, monitor, lead, clock, get_settings().lead_sync_failure_limit, ok=False)
        before = json.loads(lead.snapshot_json)["openPositionsMeta"]
        assert len(await notices(db)) == 1 and before["syncFailureNotified"] is True
        last_full_sync = lead.last_sync_at
        monitor.collector.fetch_order_history.side_effect = None
        monitor.collector.fetch_detail.side_effect = httpx.ReadTimeout("")
        await run_syncs(db, monitor, lead, clock, 10, ok=True)
        after = json.loads(lead.snapshot_json)["openPositionsMeta"]
        assert {key: after[key] for key in SYNC_FAILURE_FIELDS} == {
            key: before[key] for key in SYNC_FAILURE_FIELDS
        }
        assert lead.last_sync_at == last_full_sync and lead.status == "running"
        assert len(await notices(db)) == 1
        monitor.collector.fetch_order_history.side_effect = RuntimeError("order-history still unavailable")
        await run_syncs(db, monitor, lead, clock, 10, ok=False)
        assert len(await notices(db)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "sending", "sent", "failed"])
async def test_event_key_survives_lost_snapshot_flags_and_orchestrator_restart(monitor, clock, status):
    async with get_session_factory()() as db:
        lead = await make_lead(db, clock)
        row = enqueue_notification(db, {}, "previous incident", "body", event_type="error",
                                   event_key=f"lead-sync-failure:{lead.id}")
        row.status = status
        # Parsing must also handle compact payload JSON, independently of formatting.
        row.payload = json.dumps(json.loads(row.payload), separators=(",", ":"))
        enqueue_notification(db, {}, "different event", "body", event_key=f"other-event:{lead.id}")
        enqueue_notification(db, {}, "different lead", "body", event_key=f"lead-sync-failure:{lead.id + 1}")
        await db.commit()
        restarted = EngineOrchestrator()
        restarted.collector.sync_snapshot = AsyncMock(side_effect=RuntimeError("history unavailable"))
        restarted.router._get_config = AsyncMock()
        await run_syncs(db, restarted, lead, clock, 10, ok=False)
        assert len(await notices(db)) == 3
        assert json.loads(lead.snapshot_json)["openPositionsMeta"]["syncFailureNotified"] is True
        restarted.router._get_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_frozen_clock_success_then_failure_is_recorded(monitor, clock, monkeypatch):
    clock.now = 1_700_000_000.125
    started_ms = int(clock.now * 1000)

    class AheadDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(clock.now + 0.008, tz=tz)

    # Reproduce the independent datetime clock being ahead without real time.
    monkeypatch.setattr("app.services.engine.orchestrator.datetime", AheadDatetime)
    async with get_session_factory()() as db:
        lead = await make_lead(
            db, clock, last_sync_at=datetime.fromtimestamp(clock.now - 60, tz=timezone.utc),
        )
        success = await monitor.sync_lead(db, lead, proxies=[])
        assert success["ok"] is True and not success.get("superseded")

        monitor.collector.fetch_order_history.side_effect = RuntimeError("history unavailable")
        failure = await monitor.sync_lead(db, lead, proxies=[])
        assert failure["ok"] is False and not failure.get("superseded")
        snapshot = json.loads(lead.snapshot_json)
        assert snapshot["openPositionsMeta"]["consecutiveSyncFailures"] == 1
        assert lead.status == "error" and "history unavailable" in lead.last_error
        assert lead.last_sync_at.replace(tzinfo=timezone.utc) == datetime.fromtimestamp(
            started_ms / 1000, tz=timezone.utc,
        )

        # Legacy snapshots without generatedAt retain the successful attempt time.
        snapshot["meta"].pop("generatedAt")
        freshness = lead_freshness(lead, snapshot)
        assert freshness["generatedAt"] == started_ms
        assert freshness["snapshotAgeSec"] == 0
        assert freshness["stale"] is True
        assert freshness["consecutiveSyncFailures"] == 1


@pytest.mark.asyncio
async def test_full_detail_recovery_clears_failure_state_and_rearms_alert(monitor, clock, log_clock, caplog):
    caplog.set_level(logging.DEBUG, logger="mirrorquant.orchestrator")
    async with get_session_factory()() as db:
        lead = await make_lead(db, clock)
        monitor.collector.fetch_order_history.side_effect = RuntimeError("history unavailable")
        limit = get_settings().lead_sync_failure_limit
        await run_syncs(db, monitor, lead, clock, limit, ok=False)
        first_notice = (await notices(db))[0]
        monitor.collector.fetch_order_history.side_effect = None
        monitor.collector.fetch_detail.side_effect = httpx.ReadTimeout("")
        await run_syncs(db, monitor, lead, clock, 1, ok=True)
        assert lead.last_error.startswith("degraded: ")
        assert not [record for record in caplog.records if "sync recovered" in record.message]
        monitor.collector.fetch_detail.side_effect = None
        result = (await run_syncs(db, monitor, lead, clock, 1, ok=True))[0]
        assert result["snapshot"]["meta"]["detailOk"] is True
        assert result["snapshot"]["openPositionsMeta"]["partial"] is False
        assert all(field not in result["snapshot"]["openPositionsMeta"] for field in SYNC_FAILURE_FIELDS)
        assert lead.last_error is None and lead.status == "running"
        assert lead.last_sync_at.replace(tzinfo=timezone.utc) > first_notice.created_at.replace(tzinfo=timezone.utc)
        assert lead.id not in monitor._sync_failure_log_at
        await run_syncs(db, monitor, lead, clock, 1, ok=True)
        recovered = [record for record in caplog.records if "sync recovered" in record.message]
        assert len(recovered) == 1 and recovered[0].levelno == logging.INFO
        monitor.collector.fetch_order_history.side_effect = RuntimeError("history unavailable again")
        await run_syncs(db, monitor, lead, clock, limit, ok=False)
        assert len(await notices(db)) == 2
    errors = [record for record in caplog.records if record.levelno == logging.ERROR and "sync failed" in record.message]
    assert len(errors) == 2
    assert all("consecutiveFailures=1" in record.message for record in errors)


@pytest.mark.asyncio
async def test_unverified_warning_rearms_only_after_verified_state(monitor, clock, caplog):
    caplog.set_level(logging.WARNING, logger="mirrorquant.orchestrator")
    async with get_session_factory()() as db:
        lead = await make_lead(db, clock)
        await run_syncs(db, monitor, lead, clock, 10, ok=True)
        monitor.collector.fetch_detail.return_value = {**PROFILE, "positionShow": True}
        await run_syncs(db, monitor, lead, clock, 1, ok=True)
        monitor.router.process_lead_events.assert_awaited_once()
        monitor.collector.fetch_detail.return_value = PROFILE
        await run_syncs(db, monitor, lead, clock, 10, ok=True)
    assert len([record for record in caplog.records if record.levelno == logging.WARNING
                and "snapshot is unverified" in record.message]) == 2
