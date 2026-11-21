"""新看板契约、管理员权限和未知值回归，所有数据均为本地夹具。"""
import csv
import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import unquote

import pytest
from sqlalchemy import select

from app.core.database import get_session_factory
from app.core.security import CredentialVault, create_access_token
from app.models.domain import (
    BindingDelivery, CopyBinding, ExecutionRecord, LeadSource, OrderIntent,
    PositionAllocation, PositionGuardState, ProxyNode, SystemConfig, TradingAccount, User,
)
from app.services.dashboard_board import _realized_pnl, build_board, milliseconds
from app.services.proxy.manager import ProxyManager

NOW = datetime(2026, 11, 15, 0, 30, tzinfo=timezone.utc)
START = datetime(2026, 11, 14, 16, tzinfo=timezone.utc)
NOW_MS, START_MS = milliseconds(NOW), milliseconds(START)


@pytest.mark.parametrize("row,expected", [
    ({"pnl": 0}, None), ({"pnl": "0"}, None),
    ({"pnl": 3, "pnlKnown": False}, None), ({"pnl": 3, "pnlKnown": None}, None),
    ({"pnl": 0, "pnlKnown": True}, 0), ({"totalPnl": 0, "pnlKnown": True}, 0),
    ({"pnl": -3}, -3), ({"pnl": "NaN", "pnlKnown": True}, None),
])
def test_board_realized_pnl_requires_unambiguous_evidence(row, expected):
    # 字符串零和显式未知标记不能升级成真实零收益。
    assert _realized_pnl(row) == expected


@pytest.fixture
async def board_state(async_client, monkeypatch):
    # 这些接口不应创建任何 HTTP 传输，包括缓存未命中时。
    network = Mock(side_effect=AssertionError("Dashboard must never contact exchanges"))
    monkeypatch.setattr(ProxyManager, "build_client", network)
    async with get_session_factory()() as db:
        admin = User(username="board-admin", password_hash="unused", role="admin")
        viewer = User(username="board-viewer", password_hash="unused", role="viewer")
        proxy = ProxyNode(name="offline", url="http://proxy.invalid:8080")
        account = TradingAccount(
            name="Account", exchange="binance", environment="demo",
            encrypted_credentials=CredentialVault().encrypt_dict({"api_key": "synthetic-key", "secret_key": "synthetic-secret"}),
            api_key_masked="masked", last_sync_at=NOW, status="connected",
            risk_equity_json=json.dumps({"assets": {
                "USDT": {"equity": 1000, "available": 800, "day_start": 900, "date": "2026-11-15",
                         "today_net_flow": 0, "flow_date": "2026-11-15"},
                "USDC": {"equity": 5000, "available": 4000, "day_start": 4000, "date": "2026-11-15"},
            }}),
            positions_json=json.dumps([{"instId": "BTCUSDT", "symbol": "BTCUSDT", "posSide": "long",
                                      "pos": 1, "avgPx": 100, "markPrice": 110, "notionalUsd": 110, "upl": 10}]),
        )
        empty = TradingAccount(name="Unsynced", exchange="okx", api_key_masked="masked", encrypted_credentials="unused")
        lead = LeadSource(name="交易员", portfolio_id="board-portfolio", status="running", last_sync_at=NOW,
            snapshot_json=json.dumps({
                "meta": {"generatedAt": NOW_MS, "orderHistoryComplete": True},
                "profile": {"marginBalance": 1000, "positionShow": True},
                "openPositions": [{"symbol": "BTCUSDT", "positionSide": "LONG", "qty": 1, "avgEntry": 100,
                                   "markPrice": 110, "uPnL": 10, "uPnLPct": 10, "leverage": 5,
                                   "markValue": 110, "openTime": START_MS + 1000}],
                "openPositionsMeta": {"authoritative": True, "complete": True, "asOf": NOW_MS},
            }),
            orders_store_json=json.dumps({"opening": {"h": "opening", "sym": "BTCUSDT", "ps": "LONG",
                "side": "BUY", "q": "1", "p": "100", "t": START_MS + 1000, "ot": START_MS + 1000}}),
        )
        db.add_all([admin, viewer, proxy, account, empty, lead])
        await db.flush()
        binding = CopyBinding(name="Primary", lead_id=lead.id, account_id=account.id,
            tp_sl_mode="independent", enable_take_profit=True, take_profit_pct=10,
            enable_stop_loss=True, stop_loss_pct=5)
        db.add(binding)
        await db.flush()
        allocation = PositionAllocation(binding_id=binding.id, account_id=account.id, inst_id="BTCUSDT",
            pos_side="long", owned_qty=1, owned_qty_exact="1", entry_cost=100, entry_cost_exact="100",
            opened_at_ms=START_MS + 1000)
        db.add(allocation)
        await db.commit()
        tokens = {u.role: create_access_token(u.id, u.username, u.role) for u in (admin, viewer)}
        async_client.headers["Authorization"] = f"Bearer {tokens['admin']}"
        yield SimpleNamespace(db=db, client=async_client, tokens=tokens, account=account, empty=empty,
                              lead=lead, binding=binding, allocation=allocation, proxy=proxy, network=network)
    network.assert_not_called()


