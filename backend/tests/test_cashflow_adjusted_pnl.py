"""今日盈亏现金流调整：净划转从权益差里扣掉，拉不到流水时为 null。

回归：用户 2026-11-21 凌晨往合约账户转了约 $124，看板却把这笔钱算成盈利
（显示 +$152.33，真实交易盈亏只有约 +$28）。修正后今日盈亏 = 权益 -
日初权益 - 当日净划转；流水未知时不拿权益差顶着，直接为 null。
"""
import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.time_util import BEIJING_TZ
from app.models.domain import TradingAccount
from app.services.adapters.base import BalanceInfo
from app.services.adapters.binance import BinanceAdapter
from app.services.dashboard_board import _account_row
from app.services.engine.cashflow import (
    beijing_day_window_ms,
    cash_flow_is_fresh,
    fetch_today_net_flow,
    net_flow_from_transfers,
    stored_today_net_flow,
)
from app.services.engine.equity import persist_equity, risk_state
from app.services.engine.orchestrator import EngineOrchestrator

TODAY = "2026-11-21"
START_MS, END_MS = beijing_day_window_ms(TODAY)


def _row(amount, kind, ts, asset="USDT", status="CONFIRMED"):
    return {"asset": asset, "amount": amount, "type": kind, "timestamp": ts, "tranId": 1}


