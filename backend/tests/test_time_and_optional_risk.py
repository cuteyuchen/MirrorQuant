"""UTC API timestamps and optional copy-trading policy regressions."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text

from app.core.database import get_session_factory, init_db
from app.core.time_util import BEIJING_TZ, to_utc_iso
from app.models.domain import CopyBinding, ExecutionRecord, LeadSource, LoginAttemptGuard, ProxyNode, TradingAccount
from app.schemas.api import BindingCreate, BindingUpdate, RiskDefaults, UserOut
from app.services.adapters.base import BalanceInfo
from app.services.engine.orchestrator import EngineOrchestrator
from app.services.engine.risk import RiskEngine
from app.services.engine.router import CopyRouter
from test_router_remediations import OfflineExchange, execute_batch, trading


@pytest.mark.parametrize("value,expected", [
    (None, None),
    (datetime(2026, 11, 14, 14, 38, 19, 655000), "2026-11-14T14:38:19.655000Z"),
    (datetime(2026, 11, 14, 22, 38, 19, tzinfo=BEIJING_TZ), "2026-11-14T14:38:19Z"),
    (datetime(2026, 11, 14, 14, 38, 19, tzinfo=timezone.utc), "2026-11-14T14:38:19Z"),
])
def test_to_utc_iso(value, expected):
    assert to_utc_iso(value) == expected


def test_user_json_serialization_handles_naive_and_offset_times():
    user = UserOut(id=1, username="test", role="admin", is_active=True,
        created_at=datetime(2026, 11, 14, 14, 38, 19),
        last_login_at=datetime(2026, 11, 14, 22, 38, 19, tzinfo=BEIJING_TZ))
    data = user.model_dump(mode="json", by_alias=True)
    assert data["createdAt"] == data["lastLoginAt"] == "2026-11-14T14:38:19Z"


async def test_api_time_fields_have_utc_z(async_client):
    res = await async_client.post("/api/auth/setup", json={"username": "timeadmin", "password": "SecurePassword2026!"})
    assert res.status_code == 201
    headers = {"Authorization": f"Bearer {res.json()['token']}"}
    stamp = datetime(2026, 11, 14, 14, 38, 19, 655000)
    async with get_session_factory()() as db:
        lead = LeadSource(name="time lead", portfolio_id="time-portfolio", last_sync_at=stamp,
                          snapshot_json='{"openPositions": []}')
        account = TradingAccount(name="time account", exchange="binance", encrypted_credentials="offline",
            api_key_masked="offline", last_sync_at=stamp,
            positions_json='[{"instId": "BTCUSDT", "posSide": "long", "pos": 1}]')
        proxy = ProxyNode(name="time proxy", url="http://localhost:7890", last_checked_at=stamp)
        guard = LoginAttemptGuard(guard_key="time-guard", key_type="ip", key_value="127.0.0.2",
                                 locked_until=stamp, last_fail_at=stamp)
        db.add_all([lead, account, proxy, guard])
        await db.flush()
        db.add(ExecutionRecord(lead_id=lead.id, account_id=account.id, lead_name=lead.name, account_name=account.name,
            symbol="BTCUSDT", inst_id="BTCUSDT", side="BUY", position_side="LONG", action="OPEN_LONG",
            status="EXECUTED", created_at=stamp))
        await db.commit()
    found = []

    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"createdAt", "lastLoginAt", "lastSyncAt", "lastCheckedAt", "lockedUntil", "lastFailAt"} and item is not None:
                    assert isinstance(item, str) and item.endswith("Z"), (key, item)
                    found.append(key)
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    for path in ["/api/auth/me", "/api/accounts", "/api/leads", "/api/proxies", "/api/dashboard/overview",
                 "/api/dashboard/executions", "/api/settings/audit-logs", "/api/settings/security-guards"]:
        response = await async_client.get(path, headers=headers)
        assert response.status_code == 200, path
        collect(response.json())
    assert {"createdAt", "lastSyncAt", "lastCheckedAt", "lockedUntil", "lastFailAt"} <= set(found)
    assert found.count("lastSyncAt") >= 5


@pytest.mark.parametrize("mode", ["off", "zero", "none"])
async def test_loss_breakers_skipped_for_open_orders(trading, monkeypatch, mode):
    t = trading
    t.account.hwm_equity = t.account.day_start_equity = 10000
    original = RiskEngine.loss_breaker_checks
    if mode == "off":
        t.binding.enable_circuit_breaker = False
    elif mode == "zero":
        t.binding.max_drawdown_pct = t.binding.daily_max_loss_pct = 0
    else:
        # Legacy/in-memory policies may omit limits even though DB columns are non-null.
        monkeypatch.setattr(RiskEngine, "loss_breaker_checks", lambda b, hwm, start, eq:
            original(SimpleNamespace(enable_circuit_breaker=True, max_drawdown_pct=None, daily_max_loss_pct=None), hwm, start, eq))
    def unexpected(*args, **kwargs):
        raise AssertionError("Disabled loss checks must not run")
    monkeypatch.setattr(RiskEngine, "check_drawdown", unexpected)
    monkeypatch.setattr(RiskEngine, "check_daily_loss", unexpected)
    exchange = OfflineExchange()
    await execute_batch(t, exchange, side="BUY", remaining=1)
    assert len(exchange.orders) == 1
    assert (await t.db.scalar(select(ExecutionRecord))).status == "EXECUTED"
    assert not t.binding.circuit_breaker_tripped


@pytest.mark.parametrize("field,status", [("max_drawdown_pct", "BLOCKED_DRAWDOWN"), ("daily_max_loss_pct", "BLOCKED_DAILY_LOSS")])
async def test_enabled_loss_breakers_still_block(trading, field, status):
    t = trading
    t.binding.enable_circuit_breaker = True
    t.binding.max_drawdown_pct = t.binding.daily_max_loss_pct = 0
    setattr(t.binding, field, 10)
    t.account.hwm_equity = t.account.day_start_equity = 10000
    exchange = OfflineExchange()
    await execute_batch(t, exchange, side="BUY", remaining=1)
    assert not exchange.orders
    assert (await t.db.scalar(select(ExecutionRecord))).status == status
    assert t.binding.circuit_breaker_tripped


@pytest.mark.parametrize("enabled,limit,tripped", [(False, 10, False), (True, 0, False), (True, 10, True)])
async def test_account_monitor_respects_loss_policy(trading, enabled, limit, tripped):
    t = trading
    t.binding.enable_circuit_breaker = enabled
    t.binding.max_drawdown_pct = t.binding.daily_max_loss_pct = limit
    t.account.hwm_equity = t.account.day_start_equity = 10000
    t.account.day_start_date = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    await t.db.commit()
    adapter = SimpleNamespace(get_usdt_balance=AsyncMock(return_value=BalanceInfo(1000, 1000, 0, 0)))
    await EngineOrchestrator()._check_account_equity(t.db, t.binding, t.account, adapter)
    assert t.binding.circuit_breaker_tripped is tripped
    assert t.account.usdt_equity == 1000


@pytest.mark.parametrize("enabled,qty,leverage,status", [
    (False, 10, 13, "EXECUTED"), (True, 0.5, 2, "EXECUTED"),
])
async def test_optional_caps_and_dynamic_exchange_leverage(trading, monkeypatch, enabled, qty, leverage, status):
    t = trading
    t.binding.enable_leverage_cap = enabled
    t.binding.max_single_leverage = t.binding.max_total_leverage = 0.05
    t.binding.max_instrument_leverage = 2
    if not enabled:
        monkeypatch.setattr(RiskEngine, "cap_by_leverage", lambda **kwargs: pytest.fail("Disabled cap was invoked"))
    exchange = OfflineExchange()
    exchange.get_max_leverage.return_value = 50
    await execute_batch(t, exchange, side="BUY", qty=10, remaining=10)
    assert Decimal(exchange.orders[0]["sz"]) == Decimal(str(qty))
    assert exchange.ensure_leverage.await_args.args == ("BTCUSDT", leverage)
    assert (await t.db.scalar(select(ExecutionRecord))).status == status


async def test_dynamic_leverage_obeys_exchange_tier(trading):
    t = trading
    t.binding.enable_leverage_cap = False
    t.binding.max_instrument_leverage = 2
    exchange = OfflineExchange()
    exchange.get_max_leverage.return_value = 7
    await execute_batch(t, exchange, side="BUY", qty=10, remaining=10)
    assert exchange.ensure_leverage.await_args.args == ("BTCUSDT", 7)
    assert Decimal(exchange.orders[0]["sz"]) == 10


@pytest.mark.parametrize("reason,clear", [("Drawdown 90% exceeds breaker 30%", True), ("Daily loss breaker", True),
    ("Consecutive order failures (5/5)", False), ("Protective quote unavailable", False),
    ("Invalid exchange account equity; entries suspended", False)])
async def test_disabling_loss_protection_only_releases_loss_latches(trading, reason, clear):
    t = trading
    t.binding.enable_circuit_breaker = False
    t.binding.circuit_breaker_tripped = True
    t.binding.circuit_breaker_reason = reason
    exchange = OfflineExchange()
    await execute_batch(t, exchange, side="BUY", remaining=1)
    assert bool(exchange.orders) is clear
    assert t.binding.circuit_breaker_tripped is not clear


@pytest.mark.parametrize("schema", [RiskDefaults, BindingCreate, BindingUpdate])
def test_risk_schemas_accept_switches_and_zero_limits(schema):
    fields = dict(enable_circuit_breaker=False, enable_leverage_cap=False,
                  max_drawdown_pct=0, daily_max_loss_pct=0, max_single_leverage=0, max_total_leverage=0)
    if schema is BindingCreate:
        fields.update(name="zero", lead_id=1, account_id=2)
    policy = schema(**fields)
    assert policy.enable_circuit_breaker is False and policy.enable_leverage_cap is False
    for field in ["max_drawdown_pct", "daily_max_loss_pct", "max_single_leverage", "max_total_leverage"]:
        assert getattr(policy, field) == 0
    with pytest.raises(ValidationError):
        schema(**{**fields, "max_drawdown_pct": -1})


async def test_existing_binding_migration_preserves_rows_and_defaults(trading):
    t = trading
    await t.db.close()
    from app.core.database import get_engine
    async with get_engine().begin() as conn:
        await conn.execute(text("ALTER TABLE copy_bindings DROP COLUMN enable_circuit_breaker"))
        await conn.execute(text("ALTER TABLE copy_bindings DROP COLUMN enable_leverage_cap"))
    await init_db()
    await init_db()
    async with get_session_factory()() as db:
        binding = await db.get(CopyBinding, t.binding.id)
        assert binding.name == "primary"
        assert binding.enable_circuit_breaker is True
        assert binding.enable_leverage_cap is False


def test_daily_loss_resets_at_beijing_midnight(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 14, 16, 0, tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr("app.services.engine.router.datetime", Clock)
    account = SimpleNamespace(day_start_date="2026-11-14", day_start_equity=2000)
    CopyRouter._refresh_day_start(account, 1000)
    assert account.day_start_date == "2026-11-15"
    assert account.day_start_equity == 1000