async def test_board_utc8_contract_and_currency_isolation(board_state):
    t = board_state
    board = await build_board(t.db, now=NOW)
    assert set(board) == {"serverTime", "dayStart", "globalKillSwitch", "alerts", "accounts", "leads"}
    assert board["serverTime"] == NOW_MS and board["dayStart"] == START_MS
    account = board["accounts"][0]
    assert account["equity"] == 1000 and account["available"] == 800  # 不并入 USDC。
    assert account["todayPnl"] == 100 and account["todayPnlPct"] == pytest.approx(100 / 9)
    assert account["unrealizedPnl"] == 10 and account["lastSyncAt"] == NOW_MS
    lead = board["leads"][0]
    position = lead["positions"][0]
    assert position["key"] == "BTCUSDT_LONG" and position["openedToday"] is True
    assert position["takeProfitPrice"] == pytest.approx(110) and position["stopLossPrice"] == 95
    mine = position["mine"][0]
    assert mine["state"] == "aligned" and mine["qty"] == mine["expectedQty"] == 1
    assert mine["pnl"] == mine["pnlPct"] == 10
    assert lead["todayEvents"][0]["action"] == "OPEN" and lead["todayOpenCount"] == 1
    assert lead["todayRealizedPnl"] is None
    assert set(mine) == {"bindingId", "bindingName", "accountId", "accountName", "environment",
                        "bindingEnabled", "breakerTripped", "state", "qty", "entryPrice", "pnl",
                        "pnlPct", "expectedQty", "reason"}


async def test_board_unknown_values_are_null_and_old_day_baseline_is_not_reused(board_state):
    t = board_state
    board = await build_board(t.db, now=NOW)
    unsynced = next(a for a in board["accounts"] if a["id"] == t.empty.id)
    for key in ("equity", "available", "unrealizedPnl", "todayPnl", "todayPnlPct", "lastSyncAt"):
        assert unsynced[key] is None
    state = json.loads(t.account.risk_equity_json)
    state["assets"]["USDT"]["date"] = "2026-11-14"
    t.account.risk_equity_json = json.dumps(state)
    snapshot = json.loads(t.lead.snapshot_json)
    del snapshot["openPositions"][0]["openTime"]
    t.lead.snapshot_json = json.dumps(snapshot)
    await t.db.commit()
    board = await build_board(t.db, now=NOW)
    assert board["accounts"][0]["todayPnl"] is None
    assert board["leads"][0]["positions"][0]["openedAt"] is None
    assert board["leads"][0]["positions"][0]["openedToday"] is None


@pytest.mark.parametrize("evidence", ["incomplete", "future", "stale"])
async def test_board_unproven_snapshot_never_claims_confirmed_orphans(board_state, evidence):
    # 不完整、未来或过期的列表都不能证明带单员已经没有其他仓位。
    t = board_state
    t.db.add(PositionAllocation(binding_id=t.binding.id, account_id=t.account.id, inst_id="ETHUSDT",
        pos_side="short", owned_qty=2, entry_cost=400))
    snapshot = json.loads(t.lead.snapshot_json)
    if evidence == "incomplete":
        snapshot["openPositionsMeta"]["complete"] = False
    else:
        snapshot["openPositionsMeta"]["asOf"] = NOW_MS + 1000 if evidence == "future" else START_MS
    t.lead.snapshot_json = json.dumps(snapshot)
    await t.db.commit()
    lead = (await build_board(t.db, now=NOW))["leads"][0]
    assert lead["orphans"] is None
    assert lead["positions"][0]["mine"][0]["state"] == "unknown"
    if evidence != "incomplete":
        assert lead["stale"] is True


async def test_board_missing_positions_and_corrupt_amounts_are_unknown(board_state):
    # 缺失列表与损坏金额不允许伪装成空仓、零资产或有效数量。
    t = board_state
    t.lead.snapshot_json = json.dumps({"meta": {"generatedAt": NOW_MS}})
    t.account.risk_equity_json = json.dumps({"assets": {"USDT": ["invalid"]}})
    await t.db.commit()
    board = await build_board(t.db, now=NOW)
    assert board["leads"][0]["positions"] is None and board["leads"][0]["orphans"] is None
    assert board["accounts"][0]["equity"] is None
    assert board["accounts"][0]["riskLegacyUnverified"] is True
    assert board["leads"][0]["todayOpenCount"] is None


async def test_board_negative_binding_ownership_is_not_a_reverse_position(board_state):
    # 无效所有权只返回未知，不借负数推导持仓方向。
    t = board_state
    t.allocation.owned_qty_exact = "-1"
    await t.db.commit()
    mine = (await build_board(t.db, now=NOW))["leads"][0]["positions"][0]["mine"][0]
    assert mine["state"] == "unknown" and mine["qty"] is None and mine["pnl"] is None


