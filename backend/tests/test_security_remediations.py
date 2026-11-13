import asyncio
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jwt
import pytest
from fastapi import FastAPI, WebSocketDisconnect
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import auth, ws
from app.core.config import Settings, _ensure_secret_file, get_settings
from app.core.database import Base, get_db, get_session_factory
from app.core.security import CredentialVault, _hotp, create_access_token, create_captcha_challenge, decode_access_token
from app.models.domain import CopyBinding, LeadSource, OrderIntent, PositionAllocation, SystemConfig, TradingAccount, User

PASSWORD = "SecurityTestPassword123!"


async def setup_user(client):
    result = await client.post("/api/auth/setup", json={"username": "security_admin", "password": PASSWORD})
    assert result.status_code == 201, result.text
    return result.json()


def test_access_tokens_reject_captcha_wrong_purpose_and_missing_claims():
    assert decode_access_token(create_captcha_challenge()["captchaToken"]) is None
    token = create_access_token(1, "admin", session_version=4)
    payload = decode_access_token(token)
    assert payload["typ"] == "mq_access" and payload["ver"] == 4
    for field, value in (("typ", "mq_captcha"), ("aud", "unrelated"), ("sub", "invalid"), ("ver", "4")):
        malformed = {**payload, field: value}
        assert decode_access_token(jwt.encode(malformed, get_settings().resolved_jwt_secret(), algorithm="HS256")) is None
    for field in ("ver", "exp", "sub", "typ"):
        malformed = {key: value for key, value in payload.items() if key != field}
        assert decode_access_token(jwt.encode(malformed, get_settings().resolved_jwt_secret(), algorithm="HS256")) is None
    assert decode_access_token(create_access_token(1, "admin", expires_minutes=-1)) is None


@pytest.mark.asyncio
async def test_spa_contains_encoded_traversals_and_symlinks(tmp_path, monkeypatch):
    import app.main as main

    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("safe index")
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("outside static")
    (static / "escape.txt").symlink_to(sentinel)
    monkeypatch.setattr(main, "__file__", str(tmp_path / "app" / "main.py"))
    app = main.create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        for path in ("/..%2fsentinel.txt", "/%2e%2e%2fsentinel.txt", "/escape.txt", "/%2ftmp/sentinel.txt"):
            response = await client.get(path)
            assert response.status_code == 404
        assert (await client.get("/dashboard")).text == "safe index"


