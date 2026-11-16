"""管理员修改、登录隔离和带外解锁的本地安全回归。"""
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from argon2.exceptions import VerifyMismatchError
import pytest
from sqlalchemy import select

from app.api import auth
from app.core.database import DEFAULT_SYSTEM_CONFIG, get_session_factory
from app.core import security
from app.core.security import DUMMY_PASSWORD_HASH, create_access_token
from app.maintenance.unlock_admin import unlock_admin
from app.models.domain import AuditLog, BindingDebt, CopyBinding, LoginAttemptGuard, SystemConfig, User
from app.schemas.api import AccountCreate, AccountUpdate, BindingUpdate, LeadUpdate, ProxyUpdate, ResilienceSettings
from app.services.engine.router import CopyRouter
from test_dashboard_board import board_state


@pytest.mark.parametrize("path", ["reset-debt", "reset-breaker"])
async def test_binding_resets_share_execution_lock_and_persist_exact_state(board_state, monkeypatch, path):
    t = board_state
    debt = BindingDebt(binding_id=t.binding.id, symbol="BTCUSDT", position_side="LONG",
                       pending_usd=1, pending_usd_exact="1.000000000000000001")
    t.db.add(debt)
    t.binding.circuit_breaker_tripped, t.binding.consecutive_failures = True, 5
    await t.db.commit()
    transitions = []

    @asynccontextmanager
    async def locked():
        transitions.append("enter")
        yield
        transitions.append("exit")

    monkeypatch.setattr("app.services.engine.serialization.execution_lock", locked)
    response = await t.client.post(f"/api/bindings/{t.binding.id}/{path}")
    assert response.status_code == 200 and transitions == ["enter", "exit"]
    await t.db.refresh(debt)
    await t.db.refresh(t.binding)
    if path == "reset-debt":
        assert debt.pending_usd == 0 and debt.pending_usd_exact == "0"
        assert CopyRouter._pending_debt(debt) == 0
    else:
        assert not t.binding.circuit_breaker_tripped and t.binding.consecutive_failures == 0


async def test_generic_settings_cannot_change_kill_switch_and_public_missing_defaults_closed(board_state):
    t = board_state
    assert (await t.client.post("/api/bindings/kill-switch", json={"enabled": True})).status_code == 200
    response = await t.client.put("/api/settings", json={"globalKillSwitch": False})
    assert response.status_code == 200 and response.json()["global_kill_switch"] is True
    response = await t.client.put("/api/settings", json={"global_kill_switch": False})
    assert response.json()["global_kill_switch"] is True
    assert ResilienceSettings().flip_close_first == DEFAULT_SYSTEM_CONFIG["resilience"]["flip_close_first"]
    await t.db.delete(await t.db.get(SystemConfig, "public_monitoring_enabled"))
    await t.db.commit()
    assert (await t.client.get("/api/dashboard/public")).json() == {"enabled": False, "leads": []}


@pytest.mark.parametrize("resource", ["accounts", "leads"])
async def test_assigned_proxy_cannot_be_deleted_into_silent_direct_egress(board_state, resource):
    # 即使引用方已禁用也不能用删除代理绕过显式出口配置。
    t = board_state
    row = t.account if resource == "accounts" else t.lead
    row.proxy_id, row.is_enabled = t.proxy.id, False
    await t.db.commit()
    response = await t.client.delete(f"/api/proxies/{t.proxy.id}")
    assert response.status_code == 409 and "explicitly reassign" in response.text
    await t.db.refresh(row)
    assert row.proxy_id == t.proxy.id
    assert (await t.client.put(f"/api/{resource}/{row.id}", json={"proxyId": None})).status_code == 200
    assert (await t.client.delete(f"/api/proxies/{t.proxy.id}")).status_code == 200


@pytest.mark.parametrize("raw", ['"true"', "1", "{}", "invalid"])
async def test_public_dashboard_invalid_opt_in_stays_closed(board_state, raw):
    # 非布尔或损坏值都不是授权公开。
    t = board_state
    (await t.db.get(SystemConfig, "public_monitoring_enabled")).value_json = raw
    await t.db.commit()
    assert (await t.client.get("/api/dashboard/public")).json() == {"enabled": False, "leads": []}


@pytest.mark.parametrize("exchange", ["bybit", "bitget"])
def test_experimental_exchange_requires_explicit_environment_opt_in(monkeypatch, exchange):
    values = {"name": "test", "exchange": exchange, "apiKey": "fake-key", "secretKey": "fake-secret"}
    monkeypatch.delenv("MQ_ENABLE_EXPERIMENTAL_EXCHANGES", raising=False)
    with pytest.raises(ValueError, match="Experimental exchange disabled"):
        AccountCreate.model_validate(values)
    monkeypatch.setenv("MQ_ENABLE_EXPERIMENTAL_EXCHANGES", "1")
    with pytest.raises(ValueError):
        AccountCreate.model_validate(values)
    monkeypatch.setenv("MQ_ENABLE_EXPERIMENTAL_EXCHANGES", "true")
    assert AccountCreate.model_validate(values).exchange == exchange


