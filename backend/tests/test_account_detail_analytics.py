"""Account detail snapshots, attribution, PnL reconciliation, TWR curves, protection, and alerts tests."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import Base
from app.models.domain import (
    AccountAlertState,
    AccountCashflowEntry,
    AccountDatasetSyncState,
    AccountEquitySnapshot,
    AccountOpenOrderSnapshot,
    AccountOrderHistory,
    AccountTradeFill,
    CopyBinding,
    ExecutionRecord,
    LeadSource,
    OrderIntent,
    PositionAllocation,
    TradingAccount,
)
from app.services.adapters.base import BalanceInfo, ExchangePosition
from app.services.account_detail import (
    capture_equity_snapshot,
    compute_data_quality_summary,
    compute_equity_curve,
    compute_performance,
    compute_pnl_breakdown,
    compute_positions_and_protection,
    compute_risk_overview,
    downsample_equity_snapshots,
    evaluate_account_alerts,
    persist_account_alerts,
    reconcile_attribution,
)

NOW_MS = 1_795_700_000_000
DAY_MS = 86_400_000


async def _make_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


def _base_account(acct_id: int = 1) -> TradingAccount:
    return TradingAccount(
        id=acct_id,
        name="BN Follower",
        exchange="binance",
        environment="live",
        encrypted_credentials="enc",
        api_key_masked="****1234",
        is_enabled=True,
        margin_mode="cross",
        position_mode="hedge",
        usdt_equity=1000.0,
        avail_equity=800.0,
        hwm_equity=1000.0,
        day_start_equity=1000.0,
        day_start_date="2026-11-26",
        status="connected",
        last_sync_at=datetime.fromtimestamp(NOW_MS / 1000, tz=timezone.utc),
        positions_json="[]",
    )


@pytest.mark.asyncio
async def test_snapshot_bucket_upsert_skew_quality_and_idempotent_downsampling():
    engine, factory = await _make_session()
    try:
        async with factory() as db:
            acct = _base_account()
            db.add(acct)
            await db.commit()

            bal = BalanceInfo(1050.0, 900.0, 1020.0, 30.0, "USDT")
            pos = [
                ExchangePosition(
                    inst_id="BTCUSDT", symbol="BTCUSDT", pos_side="long", mgn_mode="cross",
                    pos=0.1, avail_pos=0.1, avg_px=65000, upl=30.0, upl_ratio=0.05,
                    notional_usd=6500.0, lever=10, cur_px=65300,
                    initial_margin=650.0, maintenance_margin=32.5,
                )
            ]
            # Same 60s bucket updates equity_min/max and marks ok
            s1 = await capture_equity_snapshot(db, acct, bal, pos, balance_as_of_ms=NOW_MS, positions_as_of_ms=NOW_MS, now_ms=NOW_MS)
            bal2 = BalanceInfo(990.0, 840.0, 1020.0, -30.0, "USDT")
            s2 = await capture_equity_snapshot(db, acct, bal2, pos, balance_as_of_ms=NOW_MS + 10_000, positions_as_of_ms=NOW_MS + 10_000, now_ms=NOW_MS + 10_000)
            assert s1[0].id == s2[0].id
            assert s2[0].equity == Decimal("990.0")
            assert s2[0].equity_min == Decimal("990.0")
            assert s2[0].equity_max == Decimal("1050.0")
            assert s2[0].data_quality == "ok"

            # Skew > 30s -> partial quality
            s_skew = await capture_equity_snapshot(
                db, acct, bal, pos, balance_as_of_ms=NOW_MS + 60_000, positions_as_of_ms=NOW_MS + 120_000, now_ms=NOW_MS + 120_000
            )
            assert s_skew[0].data_quality == "partial"

            # Old >95d raw snapshots downsample into hour & day buckets before raw deletion
            old_ts = NOW_MS - 95 * DAY_MS
            for i in range(3):
                db.add(
                    AccountEquitySnapshot(
                        account_id=1, currency="USDT", captured_at_ms=old_ts + i * 60_000,
                        bucket_start_ms=((old_ts + i * 60_000) // 60_000) * 60_000,
                        resolution="raw", source="account_sync",
                        equity=Decimal(str(800 + i * 25)), equity_min=Decimal("800"), equity_max=Decimal("850"),
                        data_quality="ok",
                    )
                )
            await db.commit()

            stats1 = await downsample_equity_snapshots(db, now_ms=NOW_MS)
            stats2 = await downsample_equity_snapshots(db, now_ms=NOW_MS)
            await db.commit()
            assert stats1["deleted_raw"] == 3
            assert stats2["deleted_raw"] == 0
            day_rows = list(
                (
                    await db.scalars(
                        select(AccountEquitySnapshot)
                        .where(AccountEquitySnapshot.account_id == 1, AccountEquitySnapshot.resolution == "day")
                        .order_by(AccountEquitySnapshot.bucket_start_ms.asc())
                    )
                ).all()
            )
            assert len(day_rows) >= 2
            assert day_rows[0].equity_max == Decimal("850")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pnl_reconciliation_twr_drawdown_ignores_deposit_and_non_usdt_isolated():
    engine, factory = await _make_session()
    try:
        async with factory() as db:
            acct = _base_account()
            db.add(acct)
            t0 = NOW_MS - 4 * DAY_MS
            t1 = NOW_MS - 3 * DAY_MS
            t2 = NOW_MS - 2 * DAY_MS
            t3 = NOW_MS - 1 * DAY_MS

            # Equity: 1000 -> drops to 800 (-20% return, 20% DD) -> deposit +1000 -> 1800 -> gain +180 -> 1980 (+10% post deposit)
            for ts, eq, upl in [
                (t0, "1000", "0"),
                (t1, "800", "0"),
                (t2, "1800", "0"),
                (t3, "1980", "20"),
            ]:
                db.add(
                    AccountEquitySnapshot(
                        account_id=1, currency="USDT", captured_at_ms=ts,
                        bucket_start_ms=(ts // 3_600_000) * 3_600_000,
                        resolution="hour", source="account_sync",
                        equity=Decimal(eq), equity_min=Decimal(eq), equity_max=Decimal(eq),
                        unrealized_pnl=Decimal(upl), data_quality="ok",
                    )
                )

            db.add_all([
                AccountDatasetSyncState(account_id=1, dataset="cashflow", status="ok", coverage_start_ms=t0 - DAY_MS, coverage_end_ms=NOW_MS, last_success_at_ms=NOW_MS),
                AccountDatasetSyncState(account_id=1, dataset="fills", status="ok", coverage_start_ms=t0 - DAY_MS, coverage_end_ms=NOW_MS, last_success_at_ms=NOW_MS),
                AccountCashflowEntry(account_id=1, exchange="binance", external_id="c1", dedupe_key="d1", asset="USDT", raw_type="TRANSFER", category="TRANSFER_IN", signed_amount=Decimal("1000"), occurred_at_ms=t2 - 10_000),
                AccountCashflowEntry(account_id=1, exchange="binance", external_id="c2", dedupe_key="d2", asset="USDT", raw_type="REALIZED_PNL", category="REALIZED_PNL", signed_amount=Decimal("-30"), occurred_at_ms=t3 - 10_000),
                AccountCashflowEntry(account_id=1, exchange="binance", external_id="c3", dedupe_key="d3", asset="USDT", raw_type="FUNDING_FEE", category="FUNDING_FEE", signed_amount=Decimal("-5"), occurred_at_ms=t3 - 8_000),
                AccountCashflowEntry(account_id=1, exchange="binance", external_id="c4", dedupe_key="d4", asset="USDT", raw_type="COMMISSION", category="COMMISSION", signed_amount=Decimal("-5"), occurred_at_ms=t3 - 6_000),
                # USDC flow must NEVER merge 1:1 into USDT PnL
                AccountCashflowEntry(account_id=1, exchange="binance", external_id="c5", dedupe_key="d5", asset="USDC", raw_type="REALIZED_PNL", category="REALIZED_PNL", signed_amount=Decimal("500"), occurred_at_ms=t3 - 4_000),
            ])
            await db.commit()

            curve = await compute_equity_curve(db, 1, "30d", NOW_MS)
            # Peak DD remained 20% at t1, NOT erased by the 1000 deposit at t2
            assert curve["maxDrawdownPct"] == pytest.approx(20.0)
            assert curve["points"][-1]["twrIndex"] == pytest.approx(0.88)
            assert curve["points"][-1]["cumulativeTradingPnl"] == pytest.approx(-20.0)  # 1980 - 1000 - 1000

            sync_states = list((await db.scalars(select(AccountDatasetSyncState))).all())
            pnl = await compute_pnl_breakdown(db, acct, "30d", NOW_MS, sync_states)
            assert pnl["tradingNetPnl"] == pytest.approx(-20.0)
            # ledgerNetPnl = -30 (realized) - 5 (funding) - 5 (comm) + (20 - 0 upl) = -20.0
            assert pnl["ledgerNetPnl"] == pytest.approx(-20.0)
            assert pnl["reconciled"] is True
            assert len(pnl["otherAssetCashflows"]) == 1 and pnl["otherAssetCashflows"][0]["asset"] == "USDC"

            # Without full cashflow coverage, tradingNetPnl must stay None (never 0)
            cf_state = next(s for s in sync_states if s.dataset == "cashflow")
            cf_state.coverage_start_ms = t3
            pnl_partial = await compute_pnl_breakdown(db, acct, "30d", NOW_MS, sync_states)
            assert pnl_partial["tradingNetPnl"] is None
            assert pnl_partial["ledgerNetPnl"] is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_attribution_fee_single_fill_id_and_cycle_win_rate():
    engine, factory = await _make_session()
    try:
        async with factory() as db:
            acct = _base_account()
            lead = LeadSource(id=1, name="Alpha", platform="binance", portfolio_id="p1")
            binding = CopyBinding(id=1, name="Bind1", lead_id=1, account_id=1)
            db.add_all([acct, lead, binding])
            await db.commit()

            intent1 = OrderIntent(id="int_1", binding_id=1, account_id=1, inst_id="BTCUSDT", pos_side="long", source_event_id="ev1", state="FILLED", requested_qty="0.2", reduce_only=False, exchange_order_id="ex_o1", source_time_ms=NOW_MS - 10_000)
            intent2 = OrderIntent(id="int_2", binding_id=1, account_id=1, inst_id="BTCUSDT", pos_side="long", source_event_id="ev2", state="FILLED", requested_qty="0.2", reduce_only=True, exchange_order_id="ex_o2", source_time_ms=NOW_MS - 5_000)
            exec1 = ExecutionRecord(id=1, binding_id=1, lead_id=1, account_id=1, lead_name="Alpha", account_name="BN", symbol="BTCUSDT", inst_id="BTCUSDT", side="buy", position_side="long", action="OPEN", status="EXECUTED", my_qty=0.2, order_id="ex_o1", slippage_pct=0.05, spread_pct=0.02, created_at=datetime.fromtimestamp((NOW_MS - 9_500) / 1000, tz=timezone.utc))
            exec2 = ExecutionRecord(id=2, binding_id=1, lead_id=1, account_id=1, lead_name="Alpha", account_name="BN", symbol="BTCUSDT", inst_id="BTCUSDT", side="sell", position_side="long", action="CLOSE", status="EXECUTED", my_qty=0.2, order_id="ex_o2", slippage_pct=0.10, spread_pct=None, created_at=datetime.fromtimestamp((NOW_MS - 4_000) / 1000, tz=timezone.utc))
            db.add_all([intent1, intent2, exec1, exec2])

            db.add(AccountDatasetSyncState(account_id=1, dataset="fills", status="ok", coverage_start_ms=NOW_MS - DAY_MS, coverage_end_ms=NOW_MS))
            db.add(AccountDatasetSyncState(account_id=1, dataset="orders", status="ok", coverage_start_ms=NOW_MS - DAY_MS, coverage_end_ms=NOW_MS))

            # Single fill for ex_o1 -> exchange_fill_id written cleanly; multi-fill for ex_o2 -> fee summed, exchange_fill_id stays None
            db.add_all([
                AccountTradeFill(account_id=1, exchange="binance", exchange_fill_id="f1", exchange_order_id="ex_o1", symbol="BTCUSDT", inst_id="BTCUSDT", side="buy", position_side="long", qty=Decimal("0.2"), price=Decimal("60000"), realized_pnl=Decimal("0"), commission_amount=Decimal("2.4"), commission_asset="USDT", maker=False, occurred_at_ms=NOW_MS - 9_000),
                AccountTradeFill(account_id=1, exchange="binance", exchange_fill_id="f2a", exchange_order_id="ex_o2", symbol="BTCUSDT", inst_id="BTCUSDT", side="sell", position_side="long", qty=Decimal("0.1"), price=Decimal("61000"), realized_pnl=Decimal("100"), commission_amount=Decimal("1.2"), commission_asset="USDT", maker=True, occurred_at_ms=NOW_MS - 4_500),
                AccountTradeFill(account_id=1, exchange="binance", exchange_fill_id="f2b", exchange_order_id="ex_o2", symbol="BTCUSDT", inst_id="BTCUSDT", side="sell", position_side="long", qty=Decimal("0.1"), price=Decimal("61000"), realized_pnl=Decimal("100"), commission_amount=Decimal("1.3"), commission_asset="USDT", maker=True, occurred_at_ms=NOW_MS - 4_400),
                # Manual fill outside 120s grace window
                AccountTradeFill(account_id=1, exchange="binance", exchange_fill_id="fm1", exchange_order_id="man_1", symbol="ETHUSDT", inst_id="ETHUSDT", side="sell", position_side="long", qty=Decimal("1"), price=Decimal("3000"), realized_pnl=Decimal("50"), commission_amount=Decimal("0.01"), commission_asset="BNB", maker=False, occurred_at_ms=NOW_MS - 300_000),
            ])
            await db.commit()

            res = await reconcile_attribution(db, 1, now_ms=NOW_MS)
            await db.commit()
            assert res["conflicts"] == []

            await db.refresh(exec1)
            await db.refresh(exec2)
            assert exec1.fee == pytest.approx(2.4)
            assert exec1.exchange_fill_id == "f1"
            assert exec1.funding is None
            assert exec2.fee == pytest.approx(2.5)
            assert exec2.exchange_fill_id is None  # Multi-fill never uses first fill id

            man_fill = await db.scalar(select(AccountTradeFill).where(AccountTradeFill.exchange_fill_id == "fm1"))
            assert man_fill.source_class == "manual"

            perf = await compute_performance(db, 1, "30d", NOW_MS)
            # ETHUSDT closed before open (incomplete cycle) must be excluded from winRate; BTCUSDT closed with +200 - 4.9 = +195.1
            assert perf["tradeSummary"]["incompleteCycleCount"] == 1
            assert perf["tradeSummary"]["closedCycleCount"] == 1
            assert perf["tradeSummary"]["winRatePct"] == pytest.approx(100.0)
            assert perf["executionQuality"]["spreadSampleCount"] == 1
            assert perf["executionQuality"]["avgSpreadPct"] == pytest.approx(0.02)
            assert "BNB" in perf["tradeSummary"]["nonUsdtCommissions"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_position_ownership_blacklist_protection_and_alert_lifecycle():
    engine, factory = await _make_session()
    try:
        async with factory() as db:
            acct = _base_account()
            acct.positions_json = json.dumps([
                {"instId": "BTCUSDT", "symbol": "BTCUSDT", "posSide": "long", "mgnMode": "cross", "pos": 0.2, "avgPx": 68000, "curPx": 70000, "liquidationPrice": 66500, "notionalUsd": 14000, "upl": 400, "lever": 10},
                {"instId": "DOGEUSDT", "symbol": "DOGEUSDT", "posSide": "long", "mgnMode": "cross", "pos": 1000, "avgPx": 0.2, "curPx": 0.2, "liquidationPrice": 0.05, "notionalUsd": 200, "upl": 0, "lever": 5},
                {"instId": "ETHUSDT", "symbol": "ETHUSDT", "posSide": "short", "mgnMode": "cross", "pos": 1.0, "avgPx": 3000, "curPx": 3000, "liquidationPrice": 3500, "notionalUsd": 3000, "upl": 0, "lever": 5},
            ])
            lead = LeadSource(id=1, name="Alpha", platform="binance", portfolio_id="p1")
            binding = CopyBinding(
                id=1, name="Bind1", lead_id=1, account_id=1, is_enabled=True,
                symbol_blacklist_json=json.dumps(["DOGEUSDT"]),
                enable_stop_loss=True,
            )
            alloc = PositionAllocation(id=1, binding_id=1, account_id=1, inst_id="BTCUSDT", pos_side="long", owned_qty=0.2, owned_qty_exact="0.2", entry_cost=13600)
            oo_snap = AccountOpenOrderSnapshot(
                account_id=1, fetched_at_ms=NOW_MS - 10_000, complete=True, order_count=1,
                orders_json=json.dumps([
                    {"orderId": "sl1", "algoId": "alg1", "category": "stop_loss", "symbol": "BTCUSDT", "instId": "BTCUSDT", "side": "sell", "positionSide": "long", "orderType": "STOP_MARKET", "status": "NEW", "qty": "0.2", "triggerPrice": "66000", "closePosition": False}
                ]),
            )
            db.add_all([acct, lead, binding, alloc, oo_snap])
            db.add(AccountDatasetSyncState(account_id=1, dataset="open_orders", status="ok", last_success_at_ms=NOW_MS - 10_000))
            await db.commit()

            pos_sum = compute_positions_and_protection(
                acct, [alloc], [binding], {1: lead}, [], oo_snap, [], [], [], [],
                list((await db.scalars(select(AccountDatasetSyncState))).all()), NOW_MS,
            )
            by_sym = {p["symbol"]: p for p in pos_sum["positions"]}
            assert by_sym["BTCUSDT"]["ownership"] == "system"
            assert by_sym["BTCUSDT"]["protection"]["status"] == "protected"
            assert by_sym["BTCUSDT"]["liqDistancePct"] == pytest.approx(5.0)
            # DOGEUSDT is blacklisted -> manual/unallocated, NOT flagged as orphan_risk
            assert by_sym["DOGEUSDT"]["unallocatedClass"] == "unallocated"
            # ETHUSDT is covered by binding with 0 allocation -> orphan_risk
            assert by_sym["ETHUSDT"]["unallocatedClass"] == "orphan_risk"

            # Stale open orders must flip protection status to unknown (never unprotected)
            oo_snap.fetched_at_ms = NOW_MS - 300_000
            pos_stale = compute_positions_and_protection(
                acct, [alloc], [binding], {1: lead}, [], oo_snap, [], [], [], [],
                list((await db.scalars(select(AccountDatasetSyncState))).all()), NOW_MS,
            )
            assert {p["symbol"]: p["protection"]["status"] for p in pos_stale["positions"]}["ETHUSDT"] == "unknown"

            # Alert lifecycle: persist creates active alert, resolves when condition clears
            oo_snap.fetched_at_ms = NOW_MS - 10_000
            await db.commit()
            active = await persist_account_alerts(db, 1, now_ms=NOW_MS)
            await db.commit()
            assert any(a["code"] == "ORPHAN_POSITION_RISK" for a in active)

            # Clear ETHUSDT position -> ORPHAN_POSITION_RISK transitions to resolved
            acct.positions_json = json.dumps([json.loads(acct.positions_json)[0]])
            await persist_account_alerts(db, 1, now_ms=NOW_MS + 60_000)
            await db.commit()
            resolved_row = await db.scalar(select(AccountAlertState).where(AccountAlertState.account_id == 1, AccountAlertState.code == "ORPHAN_POSITION_RISK"))
            assert resolved_row.status == "resolved" and resolved_row.resolved_at_ms == NOW_MS + 60_000
    finally:
        await engine.dispose()