async def test_board_allocation_for_another_account_is_unknown(board_state):
    # 绑定 ID 相同也不能把另一账户的所有权作为已对齐证据。
    t = board_state
    t.allocation.account_id = t.empty.id
    await t.db.commit()
    mine = (await build_board(t.db, now=NOW))["leads"][0]["positions"][0]["mine"][0]
    assert mine["state"] == "unknown" and "账户不匹配" in mine["reason"]
    assert mine["qty"] is None and mine["entryPrice"] is None and mine["pnl"] is None


async def test_board_foreign_allocation_is_not_claimed_as_my_orphan(board_state):
    # 带单员没有该方向也不能把另一账户的账本误报为我方孤儿持仓。
    t = board_state
    t.db.add(PositionAllocation(binding_id=t.binding.id, account_id=t.empty.id, inst_id="ETHUSDT",
        pos_side="short", owned_qty=2, entry_cost=400))
    await t.db.commit()
    assert (await build_board(t.db, now=NOW))["leads"][0]["orphans"] == []


@pytest.mark.parametrize("field", ["meta", "profile", "openPositionsMeta", "orders", "closed", "openPositions"])
async def test_board_corrupt_nested_snapshot_does_not_break_other_rows(board_state, field):
    # 损坏的历史子字段隔离为未知，不能使整个看板报 500。
    t = board_state
    snapshot = json.loads(t.lead.snapshot_json)
    snapshot[field] = ["corrupt"] if field != "openPositions" else {"corrupt": True}
    t.lead.snapshot_json = json.dumps(snapshot)
    await t.db.commit()
    board = await build_board(t.db, now=NOW)
    assert len(board["accounts"]) == 2 and len(board["leads"]) == 1
    if field == "openPositions":
        assert board["leads"][0]["positions"] is None and board["leads"][0]["orphans"] is None
    if field in ("meta", "openPositionsMeta"):
        assert board["leads"][0]["todayOpenCount"] is None


@pytest.mark.parametrize("invalid", ["symbol", "quantity", "duplicate"])
async def test_board_invalid_authoritative_rows_cannot_prove_orphans(board_state, invalid):
    # 即使带有 authoritative 标记，无效或重复列表也不是空仓/缺仓证明。
    t = board_state
    t.db.add(PositionAllocation(binding_id=t.binding.id, account_id=t.account.id, inst_id="ETHUSDT",
        pos_side="short", owned_qty=2, entry_cost=400))
    snapshot = json.loads(t.lead.snapshot_json)
    if invalid == "symbol":
        snapshot["openPositions"][0]["symbol"] = ["BTCUSDT"]
    elif invalid == "quantity":
        snapshot["openPositions"][0]["qtyExact"] = "NaN"
    else:
        snapshot["openPositions"].append(dict(snapshot["openPositions"][0]))
    t.lead.snapshot_json = json.dumps(snapshot)
    await t.db.commit()
    lead = (await build_board(t.db, now=NOW))["leads"][0]
    assert lead["orphans"] is None and lead["todayOpenCount"] is None
    if invalid == "symbol":
        assert lead["positions"] is None
    else:
        assert all(position["mine"][0]["state"] == "unknown" for position in lead["positions"])


async def test_board_zero_price_placeholders_remain_unknown(board_state):
    # 旧数据中的零成本、零市场价不等于真实零价。
    t = board_state
    t.allocation.entry_cost_exact = "0"
    physical = json.loads(t.account.positions_json)
    physical[0]["markPrice"] = 0
    t.account.positions_json = json.dumps(physical)
    snapshot = json.loads(t.lead.snapshot_json)
    snapshot["openPositions"][0]["avgEntry"] = snapshot["openPositions"][0]["markPrice"] = 0
    t.lead.snapshot_json = json.dumps(snapshot)
    await t.db.commit()
    position = (await build_board(t.db, now=NOW))["leads"][0]["positions"][0]
    assert position["entryPrice"] is None and position["markPrice"] is None
    assert position["mine"][0]["entryPrice"] is None and position["mine"][0]["pnl"] is None
    assert position["takeProfitPrice"] is None and position["stopLossPrice"] is None


async def test_board_finite_inputs_with_overflowing_derived_values_return_null(board_state):
    # 极端损坏输入的中间乘除结果也必须拒绝 Infinity，保持严格 JSON。
    t = board_state
    t.allocation.owned_qty_exact, t.allocation.entry_cost_exact = "1e-308", "1e308"
    risk = json.loads(t.account.risk_equity_json)
    risk["assets"]["USDT"]["equity"] = 1e308
    t.account.risk_equity_json = json.dumps(risk)
    snapshot = json.loads(t.lead.snapshot_json)
    snapshot["openPositions"][0]["qtyExact"] = "1e308"
    t.lead.snapshot_json = json.dumps(snapshot)
    await t.db.commit()
    board = await build_board(t.db, now=NOW)
    mine = board["leads"][0]["positions"][0]["mine"][0]
    assert mine["entryPrice"] is None and mine["pnl"] is None and mine["pnlPct"] is None
    assert mine["expectedQty"] is None
    json.dumps(board, allow_nan=False)