def _account(**overrides):
    """_account_row 只读的账户替身：权益 390.02，日初 237.69，净划转 124。"""
    state = {"assets": {"USDT": {
        "equity": 390.02, "available": 330.0, "day_start": 237.69, "date": TODAY,
        "today_net_flow": 124.0, "flow_date": TODAY, "flow_updated_at": START_MS,
    }}}
    fields = {
        "id": 3, "name": "带单", "exchange": "binance", "environment": "live",
        "status": "connected", "risk_equity_json": json.dumps(state),
        "usdt_equity": 390.02, "avail_equity": 330.0, "last_sync_at": None,
        "positions_json": "[]",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


# ---- 日期窗口与求和 ----

def test_beijing_day_window_is_midnight_to_midnight_cst():
    start, end = beijing_day_window_ms("2026-11-21")
    assert datetime.fromtimestamp(start / 1000, tz=timezone.utc) == datetime(
        2026, 11, 20, 16, tzinfo=timezone.utc)
    assert datetime.fromtimestamp(end / 1000, tz=timezone.utc) == datetime(
        2026, 11, 21, 16, tzinfo=timezone.utc)
    assert end - start == 24 * 3600 * 1000


def test_net_flow_sums_in_minus_out_and_ignores_other_rows():
    rows = [
        _row(124.0, 1, START_MS + 1000),          # 当日转入
        _row(50.0, 2, START_MS + 2000),           # 当日转出
        _row(1000.0, 1, START_MS - 1000),         # 昨天，不算
        _row(999.0, 1, END_MS + 1000),            # 明天，不算
        _row(77.0, 3, START_MS + 3000),           # COIN-M 划转，不算
        _row(55.0, 1, START_MS + 4000, asset="BTC"),  # 非 USDT，不算
        {"asset": "USDT", "amount": "bad", "type": 1, "timestamp": START_MS + 5000},
    ]
    assert net_flow_from_transfers(rows, "USDT", START_MS, END_MS) == pytest.approx(74.0)


@pytest.mark.asyncio
async def test_fetch_today_net_flow_calls_adapter_with_day_window():
    adapter = SimpleNamespace(get_futures_transfer_history=AsyncMock(
        return_value=[_row(124.0, 1, START_MS + 1000), _row(20.0, 2, START_MS + 2000)]))
    assert await fetch_today_net_flow(adapter, "USDT", TODAY) == pytest.approx(104.0)
    args = adapter.get_futures_transfer_history.call_args.args
    assert args == ("USDT", START_MS, END_MS)


# ---- 适配器：收入历史 TRANSFER 条目 ----

@pytest.mark.asyncio
async def test_adapter_transfer_history_normalizes_income():
    adapter = BinanceAdapter({}, environment="live")
    seen = []

    async def fake_request(method, path, params=None, auth=True, retries=3, base_url=None):
        seen.append((method, path, params, base_url))
        return [
            {"symbol": "", "incomeType": "TRANSFER", "income": "124.0", "asset": "USDT",
             "info": "TRANSFER", "time": START_MS + 1000, "tranId": 1, "tradeId": ""},
            {"symbol": "", "incomeType": "TRANSFER", "income": "-50.0", "asset": "USDT",
             "info": "TRANSFER", "time": START_MS + 2000, "tranId": 2, "tradeId": ""},
            {"symbol": "ZECUSDT", "incomeType": "REALIZED_PNL", "income": "10.0", "asset": "USDT",
             "info": "123", "time": START_MS + 3000, "tranId": 3, "tradeId": "1"},
            {"symbol": "", "incomeType": "TRANSFER", "income": "5.0", "asset": "BTC",
             "info": "TRANSFER", "time": START_MS + 4000, "tranId": 4, "tradeId": ""},
            {"symbol": "", "incomeType": "TRANSFER", "income": "0", "asset": "USDT",
             "info": "TRANSFER", "time": START_MS + 5000, "tranId": 5, "tradeId": ""},
            {"symbol": "", "incomeType": "TRANSFER", "income": "bad", "asset": "USDT",
             "info": "TRANSFER", "time": START_MS + 6000, "tranId": 6, "tradeId": ""},
        ]

    adapter._request = fake_request
    rows = await adapter.get_futures_transfer_history("USDT", START_MS, END_MS)
    # 只留 USDT 的 TRANSFER：正数转入 type 1，负数转出 type 2；已实现盈亏、非 USDT、零额、坏值都过滤。
    assert rows == [
        {"asset": "USDT", "amount": 124.0, "type": 1, "timestamp": START_MS + 1000, "tranId": 1},
        {"asset": "USDT", "amount": 50.0, "type": 2, "timestamp": START_MS + 2000, "tranId": 2},
    ]
    # 走 fapi 域名、带 incomeType=TRANSFER 与日窗口。
    assert seen[0][1] == "/fapi/v1/income"
    assert seen[0][2]["incomeType"] == "TRANSFER"
    assert seen[0][2]["startTime"] == START_MS and seen[0][2]["endTime"] == END_MS


# ---- 看板：扣掉净划转 / 未知为 null ----

def test_account_row_deducts_today_net_flow():
    row = _account_row(_account(), TODAY)
    assert row["todayPnl"] == pytest.approx(390.02 - 237.69 - 124.0)
    assert row["todayPnlPct"] == pytest.approx((390.02 - 237.69 - 124.0) / 237.69 * 100)


def test_account_row_null_when_flow_unknown():
    state = {"assets": {"USDT": {
        "equity": 390.02, "available": 330.0, "day_start": 237.69, "date": TODAY}}}
    assert _account_row(_account(risk_equity_json=json.dumps(state)), TODAY)["todayPnl"] is None
    # 昨天的流水记录不能用于今天。
    stale = {"assets": {"USDT": {
        "equity": 390.02, "available": 330.0, "day_start": 237.69, "date": TODAY,
        "today_net_flow": 124.0, "flow_date": "2026-11-20"}}}
    assert _account_row(_account(risk_equity_json=json.dumps(stale)), TODAY)["todayPnl"] is None


def test_account_row_accumulates_multiple_transfers():
    # 当日两笔转入 124 + 50，全额扣掉。
    state = json.loads(_account().risk_equity_json)
    state["assets"]["USDT"]["today_net_flow"] = 174.0
    row = _account_row(_account(risk_equity_json=json.dumps(state)), TODAY)
    assert row["todayPnl"] == pytest.approx(390.02 - 237.69 - 174.0)


def test_stored_flow_and_freshness_helpers():
    state = risk_state(_account())
    assert stored_today_net_flow(state, TODAY) == pytest.approx(124.0)
    assert stored_today_net_flow(state, "2026-11-22") is None
    assert cash_flow_is_fresh(state, TODAY, START_MS + 60_000) is True
    assert cash_flow_is_fresh(state, TODAY, START_MS + 301_000) is False


# ---- 持久化：persist_equity 同一次 CAS 写流水 ----

@pytest.mark.asyncio
async def test_persist_equity_writes_cash_flow_in_same_update():
    account = SimpleNamespace(
        risk_equity_json=None, usdt_equity=0, avail_equity=0,
        hwm_equity=0, day_start_equity=0, day_start_date=None,
    )
    balance = BalanceInfo(390.02, 330.0, 390.02, 33.17, "USDT")
    await persist_equity(
        None, account, balance,
        cash_flow={"net_flow": 124.0, "date": TODAY, "updated_at_ms": START_MS + 1000},
    )
    state = risk_state(account)
    assert state["assets"]["USDT"]["today_net_flow"] == pytest.approx(124.0)
    assert state["assets"]["USDT"]["flow_date"] == TODAY
    row = _account_row(SimpleNamespace(
        id=3, name="带单", exchange="binance", environment="live", status="connected",
        risk_equity_json=account.risk_equity_json, usdt_equity=390.02, avail_equity=330.0,
        last_sync_at=None, positions_json="[]"), TODAY)
    # 日初由首个观测值建立（390.02），流水 124 仍被扣掉，不会算成盈利。
    assert row["todayPnl"] == pytest.approx(390.02 - 390.02 - 124.0)


# ---- 同步集成：拉不到流水不让同步失败，今日盈亏保持 null ----

@pytest.mark.asyncio
async def test_refresh_cash_flow_failure_leaves_flow_unknown():
    engine = EngineOrchestrator.__new__(EngineOrchestrator)
    failing = SimpleNamespace(get_futures_transfer_history=AsyncMock(
        side_effect=RuntimeError("SAPI permission denied")))
    account = SimpleNamespace(id=3, risk_equity_json=json.dumps({"assets": {}}))
    assert await engine._refresh_today_cash_flow(account, failing) is None
    # 成功时返回可写入 persist_equity 的记录。
    ok = SimpleNamespace(get_futures_transfer_history=AsyncMock(
        return_value=[{"asset": "USDT", "amount": 124.0, "type": 1,
                       "timestamp": START_MS + 1000, "tranId": 1}]))
    today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    start_ms, end_ms = beijing_day_window_ms(today)
    ok.get_futures_transfer_history.return_value = [
        {"asset": "USDT", "amount": 124.0, "type": 1, "timestamp": start_ms + 1000, "tranId": 1}]
    result = await engine._refresh_today_cash_flow(account, ok)
    assert result is not None and result["net_flow"] == pytest.approx(124.0)
    assert result["date"] == today
    # 刚写过的记录在 5 分钟内不再重复拉。
    account.risk_equity_json = json.dumps({"assets": {"USDT": {
        "today_net_flow": 124.0, "flow_date": today,
        "flow_updated_at": int(datetime.now(timezone.utc).timestamp() * 1000)}}})
    assert await engine._refresh_today_cash_flow(account, ok) is None
    assert ok.get_futures_transfer_history.call_count == 1