@pytest.mark.parametrize("exchange", ["bybit", "bitget"])
async def test_account_creation_experimental_exchange_gate_at_http_boundary(board_state, monkeypatch, exchange):
    # 环境门禁在请求 schema 上执行，不允许客户端自行绕过。
    t = board_state
    payload = {"name": "Experimental", "exchange": exchange, "apiKey": "fake-key",
               "secretKey": "fake-secret", "passphrase": "fake-passphrase"}
    monkeypatch.delenv("MQ_ENABLE_EXPERIMENTAL_EXCHANGES", raising=False)
    assert (await t.client.post("/api/accounts", json=payload)).status_code == 422
    monkeypatch.setenv("MQ_ENABLE_EXPERIMENTAL_EXCHANGES", "true")
    assert (await t.client.post("/api/accounts", json=payload)).status_code == 201


@pytest.mark.parametrize("model,field", [
    (LeadUpdate, "name"), (LeadUpdate, "pollIntervalSec"), (ProxyUpdate, "url"),
    (ProxyUpdate, "isEnabled"), (BindingUpdate, "riskScale"), (BindingUpdate, "symbolMap"),
])
def test_update_not_null_fields_reject_explicit_null_but_allow_omission(model, field):
    assert model.model_validate({}).model_dump(exclude_unset=True) == {}
    with pytest.raises(ValueError, match="cannot be null"):
        model.model_validate({field: None})


@pytest.mark.parametrize("model", [AccountUpdate, LeadUpdate, ProxyUpdate, BindingUpdate])
def test_all_update_non_nullable_fields_reject_null(model):
    # 逐字段校验，避免新字段遗漏 NOT NULL 更新保护。
    for name, field in model.model_fields.items():
        if name not in model.nullable_fields:
            with pytest.raises(ValueError, match="cannot be null"):
                model.model_validate({field.alias or name: None})


def test_dummy_hash_performs_a_real_argon2_mismatch_verification():
    # 不存在用户仍走同参数哈希，格式有效但绝不作为任何登录凭据。
    with pytest.raises(VerifyMismatchError):
        security._ph.verify(DUMMY_PASSWORD_HASH, "synthetic-noncredential")


async def login_fixture():
    async with get_session_factory()() as db:
        user = User(username="login-admin", role="admin", password_hash="synthetic-stored-hash")
        db.add(user)
        await db.commit()
        return user.id


async def test_hard_lock_is_per_username_ip_and_username_only_delays(async_client, monkeypatch):
    await login_fixture()
    now = datetime.now(timezone.utc)
    async with get_session_factory()() as db:
        db.add_all([
            LoginAttemptGuard(guard_key="user:login-admin", key_type="user", key_value="login-admin",
                              fail_count=100, last_fail_at=now, locked_until=now + timedelta(hours=2)),
            LoginAttemptGuard(guard_key="user_ip:" + auth._pair_value("login-admin", "203.0.113.1"),
                              key_type="user_ip", key_value=auth._pair_value("login-admin", "203.0.113.1"),
                              fail_count=5, last_fail_at=now, locked_until=now + timedelta(minutes=5)),
        ])
        await db.commit()
    verify = AsyncMock(return_value=True)
    delay = AsyncMock()
    monkeypatch.setattr(auth, "verify_password_async", verify)
    monkeypatch.setattr(auth.asyncio, "sleep", delay)
    payload = {"username": "login-admin", "password": "synthetic"}
    blocked = await async_client.post("/api/auth/login", json=payload, headers={"X-Forwarded-For": "203.0.113.1"})
    assert blocked.status_code == 429 and int(blocked.headers["Retry-After"]) > 0
    verify.assert_not_awaited()
    allowed = await async_client.post("/api/auth/login", json=payload, headers={"X-Forwarded-For": "203.0.113.2"})
    assert allowed.status_code == 200
    verify.assert_awaited_once()
    delay.assert_awaited_once_with(5.0)