@pytest.mark.parametrize("exact_qty,expected", [("0.5", 0.5), ("NaN", None)])
async def test_board_uses_exact_source_qty_without_invalid_fallback(board_state, exact_qty, expected):
    # 精确字段非法时保持 null，不回退到展示浮点值伪造持仓。
    t = board_state
    snapshot = json.loads(t.lead.snapshot_json)
    snapshot["openPositions"][0]["qtyExact"] = exact_qty
    t.lead.snapshot_json = json.dumps(snapshot)
    await t.db.commit()
    position = (await build_board(t.db, now=NOW))["leads"][0]["positions"][0]
    assert position["qty"] == expected and position["mine"][0]["expectedQty"] == expected


@pytest.mark.parametrize("notional", [110, None])
async def test_board_okx_allocation_contracts_require_persisted_base_conversion(board_state, notional):
    # OKX 张数不能直接当基币数量；缺少持久化名义金额时不猜合约乘数。
    t = board_state
    t.account.exchange = "okx"
    t.account.positions_json = json.dumps([{
        "instId": "BTC-USDT-SWAP", "symbol": "BTCUSDT", "posSide": "long", "pos": 100,
        "avgPx": 100, "markPrice": 110, "notionalUsd": notional, "upl": 10,
    }])
    t.allocation.inst_id = "BTC-USDT-SWAP"
    t.allocation.owned_qty_exact, t.allocation.entry_cost_exact = "100", "10000"
    await t.db.commit()
    position = (await build_board(t.db, now=NOW))["leads"][0]["positions"][0]
    mine = position["mine"][0]
    if notional is None:
        assert mine["state"] == "unknown" and mine["qty"] is None and mine["pnl"] is None
        assert position["takeProfitPrice"] is None and position["stopLossPrice"] is None
    else:
        assert mine["state"] == "aligned" and mine["qty"] == 1 and mine["entryPrice"] == 100
        assert mine["pnl"] == 10 and mine["expectedQty"] == 1


async def test_board_software_trailing_stop_matches_persisted_guard(board_state):
    # 软件止损展示使用同一绑定的峰值与回撤设置，不借带单员价位代替。
    t = board_state
    t.binding.enable_trailing_stop = True
    t.binding.trailing_callback_pct = 2
    t.db.add(PositionGuardState(binding_id=t.binding.id, inst_id="BTCUSDT", pos_side="long",
                               entry_px=100, peak_px=120, trough_px=100))
    await t.db.commit()
    position = (await build_board(t.db, now=NOW))["leads"][0]["positions"][0]
    assert position["takeProfitPrice"] == pytest.approx(110)
    assert position["stopLossPrice"] == pytest.approx(117.6)


@pytest.mark.parametrize("owned,expected_state", [("0", "missing"), ("0.5", "partial"), ("1.03", "aligned")])
async def test_board_mine_states_use_binding_owned_quantity(board_state, owned, expected_state):
    t = board_state
    t.allocation.owned_qty_exact = owned
    t.allocation.entry_cost_exact = str(float(owned) * 100)
    await t.db.commit()
    mine = (await build_board(t.db, now=NOW))["leads"][0]["positions"][0]["mine"][0]
    assert mine["state"] == expected_state
    assert mine["qty"] == float(owned)


async def test_board_alerts_cover_all_required_incidents(board_state):
    t = board_state
    (await t.db.get(SystemConfig, "global_kill_switch")).value_json = "true"
    t.binding.circuit_breaker_tripped, t.binding.circuit_breaker_reason = True, "Test breaker"
    t.lead.status, t.lead.last_error = "error", "Lead offline"
    t.account.status, t.account.last_error = "error", "Account offline"
    risk = json.loads(t.account.risk_equity_json)
    risk["legacy_unverified"] = True
    t.account.risk_equity_json = json.dumps(risk)
    t.db.add(OrderIntent(id="board-intent", binding_id=t.binding.id, account_id=t.account.id,
        inst_id="BTCUSDT", pos_side="long", source_event_id="board-event", state="UNKNOWN",
        requested_qty="1", filled_qty="0.2", avg_px="100", reduce_only=False))
    t.db.add(BindingDelivery(binding_id=t.binding.id, source_event_id="review", source_sequence=NOW_MS,
        payload_json="{}", state="PENDING", exit_progress="REVIEW_REQUIRED"))
    await t.db.commit()
    board = await build_board(t.db, now=NOW)
    codes = {alert["code"] for alert in board["alerts"]}
    assert codes >= {"GLOBAL_KILL_SWITCH", "BINDING_BREAKER", "ORDER_INTENT_UNRESOLVED", "LEAD_SNAPSHOT_STALE",
                     "LEAD_SYNC_FAILED", "ACCOUNT_SYNC_FAILED", "RISK_LEGACY_UNVERIFIED", "DELIVERY_REVIEW_REQUIRED"}
    assert all(set(alert) == {"level", "code", "message", "bindingId", "leadId", "accountId"} for alert in board["alerts"])
    mine = board["leads"][0]["positions"][0]["mine"][0]
    assert mine["state"] == "blocked" and "急停" in mine["reason"]


