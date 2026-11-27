"""API RBAC/isolation, pagination, redaction, coalesced sync, test-balance non-snapshot, orchestrator fault-isolation, and legacy SQLite migration tests."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import main
from app.core.config import get_settings
from app.core.database import (
    _apply_sqlite_migrations,
    get_session_factory,
    init_db,
    reset_db_engine,
    reset_engine_for_tests,
)
from app.core.security import CredentialVault, create_access_token
from app.models.domain import (
    AccountCashflowEntry,
    AccountDatasetSyncState,
    AccountEquitySnapshot,
    AccountOpenOrderSnapshot,
    AccountOrderHistory,
    AccountTradeFill,
    CopyBinding,
    ExecutionRecord,
    LeadSource,
    PositionAllocation,
    ProxyNode,
    TradingAccount,
    User,
)
from app.services.adapters.base import (
    AccountRiskInfo,
    BalanceInfo,
    CashflowEntry,
    DatasetCapability,
    DatasetPage,
    ExchangeAPIError,
    ExchangePosition,
)
from app.services.account_detail import sync_account_detail
from app.services.engine.orchestrator import EngineOrchestrator

NOW_MS = 1_795_700_000_000


async def _setup_test_app(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_file = data_dir / "mq_test.db"
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-at-least-32-chars-long")
    get_settings.cache_clear()
    await reset_db_engine()
    reset_engine_for_tests(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    await init_db()
    return main.create_app()


@pytest.mark.asyncio
async def test_api_rbac_soft_delete_binding_isolation_pagination_and_redaction(tmp_path, monkeypatch):
    app = await _setup_test_app(tmp_path, monkeypatch)
    try:
        factory = get_session_factory()
        async with factory() as db:
            admin = User(id=1, username="admin_u", password_hash="h", role="admin")
            alice = User(id=2, username="alice", password_hash="h", role="user")
            bob = User(id=3, username="bob", password_hash="h", role="user")
            lead = LeadSource(id=1, name="LeadOne", platform="binance", portfolio_id="p100")
            proxy = ProxyNode(id=1, name="p1", url="http://127.0.0.1:7890", is_enabled=True, is_healthy=True)
            vault = CredentialVault()
            creds = vault.encrypt_dict({"api_key": "REAL_API_KEY_SECRET", "secret_key": "REAL_SECRET"})

            acct_alice = TradingAccount(
                id=10, owner_user_id=2, name="AliceAcct", exchange="binance", environment="live",
                encrypted_credentials=creds, api_key_masked="REAL****CRET",
                proxy_id=1, is_enabled=True, margin_mode="cross", position_mode="hedge",
                usdt_equity=2500.0, avail_equity=2000.0, status="error",
                last_error="Proxy error via http://proxyuser:proxypass@10.0.0.1:8080/api?api_key=leak",
                last_sync_at=datetime.fromtimestamp(NOW_MS / 1000, tz=timezone.utc),
                positions_json="[]",
            )
            acct_bob = TradingAccount(
                id=11, owner_user_id=3, name="BobAcct", exchange="binance", environment="demo",
                encrypted_credentials=creds, api_key_masked="REAL****CRET", status="connected",
            )
            acct_deleted = TradingAccount(
                id=12, owner_user_id=2, name="DeletedAcct", exchange="binance", environment="demo",
                encrypted_credentials=creds, api_key_masked="REAL****CRET", status="idle",
                deleted_at=datetime.now(timezone.utc),
            )
            bind_alice = CopyBinding(id=20, owner_user_id=2, name="AliceBind", lead_id=1, account_id=10)
            bind_bob = CopyBinding(id=21, owner_user_id=3, name="BobBind", lead_id=1, account_id=11)

            db.add_all([admin, alice, bob, lead, proxy])
            await db.flush()
            db.add_all([acct_alice, acct_bob, acct_deleted])
            await db.flush()
            db.add_all([bind_alice, bind_bob])
            await db.commit()

            # Add ledger rows to acct_alice (multi-page cashflows + fills/executions/orders) & deleted account
            for i in range(5):
                db.add(
                    AccountCashflowEntry(
                        account_id=10, exchange="binance", external_id=f"ext-{i}", dedupe_key=f"cf-{i}",
                        asset="USDT", raw_type="REALIZED_PNL", category="REALIZED_PNL",
                        signed_amount=Decimal(str(10 + i)), occurred_at_ms=NOW_MS - i * 1000,
                        metadata_json=json.dumps({"api_key": "SECRET_LEAK", "info": "ok"}),
                    )
                )
            db.add_all([
                AccountTradeFill(account_id=10, exchange="binance", exchange_fill_id="fill-a1", symbol="BTCUSDT", inst_id="BTCUSDT", side="buy", position_side="long", qty=Decimal("0.1"), price=Decimal("60000"), realized_pnl=Decimal("20"), commission_amount=Decimal("1"), commission_asset="USDT", maker=False, occurred_at_ms=NOW_MS - 1000, source_class="system", binding_id=20),
                ExecutionRecord(id=50, binding_id=20, lead_id=1, account_id=10, lead_name="LeadOne", account_name="AliceAcct", symbol="BTCUSDT", inst_id="BTCUSDT", side="sell", position_side="long", action="CLOSE", status="BLOCKED_SLIPPAGE", details="Proxy http://u:p@1.2.3.4 blocked", created_at=datetime.fromtimestamp((NOW_MS - 1000) / 1000, tz=timezone.utc)),
                AccountOrderHistory(account_id=10, exchange="binance", dedupe_key="oh-1", exchange_order_id="o-1", category="ordinary", symbol="BTCUSDT", inst_id="BTCUSDT", side="buy", position_side="long", order_type="LIMIT", status="FILLED", qty=Decimal("0.1"), filled_qty=Decimal("0.1"), created_at_ms=NOW_MS - 2000, updated_at_ms=NOW_MS - 1000, source_class="manual"),
                AccountEquitySnapshot(account_id=10, currency="USDT", captured_at_ms=NOW_MS - 60_000, bucket_start_ms=((NOW_MS - 60_000) // 60_000) * 60_000, resolution="raw", source="account_sync", equity=Decimal("2500"), unrealized_pnl=Decimal("0"), data_quality="ok"),
                # Ledger on soft-deleted account must remain preserved even though endpoint returns 404
                AccountCashflowEntry(account_id=12, exchange="binance", external_id="del-1", dedupe_key="del-1", asset="USDT", raw_type="TRANSFER", category="TRANSFER_IN", signed_amount=Decimal("100"), occurred_at_ms=NOW_MS),
            ])
            await db.commit()

        cookie_name = get_settings().cookie_name
        alice_tok = create_access_token(2, "alice", "user", 0)
        bob_tok = create_access_token(3, "bob", "user", 0)
        admin_tok = create_access_token(1, "admin_u", "admin", 0)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            # 1. Owner accesses /detail; secrets & proxy userinfo redacted
            r = await client.get("/api/accounts/10/detail", cookies={cookie_name: alice_tok})
            assert r.status_code == 200
            body_text = r.text
            assert "REAL_API_KEY_SECRET" not in body_text
            assert "proxypass" not in body_text
            assert r.json()["openOrdersSummary"]["openOrdersKnown"] is False

            # 2. Other user (Bob) denied (403); soft-deleted account returns 404; admin can read Alice
            assert (await client.get("/api/accounts/10/detail", cookies={cookie_name: bob_tok})).status_code == 403
            assert (await client.get("/api/accounts/12/detail", cookies={cookie_name: alice_tok})).status_code == 404
            assert (await client.get("/api/accounts/10/detail", cookies={cookie_name: admin_tok})).status_code == 200

            # 3. Cross-account bindingId must return 404 (cannot pass Bob's bindingId=21 against Alice's account=10)
            assert (await client.get("/api/accounts/10/trades?bindingId=21", cookies={cookie_name: alice_tok})).status_code == 404
            assert (await client.get("/api/accounts/10/performance?bindingId=21", cookies={cookie_name: alice_tok})).status_code == 404

            # 4. Limit validation & stable cashflow cursor pagination
            assert (await client.get("/api/accounts/10/cashflows?limit=501", cookies={cookie_name: alice_tok})).status_code == 422
            p1 = await client.get("/api/accounts/10/cashflows?limit=2", cookies={cookie_name: alice_tok})
            assert p1.status_code == 200
            d1 = p1.json()
            assert len(d1["items"]) == 2 and d1["hasMore"] is True
            assert d1["items"][0]["metadata"]["api_key"] != "SECRET_LEAK"
            p2 = await client.get(f"/api/accounts/10/cashflows?limit=2&cursor={d1['nextCursor']}", cookies={cookie_name: alice_tok})
            p3 = await client.get(f"/api/accounts/10/cashflows?limit=2&cursor={p2.json()['nextCursor']}", cookies={cookie_name: alice_tok})
            seen_ids = [x["id"] for x in d1["items"] + p2.json()["items"] + p3.json()["items"]]
            assert len(seen_ids) == 5 and len(set(seen_ids)) == 5

            # 5. Unified trades timeline pagination across fill + execution + order at identical occurredAtMs
            t1 = await client.get("/api/accounts/10/trades?limit=2", cookies={cookie_name: alice_tok})
            t2 = await client.get(f"/api/accounts/10/trades?limit=2&cursor={t1.json()['nextCursor']}", cookies={cookie_name: alice_tok})
            combined_trades = t1.json()["items"] + t2.json()["items"]
            assert [(x["recordType"], x["id"]) for x in combined_trades] == [("fill", 1), ("execution", 50), ("order", 1)]
            exec_item = next(x for x in combined_trades if x["recordType"] == "execution")
            assert exec_item["status"] == "BLOCKED_SLIPPAGE"
            assert "u:p@" not in (exec_item["details"] or "")

            # 5b. Exercise /equity, /orders (open & history), /performance, and POST /detail/sync
            eq_res = await client.get("/api/accounts/10/equity?range=30d", cookies={cookie_name: alice_tok})
            assert eq_res.status_code == 200 and len(eq_res.json()["points"]) == 1
            oo_res = await client.get("/api/accounts/10/orders?state=open", cookies={cookie_name: alice_tok})
            assert oo_res.status_code == 200 and oo_res.json()["openOrdersKnown"] is False
            oh_res = await client.get("/api/accounts/10/orders?state=history", cookies={cookie_name: alice_tok})
            assert oh_res.status_code == 200 and len(oh_res.json()["items"]) == 1
            perf_res = await client.get("/api/accounts/10/performance?range=90d", cookies={cookie_name: alice_tok})
            assert perf_res.status_code == 200 and perf_res.json()["executionQuality"]["blockedCount"] == 1
            with patch(
                "app.api.account_detail.sync_account_detail",
                AsyncMock(return_value={"ok": True, "accountId": 10, "syncedAtMs": NOW_MS, "datasets": {}, "results": {"open_orders": {"status": "ok"}}}),
            ):
                sync_res = await client.post("/api/accounts/10/detail/sync", cookies={cookie_name: alice_tok})
            assert sync_res.status_code == 200 and sync_res.json()["ok"] is True

            # 6. Test connectivity endpoint does NOT write equity snapshots
            fake_adapter = SimpleNamespace(
                get_contract_balance=AsyncMock(return_value=BalanceInfo(3000, 2900, 3000, 0, "USDT", usdt_eq=3000)),
                get_usdt_balance=AsyncMock(return_value=BalanceInfo(3000, 2900, 3000, 0, "USDT", usdt_eq=3000)),
                get_positions=AsyncMock(return_value=[]),
                get_account_config=AsyncMock(return_value={"position_mode": "hedge"}),
                close=AsyncMock(),
            )
            with patch("app.api.accounts.create_exchange_adapter", return_value=fake_adapter):
                test_res = await client.post("/api/accounts/10/test", cookies={cookie_name: alice_tok})
            assert test_res.status_code == 200
            async with factory() as db:
                snap_cnt = await db.scalar(select(func.count(AccountEquitySnapshot.id)).where(AccountEquitySnapshot.account_id == 10))
                assert snap_cnt == 1  # Still just our seeded row, zero rows added by /test

                # Soft-deleted account ledger survived
                del_cf_cnt = await db.scalar(select(func.count(AccountCashflowEntry.id)).where(AccountCashflowEntry.account_id == 12))
                assert del_cf_cnt == 1
    finally:
        await reset_db_engine()
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_manual_sync_coalescing_rate_limit_and_orchestrator_fault_isolation(tmp_path, monkeypatch):
    app = await _setup_test_app(tmp_path, monkeypatch)
    try:
        factory = get_session_factory()
        async with factory() as db:
            admin = User(id=1, username="admin_u", password_hash="h", role="admin")
            lead = LeadSource(id=1, name="LeadOne", platform="binance", portfolio_id="p100")
            vault = CredentialVault()
            creds = vault.encrypt_dict({"api_key": "k", "secret_key": "s"})
            acct = TradingAccount(
                id=1, owner_user_id=1, name="LiveAcct", exchange="binance", environment="live",
                encrypted_credentials=creds, api_key_masked="k****s", is_enabled=True,
                status="connected", last_error=None, positions_json="[]",
            )
            binding = CopyBinding(id=1, owner_user_id=1, name="B1", lead_id=1, account_id=1, circuit_breaker_tripped=False)
            db.add_all([admin, lead])
            await db.flush()
            db.add(acct)
            await db.flush()
            db.add(binding)
            await db.commit()

        call_counts = {"open_orders": 0}

        class SlowRateLimitedAdapter:
            def get_dataset_capabilities(self):
                return {"daily_snapshots": DatasetCapability("daily_snapshots", supported=False, note="SAPI unsupported")}

            async def get_open_orders_normalized(self):
                call_counts["open_orders"] += 1
                await asyncio.sleep(0.05)
                return []

            async def get_account_risk_snapshot(self):
                return AccountRiskInfo(currency="USDT", equity=Decimal("1000"), available_balance=Decimal("900"), wallet_balance=Decimal("1000"), unrealized_pnl=Decimal("0"))

            async def get_positions(self):
                return []

            async def get_cashflow_history(self, s, e, cursor=None):
                raise ExchangeAPIError("binance", "-1003", "Too many requests (rate limit)")

            async def get_order_history(self, s, e, symbols=None):
                return DatasetPage(items=[], complete=True, coverage_start_ms=s, coverage_end_ms=e)

            async def get_trade_fills(self, syms, s, e):
                return DatasetPage(items=[], complete=True, coverage_start_ms=s, coverage_end_ms=e)

            async def get_force_orders(self, s, e):
                return DatasetPage(items=[], complete=True, coverage_start_ms=s, coverage_end_ms=e)

            async def get_commission_rates(self, syms):
                return []

        adapter = SlowRateLimitedAdapter()
        # Concurrent calls for the same account coalesce into 1 upstream execution
        r1, r2 = await asyncio.gather(
            sync_account_detail(None, 1, manual=True, adapter_override=adapter),
            sync_account_detail(None, 1, manual=True, adapter_override=adapter),
        )
        assert call_counts["open_orders"] == 1
        assert r1["ok"] is True and r2["ok"] is True
        assert r1["datasets"]["cashflow"]["status"] == "rate_limited"
        assert r1["datasets"]["daily_snapshots"]["status"] == "unsupported"

        # Fault injection: capture_equity_snapshot failure during EngineOrchestrator.sync_account MUST NOT change account.status / last_error / breaker
        orch = EngineOrchestrator()
        bal = BalanceInfo(1200.0, 1100.0, 1200.0, 0.0, "USDT", usdt_eq=1200.0)
        sync_adapter = SimpleNamespace(
            get_contract_balance=AsyncMock(return_value=bal),
            get_usdt_balance=AsyncMock(return_value=bal),
            get_positions=AsyncMock(return_value=[]),
            get_futures_transfer_history=AsyncMock(return_value=[]),
            sync_time=AsyncMock(return_value=0),
        )
        async with factory() as db:
            acct_row = await db.get(TradingAccount, 1)
            proxy_row = ProxyNode(id=1, name="p", url="http://127.0.0.1:7890", is_enabled=True, is_healthy=True)
            with patch("app.services.engine.orchestrator.create_exchange_adapter", return_value=sync_adapter), \
                 patch("app.services.engine.orchestrator.capture_equity_snapshot", side_effect=RuntimeError("snapshot disk full")):
                res = await orch.sync_account(db, acct_row, proxies=[proxy_row])
            assert res["ok"] is True
            await db.refresh(acct_row)
            assert acct_row.status == "connected"
            assert acct_row.last_error is None
            b_row = await db.get(CopyBinding, 1)
            assert b_row.circuit_breaker_tripped is False
    finally:
        await reset_db_engine()
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_legacy_sqlite_database_copy_upgrades_idempotently(tmp_path, monkeypatch):
    """Simulate upgrading an existing pre-1.3.25 database copy twice without data loss."""
    legacy_db = tmp_path / "legacy_1_3_24.db"
    new_tables = {
        "account_equity_snapshots",
        "account_cashflow_entries",
        "account_trade_fills",
        "account_order_history",
        "account_open_order_snapshots",
        "account_dataset_sync_states",
        "account_alert_states",
    }
    sync_engine = create_engine(f"sqlite:///{legacy_db.as_posix()}")
    from app.core.database import Base as DomainBase
    pre_tables = [tbl for name, tbl in DomainBase.metadata.tables.items() if name not in new_tables]
    DomainBase.metadata.create_all(sync_engine, tables=pre_tables)
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO trading_accounts "
                "(id, name, exchange, environment, encrypted_credentials, api_key_masked, "
                "is_enabled, margin_mode, position_mode, usdt_equity, avail_equity, hwm_equity, "
                "day_start_equity, clock_offset_ms, total_notional, unrealized_pnl, status, created_at, updated_at) "
                "VALUES (7, 'LegacyAcct', 'binance', 'demo', 'enc', '****', "
                "1, 'cross', 'hedge', 500.0, 500.0, 500.0, 500.0, 0, 0.0, 0.0, 'idle', '2026-01-01', '2026-01-01')"
            )
        )
    sync_engine.dispose()

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{legacy_db.as_posix()}")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-at-least-32-chars-long")
    get_settings.cache_clear()
    await reset_db_engine()
    reset_engine_for_tests(f"sqlite+aiosqlite:///{legacy_db.as_posix()}")
    try:
        await init_db()
        await init_db()  # Idempotent second pass
        async with get_session_factory()() as db:
            acct = await db.get(TradingAccount, 7)
            assert acct is not None and acct.name == "LegacyAcct" and acct.usdt_equity == 500.0
            tables = {
                row[0]
                for row in (
                    await db.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
                ).fetchall()
            }
            assert {
                "account_equity_snapshots",
                "account_cashflow_entries",
                "account_trade_fills",
                "account_order_history",
                "account_open_order_snapshots",
                "account_dataset_sync_states",
                "account_alert_states",
            } <= tables
    finally:
        await reset_db_engine()
        get_settings.cache_clear()