async def test_same_ip_lock_for_another_username_does_not_block_admin(async_client, monkeypatch):
    # 硬锁必须同时命中用户名与 IP，历史 IP 全局锁也不参与登录 admission。
    await login_fixture()
    now = datetime.now(timezone.utc)
    ip = "203.0.113.9"
    pair = auth._pair_value("intruder", ip)
    async with get_session_factory()() as db:
        db.add_all([
            LoginAttemptGuard(guard_key="user_ip:" + pair, key_type="user_ip", key_value=pair,
                fail_count=15, last_fail_at=now, locked_until=now + timedelta(hours=2)),
            LoginAttemptGuard(guard_key="ip:" + ip, key_type="ip", key_value=ip, fail_count=15,
                last_fail_at=now, locked_until=now + timedelta(hours=2)),
        ])
        await db.commit()
    verify = AsyncMock(return_value=True)
    monkeypatch.setattr(auth, "verify_password_async", verify)
    result = await async_client.post("/api/auth/login", json={"username": "login-admin", "password": "synthetic"},
                                     headers={"X-Forwarded-For": ip})
    assert result.status_code == 200
    verify.assert_awaited_once()


async def test_old_failures_decay_before_increment_and_unknown_users_still_hash(async_client, monkeypatch):
    now = datetime.now(timezone.utc)
    pair = auth._pair_value("missing-admin", "203.0.113.3")
    async with get_session_factory()() as db:
        db.add(LoginAttemptGuard(guard_key="user_ip:" + pair, key_type="user_ip", key_value=pair,
            fail_count=14, last_fail_at=now - timedelta(hours=1), locked_until=now - timedelta(minutes=1)))
        await db.commit()
    verify = AsyncMock(return_value=False)
    monkeypatch.setattr(auth, "verify_password_async", verify)
    result = await async_client.post("/api/auth/login", json={"username": "missing-admin", "password": "wrong"},
                                     headers={"X-Forwarded-For": "203.0.113.3"})
    assert result.status_code == 401 and result.json()["failCount"] == 1
    verify.assert_awaited_once_with("wrong", DUMMY_PASSWORD_HASH)
    too_long = await async_client.post("/api/auth/login", json={"username": "x" * 65, "password": "wrong"})
    assert too_long.status_code == 422 and verify.await_count == 1


async def test_expired_username_failures_no_longer_delay_or_require_captcha(async_client, monkeypatch):
    # 时间窗过期后，用户名延迟与组合键验证码要求一起衰减。
    username, ip = "expired-admin", "203.0.113.4"
    pair = auth._pair_value(username, ip)
    now = datetime.now(timezone.utc)
    async with get_session_factory()() as db:
        db.add_all([
            LoginAttemptGuard(guard_key=f"user:{username}", key_type="user", key_value=username,
                              fail_count=100, last_fail_at=now - timedelta(hours=1)),
            LoginAttemptGuard(guard_key="user_ip:" + pair, key_type="user_ip", key_value=pair,
                              fail_count=14, last_fail_at=now - timedelta(hours=1)),
        ])
        await db.commit()
    verify, delay = AsyncMock(return_value=False), AsyncMock()
    monkeypatch.setattr(auth, "verify_password_async", verify)
    monkeypatch.setattr(auth.asyncio, "sleep", delay)
    response = await async_client.post("/api/auth/login", json={"username": username, "password": "wrong"},
                                       headers={"X-Forwarded-For": ip})
    assert response.status_code == 401 and response.json()["failCount"] == 1
    assert response.json()["captchaRequired"] is False
    delay.assert_not_awaited()
    verify.assert_awaited_once_with("wrong", DUMMY_PASSWORD_HASH)


async def test_offline_unlock_only_resets_selected_admin_pair_and_preserves_credentials():
    user_id = await login_fixture()
    now = datetime.now(timezone.utc)
    async with get_session_factory()() as db:
        for username, ip in [("login-admin", "203.0.113.1"), ("login-admin", "203.0.113.2"), ("other", "203.0.113.1")]:
            pair = auth._pair_value(username, ip)
            db.add(LoginAttemptGuard(guard_key="user_ip:" + pair, key_type="user_ip", key_value=pair,
                fail_count=5, last_username=username, last_ip=ip, last_fail_at=now,
                locked_until=now + timedelta(minutes=5)))
        db.add(LoginAttemptGuard(guard_key="user:login-admin", key_type="user", key_value="login-admin",
                                fail_count=9, last_fail_at=now))
        await db.commit()
    assert await unlock_admin("login-admin", "203.0.113.1") == 2
    async with get_session_factory()() as db:
        rows = list((await db.scalars(select(LoginAttemptGuard).where(LoginAttemptGuard.key_type == "user_ip"))).all())
        assert {(r.last_username, r.last_ip): r.fail_count for r in rows} == {
            ("login-admin", "203.0.113.1"): 0, ("login-admin", "203.0.113.2"): 5, ("other", "203.0.113.1"): 5}
        user = await db.get(User, user_id)
        assert user.password_hash == "synthetic-stored-hash" and not user.totp_enabled and user.session_version == 0
        assert (await db.scalar(select(AuditLog))).action == "UNLOCK_ADMIN_LOGIN"
    with pytest.raises(ValueError, match="Admin not found"):
        await unlock_admin("missing")