@pytest.mark.asyncio
async def test_bootstrap_requires_operator_token_and_is_atomic(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bootstrap.db'}", connect_args={"timeout": 10})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        db.add(SystemConfig(key="bootstrap_claimed", value_json="false"))
        await db.commit()

    async def test_db():
        async with sessions() as db:
            yield db

    app = FastAPI()
    app.include_router(auth.router)
    app.dependency_overrides[get_db] = test_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            missing = await client.post("/api/auth/setup", json={"username": "intruder", "password": PASSWORD})
            assert missing.status_code == 403
            responses = await asyncio.gather(*(
                client.post("/api/auth/setup", json={"username": f"admin{index}", "password": PASSWORD},
                            headers={"X-MQ-Bootstrap-Token": get_settings().bootstrap_token})
                for index in range(4)
            ))
            assert sorted(response.status_code for response in responses) == [201, 409, 409, 409]
        async with sessions() as db:
            assert await db.scalar(select(func.count(User.id))) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_password_and_logout_revoke_copied_bearer_tokens(async_client):
    created = await setup_user(async_client)
    copied = created["token"]
    changed = await async_client.post("/api/auth/change-password", json={"currentPassword": PASSWORD, "newPassword": PASSWORD + "new"})
    assert changed.status_code == 200
    async_client.cookies.clear()
    assert (await async_client.get("/api/auth/me", headers={"Authorization": f"Bearer {copied}"})).status_code == 401
    login = await async_client.post("/api/auth/login", json={"username": "security_admin", "password": PASSWORD + "new"})
    assert login.status_code == 200
    copied = login.json()["token"]
    assert (await async_client.post("/api/auth/logout")).status_code == 200
    assert (await async_client.get("/api/auth/me", headers={"Authorization": f"Bearer {copied}"})).status_code == 401


@pytest.mark.asyncio
async def test_mfa_requires_password_existing_factor_and_revokes_sessions(async_client):
    await setup_user(async_client)
    assert (await async_client.post("/api/auth/2fa/setup", json={"currentPassword": "wrong"})).status_code == 400
    enrollment = await async_client.post("/api/auth/2fa/setup", json={"currentPassword": PASSWORD})
    assert enrollment.status_code == 200
    code = _hotp(enrollment.json()["secret"], int(time.time()) // 30)
    assert (await async_client.post("/api/auth/2fa/enable", json={"code": code})).status_code == 200
    assert (await async_client.get("/api/auth/me")).status_code == 401
    login = await async_client.post("/api/auth/login", json={"username": "security_admin", "password": PASSWORD, "totpCode": code})
    assert login.status_code == 200
    assert (await async_client.post("/api/auth/2fa/setup", json={"currentPassword": PASSWORD})).status_code == 400
    assert (await async_client.post("/api/auth/2fa/enable", json={"code": code})).status_code == 400
    for value in (None, "", " "):
        response = await async_client.post("/api/auth/2fa/disable", json={"currentPassword": PASSWORD, "code": value})
        assert response.status_code == 400
    assert (await async_client.post("/api/auth/2fa/disable", json={"currentPassword": PASSWORD, "code": code})).status_code == 200
    assert (await async_client.get("/api/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_mfa_corrupted_or_expired_state_fails_closed(async_client):
    created = await setup_user(async_client)
    user_id = created["user"]["id"]
    async with get_session_factory()() as db:
        await db.execute(update(User).where(User.id == user_id).values(totp_enabled=True, totp_secret_enc=None))
        await db.commit()
    response = await async_client.post("/api/auth/login", json={"username": "security_admin", "password": PASSWORD})
    assert response.status_code == 403 and "corrupted" in response.json()["detail"]
    async with get_session_factory()() as db:
        pending = CredentialVault().encrypt_dict({"secret": "AAAAAAAAAAAAAAAA", "expires_at": 0})
        await db.execute(update(User).where(User.id == user_id).values(totp_enabled=False, totp_pending_secret_enc=pending))
        await db.commit()
    response = await async_client.post("/api/auth/2fa/enable", json={"code": "123456"})
    assert response.status_code == 400 and "expired" in response.json()["detail"]


class FakeSocket:
    def __init__(self, token, origin="http://localhost:8000"):
        self.headers = {"origin": origin} if origin is not None else {}
        self.cookies = {get_settings().cookie_name: token}
        self.query_params = {}
        self.close = AsyncMock()
        self.accept = AsyncMock()
        self.send_json = AsyncMock()
        self.receive_text = AsyncMock(side_effect=WebSocketDisconnect())


@pytest.mark.asyncio
async def test_websocket_validates_origin_user_version_and_rechecks_before_broadcast(async_client):
    created = await setup_user(async_client)
    token = created["token"]
    for candidate, origin in (
        (create_captcha_challenge()["captchaToken"], "http://localhost:8000"),
        (token, "https://attacker.invalid"),
        (token, None),
        (create_access_token(999, "nonexistent"), "http://localhost:8000"),
        (create_access_token(created["user"]["id"], "security_admin", session_version=9), "http://localhost:8000"),
    ):
        socket = FakeSocket(candidate, origin)
        await ws.websocket_stream(socket)
        socket.accept.assert_not_awaited()
        socket.close.assert_awaited_once_with(code=1008)

    socket = FakeSocket(token)

    async def revoke_then_broadcast():
        async with get_session_factory()() as db:
            await db.execute(update(User).where(User.id == created["user"]["id"]).values(session_version=1))
            await db.commit()
        await ws.orchestrator.ws_hub.broadcast({"type": "private", "balance": 100})
        raise WebSocketDisconnect()

    socket.receive_text.side_effect = revoke_then_broadcast
    await ws.websocket_stream(socket)
    socket.accept.assert_awaited_once()
    socket.close.assert_awaited_once_with(code=1008)
    assert all(call.args[0]["type"] != "private" for call in socket.send_json.await_args_list)
    async with get_session_factory()() as db:
        await db.execute(update(User).where(User.id == created["user"]["id"]).values(is_active=False))
        await db.commit()
    socket = FakeSocket(create_access_token(created["user"]["id"], "security_admin", session_version=1))
    await ws.websocket_stream(socket)
    socket.accept.assert_not_awaited()
    socket.close.assert_awaited_once_with(code=1008)


@pytest.mark.asyncio
async def test_global_risk_and_resilience_reject_unsafe_values_on_write_and_apply(async_client):
    await setup_user(async_client)
    for payload in (
        {"riskDefaults": {"maxTotalLeverage": 1000000}},
        {"riskDefaults": {"dailyMaxLossPct": -1}},
        {"riskDefaults": {"hardStopLossPct": 0}},
        {"riskDefaults": {"orderMode": "invalid"}},
        {"riskDefaults": {"enableStopLoss": "false"}},
        {"riskDefaults": {"unknownOption": 1}},
        {"resilience": {"reconcileIntervalSec": 0}},
        {"resilience": {"autoCloseOrphanPositions": "sometimes"}},
    ):
        assert (await async_client.put("/api/settings", json=payload)).status_code == 422
    async with get_session_factory()() as db:
        await db.execute(update(SystemConfig).where(SystemConfig.key == "risk_defaults").values(value_json='{"hard_stop_loss_pct": 0}'))
        await db.commit()
    assert (await async_client.post("/api/settings/apply-defaults-to-bindings")).status_code == 422


def test_secret_file_creation_is_atomic_private_and_does_not_replace_empty_keys(tmp_path):
    path = tmp_path / "vault" / ".master_key"
    generated = []

    def generator():
        generated.append("one-persisted-key")
        time.sleep(0.01)
        return generated[-1]

    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(lambda _: _ensure_secret_file(path, generator), range(8)))
    assert generated == ["one-persisted-key"] and set(results) == {"one-persisted-key"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.write_text("")
    with pytest.raises(RuntimeError, match="Empty secret"):
        _ensure_secret_file(path, generator)


def test_missing_database_key_and_weak_configured_key_fail_closed(tmp_path):
    database = tmp_path / "existing.db"
    database.write_bytes(b"existing database sentinel")
    settings = Settings(_env_file=None, data_dir=str(tmp_path), database_url=f"sqlite+aiosqlite:///{database}", master_key="")
    with pytest.raises(RuntimeError, match="Key missing"):
        settings.resolved_master_key()
    assert not (tmp_path / ".master_key").exists()
    with pytest.raises(ValueError, match="32 random bytes"):
        Settings(_env_file=None, master_key="weak").resolved_master_key()


def test_configured_console_origins_accept_documented_environment_format(monkeypatch):
    monkeypatch.setenv("MQ_CORS_ORIGINS", "https://console.example,http://localhost:8080")
    assert Settings(_env_file=None).cors_origins == ["https://console.example", "http://localhost:8080"]


@pytest.mark.asyncio
async def test_startup_refuses_credentials_encrypted_by_another_key():
    import app.main as main

    async with get_session_factory()() as db:
        db.add(TradingAccount(name="wrong-key", exchange="okx", environment="demo", api_key_masked="test****",
                              encrypted_credentials=CredentialVault(master_key="different synthetic key").encrypt_dict({"api_key": "fake"})))
        await db.commit()
    with pytest.raises(RuntimeError, match="restore the correct master key"):
        async with main.lifespan(main.app):
            pytest.fail("Startup must not enable trading with unreadable credentials")


@pytest.mark.asyncio
async def test_lifecycle_changes_preserve_owned_and_unknown_exposure(async_client, monkeypatch):
    await setup_user(async_client)
    async with get_session_factory()() as db:
        lead = LeadSource(name="lead", portfolio_id="synthetic-lead", is_enabled=False)
        account = TradingAccount(name="account", exchange="okx", environment="demo", is_enabled=False,
                                 api_key_masked="test****",
                                 encrypted_credentials=CredentialVault().encrypt_dict({"api_key": "test", "secret_key": "test"}))
        db.add_all([lead, account])
        await db.flush()
        binding = CopyBinding(name="binding", lead_id=lead.id, account_id=account.id, is_enabled=False)
        db.add(binding)
        await db.flush()
        allocation = PositionAllocation(binding_id=binding.id, account_id=account.id, inst_id="BTC-USDT-SWAP", pos_side="long", owned_qty=2)
        db.add(allocation)
        await db.commit()
        ids = (lead.id, account.id, binding.id)
    lead_id, account_id, binding_id = ids
    for path in (f"/api/leads/{lead_id}", f"/api/accounts/{account_id}", f"/api/bindings/{binding_id}"):
        assert (await async_client.delete(path)).status_code == 409
    assert (await async_client.put(f"/api/bindings/{binding_id}", json={"symbolMap": {"BTCUSDT": "ETH-USDT-SWAP"}})).status_code == 409
    assert (await async_client.put(f"/api/accounts/{account_id}", json={"environment": "live"})).status_code == 409
    async with get_session_factory()() as db:
        await db.execute(update(PositionAllocation).values(owned_qty=0))
        db.add(OrderIntent(id="test-unknown", binding_id=binding_id, account_id=account_id, inst_id="BTC-USDT-SWAP",
                           pos_side="long", source_event_id="synthetic", state="UNKNOWN", requested_qty="1", reduce_only=False))
        await db.commit()
    assert (await async_client.delete(f"/api/bindings/{binding_id}")).status_code == 409
    async with get_session_factory()() as db:
        await db.execute(update(OrderIntent).values(state="FILLED", filled_qty="1"))
        await db.commit()
    adapter = SimpleNamespace(get_positions=AsyncMock(return_value=[SimpleNamespace(pos=3)]),
                              get_open_orders=AsyncMock(return_value=[]))
    monkeypatch.setattr("app.api.exposure.create_exchange_adapter", lambda *args, **kwargs: adapter)
    assert (await async_client.put(f"/api/accounts/{account_id}", json={"environment": "live"})).status_code == 409
    adapter.get_positions.return_value = [SimpleNamespace(pos=float("nan"))]
    assert (await async_client.put(f"/api/accounts/{account_id}", json={"environment": "live"})).status_code == 409
    adapter.get_positions.return_value = []
    adapter.get_open_orders.return_value = [{"id": "pending-conditional"}]
    assert (await async_client.put(f"/api/accounts/{account_id}", json={"environment": "live"})).status_code == 409
    adapter.get_open_orders.return_value = []
    assert (await async_client.put(f"/api/accounts/{account_id}", json={"environment": "live"})).status_code == 200
    # Filled-order evidence is retained even after the account is flat.
    assert (await async_client.delete(f"/api/bindings/{binding_id}")).status_code == 409