async def test_board_old_kill_record_does_not_keep_released_switch_blocked(board_state):
    # 当前急停状态优先，历史阻断记录不能永久锁住看板状态。
    t = board_state
    t.db.add(ExecutionRecord(binding_id=t.binding.id, lead_id=t.lead.id, account_id=t.account.id,
        lead_name=t.lead.name, account_name=t.account.name, symbol="BTCUSDT", inst_id="BTCUSDT",
        side="BUY", position_side="LONG", action="OPEN_LONG", status="BLOCKED_KILL_SWITCH",
        details="Global kill switch is active", created_at=NOW))
    await t.db.commit()
    mine = (await build_board(t.db, now=NOW))["leads"][0]["positions"][0]["mine"][0]
    assert mine["state"] == "aligned" and mine["reason"] is None


async def test_board_source_deferred_record_is_unknown_until_confirmed(board_state):
    # 新鲜快照也不能掩盖当前分配缺少出场证明，成功确认后才恢复对齐。
    t = board_state
    record = ExecutionRecord(binding_id=t.binding.id, lead_id=t.lead.id, account_id=t.account.id,
        lead_name=t.lead.name, account_name=t.account.name, symbol="BTCUSDT", inst_id="BTCUSDT",
        side="SELL", position_side="LONG", action="CLOSE_LONG", status="DEFERRED_SOURCE_UNPROVEN",
        details="Source exit lacks allocation opening time", created_at=NOW)
    t.db.add(record)
    await t.db.commit()
    mine = (await build_board(t.db, now=NOW))["leads"][0]["positions"][0]["mine"][0]
    assert mine["state"] == "unknown" and mine["reason"] == record.details
    record.status, record.details = "EXECUTED", "Confirmed"
    await t.db.commit()
    mine = (await build_board(t.db, now=NOW))["leads"][0]["positions"][0]["mine"][0]
    assert mine["state"] == "aligned" and mine["reason"] is None


async def test_board_orphan_and_multi_binding_guards_are_not_fabricated(board_state):
    t = board_state
    second_account = t.empty
    second_account.exchange, second_account.last_sync_at = "binance", NOW
    second_account.positions_json = t.account.positions_json
    second_account.risk_equity_json = t.account.risk_equity_json
    second = CopyBinding(name="Second", lead_id=t.lead.id, account_id=second_account.id,
        tp_sl_mode="independent", enable_take_profit=True, take_profit_pct=20)
    t.db.add(second)
    await t.db.flush()
    t.db.add_all([
        PositionAllocation(binding_id=second.id, account_id=second_account.id, inst_id="BTCUSDT",
                           pos_side="long", owned_qty=1, entry_cost=100),
        PositionAllocation(binding_id=t.binding.id, account_id=t.account.id, inst_id="ETHUSDT",
                           pos_side="short", owned_qty=2, entry_cost=400),
    ])
    await t.db.commit()
    lead = (await build_board(t.db, now=NOW))["leads"][0]
    assert lead["positions"][0]["takeProfitPrice"] is None
    orphan = lead["orphans"][0]
    assert orphan["symbol"] == "ETHUSDT" and orphan["side"] == "SHORT" and orphan["qty"] == 2
    assert orphan["expectedQty"] == 0


async def test_board_events_classify_open_add_reduce_close_and_correlate_execution(board_state):
    t = board_state
    snapshot = json.loads(t.lead.snapshot_json)
    snapshot["openPositions"] = []
    t.lead.snapshot_json = json.dumps(snapshot)
    raw = {}
    for i, (side, qty, pnl) in enumerate([("BUY", 1, 0), ("BUY", 1, 0), ("SELL", 0.5, 5), ("SELL", 1.5, 15)], 1):
        raw[str(i)] = {"sym": "BTCUSDT", "ps": "LONG", "side": side, "q": qty, "p": 100,
                       "t": START_MS + i * 1000, "pnl": pnl, "pnlKnown": True}
    raw["old"] = {"sym": "ETHUSDT", "ps": "LONG", "side": "BUY", "q": 1, "p": 100, "t": START_MS - 1000}
    t.lead.orders_store_json = json.dumps(raw)
    t.db.add(BindingDelivery(binding_id=t.binding.id, source_event_id="4:1.5", source_sequence=START_MS + 4000,
        payload_json=json.dumps({"_source_key": "4"}), state="APPLIED"))
    t.db.add(ExecutionRecord(binding_id=t.binding.id, lead_id=t.lead.id, account_id=t.account.id,
        lead_name=t.lead.name, account_name=t.account.name, symbol="BTCUSDT", inst_id="BTCUSDT",
        side="SELL", position_side="LONG", action="CLOSE_LONG", status="EXECUTED",
        my_qty=1.5, my_price=100, details="Confirmed", delivery_event_id="4:1.5", created_at=NOW))
    await t.db.commit()
    lead = (await build_board(t.db, now=NOW))["leads"][0]
    assert [e["action"] for e in lead["todayEvents"]] == ["CLOSE", "REDUCE", "ADD", "OPEN"]
    assert lead["todayOpenCount"] == lead["todayCloseCount"] == 1
    assert lead["todayRealizedPnl"] == 20
    assert lead["todayEvents"][0]["mine"][0]["details"] == "Confirmed"


@pytest.mark.parametrize("history_complete,known", [(True, True), (False, False)])
async def test_board_reconstructed_snapshot_today_stats_follow_history_completeness(board_state, history_complete, known):
    t = board_state
    snapshot = json.loads(t.lead.snapshot_json)
    snapshot["openPositions"] = []
    snapshot["meta"]["orderHistoryComplete"] = history_complete
    snapshot["profile"]["positionShow"] = False
    snapshot["openPositionsMeta"] = {"reconstructed": True, "derived": True, "authoritative": False,
                                     "complete": False, "positionShow": False, "asOf": NOW_MS}
    t.lead.snapshot_json = json.dumps(snapshot)
    raw = {}
    for i, (side, qty, pnl) in enumerate([("BUY", 1, 0), ("BUY", 1, 0), ("SELL", 0.5, 5), ("SELL", 1.5, 15)], 1):
        raw[str(i)] = {"sym": "BTCUSDT", "ps": "LONG", "side": side, "q": qty, "p": 100,
                       "t": START_MS + i * 1000, "pnl": pnl, "pnlKnown": True}
    raw["old"] = {"sym": "ETHUSDT", "ps": "LONG", "side": "BUY", "q": 1, "p": 100, "t": START_MS - 1000}
    t.lead.orders_store_json = json.dumps(raw)
    await t.db.commit()
    lead = (await build_board(t.db, now=NOW))["leads"][0]
    if known:
        assert [e["action"] for e in lead["todayEvents"]] == ["CLOSE", "REDUCE", "ADD", "OPEN"]
        assert lead["todayOpenCount"] == lead["todayCloseCount"] == 1
        assert lead["todayRealizedPnl"] == 20
    else:
        assert lead["todayEvents"] == []
        assert lead["todayOpenCount"] is None and lead["todayCloseCount"] is None
        assert lead["todayRealizedPnl"] is None


async def test_board_display_snapshot_fallback_does_not_duplicate_deliveries(board_state):
    # 缺失稳定来源键的展示快照不能再与投递兜底重复生成同一笔 OPEN。
    t = board_state
    t.lead.orders_store_json = None
    snapshot = json.loads(t.lead.snapshot_json)
    snapshot["orders"] = [{"symbol": "BTCUSDT", "positionSide": "LONG", "side": "BUY",
                           "qty": 1, "avgPrice": 100, "time": START_MS + 1000}]
    t.lead.snapshot_json = json.dumps(snapshot)
    t.db.add(BindingDelivery(binding_id=t.binding.id, source_event_id="stable-source",
        source_sequence=START_MS + 1000, payload_json=json.dumps({
            "_source_key": "stable-source", "symbol": "BTCUSDT", "positionSide": "LONG",
            "side": "BUY", "qty": 1, "avgPrice": 100,
        }), state="APPLIED"))
    await t.db.commit()
    lead = (await build_board(t.db, now=NOW))["leads"][0]
    assert len(lead["todayEvents"]) == 1 and lead["todayOpenCount"] == 1


async def test_board_multiple_delivery_deltas_do_not_invent_cumulative_source_pnl(board_state):
    # 同一来源的多次部分成交没有可证明的收益差分，不拿首笔累计收益代表全部。
    t = board_state
    snapshot = json.loads(t.lead.snapshot_json)
    snapshot["openPositions"] = []
    t.lead.snapshot_json = json.dumps(snapshot)
    t.lead.orders_store_json = json.dumps({"old": {
        "sym": "ETHUSDT", "ps": "LONG", "side": "BUY", "q": 1, "p": 100, "t": START_MS - 1000,
    }})
    for i, (pnl, known) in enumerate([(5, True), (None, False)], 1):
        t.db.add(BindingDelivery(binding_id=t.binding.id, source_event_id=f"partial-{i}",
            source_sequence=START_MS + i * 1000, payload_json=json.dumps({
                "_source_key": "cumulative-source", "symbol": "BTCUSDT", "positionSide": "LONG",
                "side": "SELL", "qty": 0.5, "avgPrice": 100, "totalPnl": pnl, "pnlKnown": known,
            }), state="APPLIED"))
    await t.db.commit()
    lead = (await build_board(t.db, now=NOW))["leads"][0]
    assert lead["todayEvents"] == [] and lead["todayRealizedPnl"] is None
    assert lead["todayOpenCount"] is None and lead["todayCloseCount"] is None


async def test_board_event_after_position_observation_is_not_classified_from_old_qty(board_state):
    # 缺少成交后的观察时，不把旧数量当成本笔成交后的持仓。
    t = board_state
    snapshot = json.loads(t.lead.snapshot_json)
    snapshot["openPositionsMeta"]["asOf"] = NOW_MS - 1000
    t.lead.snapshot_json = json.dumps(snapshot)
    t.lead.orders_store_json = json.dumps({"new": {"sym": "BTCUSDT", "ps": "LONG", "side": "BUY",
        "q": 1, "p": 100, "t": NOW_MS}})
    await t.db.commit()
    lead = (await build_board(t.db, now=NOW))["leads"][0]
    assert lead["todayEvents"] == [] and lead["todayOpenCount"] is None and lead["todayCloseCount"] is None


async def test_board_discontinuous_history_does_not_fabricate_a_full_close(board_state):
    # 已知空仓不代表旧 SELL 是最后一笔，漏单历史缺少可靠动作与日收益。
    t = board_state
    snapshot = json.loads(t.lead.snapshot_json)
    snapshot["openPositions"] = []
    snapshot["meta"]["orderHistoryComplete"] = False
    t.lead.snapshot_json = json.dumps(snapshot)
    t.lead.orders_store_json = json.dumps({
        "old": {"sym": "ETHUSDT", "ps": "LONG", "side": "BUY", "q": 1, "p": 100, "t": START_MS - 1000},
        "exit": {"sym": "BTCUSDT", "ps": "LONG", "side": "SELL", "q": 1, "p": 100,
                 "t": START_MS + 1000, "pnl": 0, "pnlKnown": True},
    })
    await t.db.commit()
    lead = (await build_board(t.db, now=NOW))["leads"][0]
    assert lead["todayEvents"] == []
    assert lead["todayOpenCount"] is None and lead["todayCloseCount"] is None
    assert lead["todayRealizedPnl"] is None


async def test_board_refreshed_closed_cycle_is_counted_once(board_state):
    # 同一闭仓 ID 的后续刷新不能生成第二个 CLOSE 事件。
    t = board_state
    old = {"id": "cycle", "symbol": "ETHUSDT", "side": "SHORT", "opened": START_MS - 1000,
           "closed": START_MS + 1000, "pnl": 3}
    t.lead.closed_cache_json = json.dumps([old])
    snapshot = json.loads(t.lead.snapshot_json)
    snapshot["closed"] = [{**old, "closed": START_MS + 2000, "pnl": 4}]
    t.lead.snapshot_json = json.dumps(snapshot)
    await t.db.commit()
    events = (await build_board(t.db, now=NOW))["leads"][0]["todayEvents"]
    closes = [event for event in events if event["action"] == "CLOSE"]
    assert len(closes) == 1 and closes[0]["time"] == START_MS + 2000 and closes[0]["realizedPnl"] == 4


async def test_board_daily_events_are_bounded_and_counts_use_full_day(board_state):
    # 展示只取最后 100 条，日内次数仍基于完整已持久化来源历史。
    t = board_state
    snapshot = json.loads(t.lead.snapshot_json)
    snapshot["openPositions"][0]["qty"] = 150
    t.lead.snapshot_json = json.dumps(snapshot)
    t.lead.orders_store_json = json.dumps({
        str(i): {"sym": "BTCUSDT", "ps": "LONG", "side": "BUY", "q": 1, "p": 100,
                 "t": START_MS + i * 1000} for i in range(1, 151)
    })
    await t.db.commit()
    lead = (await build_board(t.db, now=NOW))["leads"][0]
    assert len(lead["todayEvents"]) == 100 and lead["todayOpenCount"] == 1
    assert lead["todayEvents"][0]["time"] == START_MS + 150000
    assert lead["todayEvents"][-1]["time"] == START_MS + 51000


@pytest.mark.parametrize("path", ["/api/dashboard/board", "/api/dashboard/intents", "/api/dashboard/executions"])
@pytest.mark.parametrize("role,status", [("admin", 200), ("viewer", 403), ("anonymous", 401)])
async def test_dashboard_contract_endpoints_require_admin(board_state, path, role, status):
    t = board_state
    t.client.headers.pop("Authorization", None)
    headers = {"Authorization": f"Bearer {t.tokens[role]}"} if role != "anonymous" else {}
    assert (await t.client.get(path, headers=headers)).status_code == status


async def test_intents_filter_camel_case_and_execution_pagination(board_state):
    t = board_state
    for i, state in enumerate(("UNKNOWN", "SUBMITTING", "FILLED")):
        t.db.add(OrderIntent(id=f"intent-{i}", binding_id=t.binding.id, account_id=t.account.id,
            inst_id="BTCUSDT", pos_side="long", source_event_id=f"source-{i}", state=state,
            requested_qty="2.5", filled_qty="0.5", avg_px="100", reduce_only=True, created_at=NOW))
        t.db.add(ExecutionRecord(binding_id=t.binding.id, lead_id=t.lead.id, account_id=t.account.id,
            lead_name=t.lead.name, account_name=t.account.name, symbol="BTCUSDT", inst_id="BTCUSDT",
            side="BUY", position_side="LONG", action="OPEN_LONG", status="FAILED", details=f"Detail {i}",
            created_at=NOW + timedelta(seconds=i)))
    await t.db.commit()
    items = (await t.client.get("/api/dashboard/intents?state=UNKNOWN,SUBMITTING")).json()["items"]
    assert {item["state"] for item in items} == {"UNKNOWN", "SUBMITTING"}
    assert all(item["requestedQty"] == 2.5 and item["filledQty"] == 0.5 and item["createdAt"] == NOW_MS for item in items)
    assert set(items[0]) == {"id", "bindingId", "bindingName", "accountId", "accountName", "instId",
                            "posSide", "state", "requestedQty", "filledQty", "exchangeOrderId",
                            "reduceOnly", "createdAt", "sourceEventId"}
    assert (await t.client.get("/api/dashboard/intents?state=INVALID")).status_code == 422
    result = (await t.client.get(f"/api/dashboard/executions?binding_id={t.binding.id}&status=FAILED&limit=1&offset=1")).json()
    assert result["total"] == 3 and len(result["items"]) == 1 and result["items"][0]["details"] == "Detail 1"


async def test_account_and_lead_put_support_name_enabled_proxy_without_network(board_state):
    t = board_state
    for resource, row in (("accounts", t.account), ("leads", t.lead)):
        response = await t.client.put(f"/api/{resource}/{row.id}",
            json={"name": "Renamed", "isEnabled": False, "proxyId": t.proxy.id})
        assert response.status_code == 200
        item = response.json()["item"]
        assert item["name"] == "Renamed" and item["isEnabled"] is False and item["proxyId"] == t.proxy.id
        response = await t.client.put(f"/api/{resource}/{row.id}", json={"isEnabled": True, "proxyId": None})
        assert response.status_code == 200 and response.json()["item"]["isEnabled"] is True
        assert response.json()["item"]["proxyId"] is None
        assert (await t.client.put(f"/api/{resource}/{row.id}", json={"isEnabled": None})).status_code == 422


@pytest.mark.parametrize("resource", ["accounts", "leads"])
@pytest.mark.parametrize("role,status", [("admin", 200), ("viewer", 403), ("anonymous", 401)])
async def test_account_and_lead_put_require_admin(board_state, resource, role, status):
    # 编辑开关、名称和出口的接口不能对只读角色放行。
    t = board_state
    row = t.account if resource == "accounts" else t.lead
    t.client.headers.pop("Authorization", None)
    headers = {"Authorization": f"Bearer {t.tokens[role]}"} if role != "anonymous" else {}
    response = await t.client.put(f"/api/{resource}/{row.id}", json={"name": "Renamed"}, headers=headers)
    assert response.status_code == status


async def test_csv_filename_and_formula_fields_are_safe(board_state):
    t = board_state
    t.lead.snapshot_json = json.dumps({"orders": [{"symbol": "=HYPERLINK(\"bad\")", "side": "+CMD",
        "positionSide": "LONG", "qty": -2, "avgPrice": 100, "pnl": -10}]})
    await t.db.commit()
    response = await t.client.get(f"/api/leads/{t.lead.id}/export-csv")
    assert response.status_code == 200
    assert "filename*=UTF-8''" in response.headers["Content-Disposition"]
    assert "交易员_orders.csv" in unquote(response.headers["Content-Disposition"])
    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[1][2].startswith("'=") and rows[1][3].startswith("'+")
    assert rows[1][7] == "-2" and rows[1][9] == "-10"  # 真实数值不被当成公式。


async def test_board_reconstructed_positions_with_complete_history_are_confirmed(board_state):
    # 隐藏仓位带单员：订单历史完整 + 重构标记 + 新鲜快照，持仓证据即确认，状态按数量推导，孤儿仓可判定。
    t = board_state
    t.db.add(PositionAllocation(binding_id=t.binding.id, account_id=t.account.id, inst_id="ETHUSDT",
        pos_side="short", owned_qty=2, entry_cost=400))
    snapshot = json.loads(t.lead.snapshot_json)
    snapshot["profile"]["positionShow"] = False
    snapshot["openPositionsMeta"] = {"source": "derived-from-order-history", "reconstructed": True,
                                     "derived": True, "positionShow": False, "asOf": NOW_MS,
                                     "complete": False, "authoritative": False}
    t.lead.snapshot_json = json.dumps(snapshot)
    await t.db.commit()
    lead = (await build_board(t.db, now=NOW))["leads"][0]
    assert lead["positions"][0]["mine"][0]["state"] == "aligned"
    assert lead["orphans"] is not None and len(lead["orphans"]) == 1


@pytest.mark.parametrize("breakdown", ["no-reconstructed-flag", "incomplete-history", "stale"])
async def test_board_reconstructed_positions_without_full_evidence_stay_unknown(board_state, breakdown):
    # 重构证据缺任一环（无重构标记、订单历史不完整、快照过期），仍判未知，不伪装对齐。
    t = board_state
    snapshot = json.loads(t.lead.snapshot_json)
    snapshot["profile"]["positionShow"] = False
    snapshot["openPositionsMeta"] = {"reconstructed": True, "asOf": NOW_MS, "complete": False, "authoritative": False}
    if breakdown == "no-reconstructed-flag":
        snapshot["openPositionsMeta"]["reconstructed"] = False
    elif breakdown == "incomplete-history":
        snapshot["meta"]["orderHistoryComplete"] = False
    else:
        snapshot["openPositionsMeta"]["asOf"] = START_MS
    t.lead.snapshot_json = json.dumps(snapshot)
    await t.db.commit()
    lead = (await build_board(t.db, now=NOW))["leads"][0]
    assert lead["positions"][0]["mine"][0]["state"] == "unknown"
    assert lead["orphans"] is None
