"""1.3.3 用户注册与管理员管理：邀请码注册、管理员用户管理、权限隔离。

覆盖：邀请码生成/使用/过期/作废、注册（有效码成功、无效码失败）、
管理员用户列表与详情、删用户守门（启用绑定或持仓时 409、关掉后可删且
连带软删账户绑定）、权限隔离（非 admin 碰不了别人的账户/绑定、列表
只见自己的）、登录拒绝已删用户。
"""
import json

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.database import get_session_factory
from app.models.domain import (
    CopyBinding,
    ExecutionRecord,
    PositionAllocation,
    TradingAccount,
    User,
)

ADMIN_PASSWORD = "AdminPass2026!"
USER_PASSWORD = "UserPass2026!"


async def _setup_admin(client: AsyncClient) -> dict:
    res = await client.post("/api/auth/setup", json={"username": "admin", "password": ADMIN_PASSWORD})
    assert res.status_code == 201, res.text
    token = res.json()["token"]
    # 注册/登录响应会往共享 client 的 cookie 里写新 token；清掉它，后续请求
    # 以 Authorization 头为准，否则 cookie 会盖过头里的另一个用户。
    client.cookies.clear()
    return {"Authorization": f"Bearer {token}"}


async def _make_invite(client: AsyncClient, headers: dict, max_uses: int = 1, expires_at=None) -> dict:
    body = {"maxUses": max_uses}
    if expires_at:
        body["expiresAt"] = expires_at
    res = await client.post("/api/admin/invite-codes", json=body, headers=headers)
    assert res.status_code == 201, res.text
    return res.json()["item"]


async def _register(client: AsyncClient, username: str, password: str, code: str):
    return await client.post(
        "/api/auth/register", json={"username": username, "password": password, "inviteCode": code})


async def _register_user(client: AsyncClient, admin_headers: dict, username: str, code: str) -> dict:
    res = await _register(client, username, USER_PASSWORD, code)
    assert res.status_code == 201, res.text
    client.cookies.clear()
    return {"Authorization": f"Bearer {res.json()['token']}"}


async def _make_lead_account_binding(client: AsyncClient, headers: dict, admin_headers: dict = None):
    """Create proxy + lead (admin-only) then account + binding as the user.

    Returns ids. admin_headers defaults to headers (admin creating for self).
    """
    admin_headers = admin_headers or headers
    res = await client.post("/api/proxies", headers=admin_headers,
                            json={"name": "P1", "url": "http://127.0.0.1:7890", "region": "JP", "priority": 10})
    assert res.status_code == 201, res.text
    proxy_id = res.json()["item"]["id"]
    res = await client.post("/api/leads", headers=admin_headers, json={
        "name": "Lead-1", "platform": "binance_bapi", "portfolioId": "1112223334445556",
        "pollIntervalSec": 15, "proxyId": proxy_id, "isPublicDashboard": False})
    assert res.status_code == 201, res.text
    lead_id = res.json()["item"]["id"]
    res = await client.post("/api/accounts", headers=headers, json={
        "name": "Acct-1", "exchange": "binance", "environment": "demo",
        "api_key": "test_key_12345", "secret_key": "test_secret_67890", "proxyId": proxy_id})
    assert res.status_code == 201, res.text
    account_id = res.json()["item"]["id"]
    res = await client.post("/api/bindings", headers=headers, json={
        "name": "Lead -> Acct", "lead_id": lead_id, "account_id": account_id})
    assert res.status_code == 201, res.text
    return {"proxy_id": proxy_id, "lead_id": lead_id, "account_id": account_id,
            "binding_id": res.json()["item"]["id"]}


async def _set_allocation(binding_id: int, account_id: int, qty: float, inst_id: str = "ZECUSDT",
                          pos_side: str = "short") -> None:
    async with get_session_factory()() as db:
        db.add(PositionAllocation(binding_id=binding_id, account_id=account_id, inst_id=inst_id,
                                  pos_side=pos_side, owned_qty=qty, entry_cost=qty * 700 if qty else 0.0))
        await db.commit()


async def _set_account_positions(account_id: int, positions: list) -> None:
    async with get_session_factory()() as db:
        acct = await db.get(TradingAccount, account_id)
        acct.positions_json = json.dumps(positions)
        await db.commit()


async def _add_execution(binding_id: int, lead_id: int, account_id: int) -> int:
    async with get_session_factory()() as db:
        rec = ExecutionRecord(binding_id=binding_id, lead_id=lead_id, account_id=account_id,
                              lead_name="Lead-1", account_name="Acct-1", symbol="ZEC",
                              inst_id="ZECUSDT", side="SELL", position_side="short",
                              action="OPEN", status="EXECUTED")
        db.add(rec)
        await db.commit()
        return rec.id


# ---- 邀请码与注册 ----

@pytest.mark.asyncio
async def test_register_with_valid_invite_code(async_client: AsyncClient):
    admin = await _setup_admin(async_client)
    code = await _make_invite(async_client, admin)
    assert len(code["code"]) == 16 and code["status"] == "active" and code["usedCount"] == 0

    res = await _register(async_client, "alice", USER_PASSWORD, code["code"])
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["user"]["username"] == "alice" and body["user"]["role"] == "user"
    assert body["token"]
    async_client.cookies.clear()
    # 注册即登录：token 能调 /me。
    res = await async_client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert res.status_code == 200 and res.json()["user"]["username"] == "alice"
    # 单次码用掉后即耗尽。
    res = await async_client.get("/api/admin/invite-codes", headers=admin)
    assert res.json()["items"][0]["usedCount"] == 1
    assert res.json()["items"][0]["status"] == "exhausted"


@pytest.mark.asyncio
async def test_register_rejects_bad_invite_codes(async_client: AsyncClient):
    admin = await _setup_admin(async_client)
    # 不存在的码。
    res = await _register(async_client, "alice", USER_PASSWORD, "NOPE123456789012")
    assert res.status_code == 400
    # 过期码。
    code = await _make_invite(async_client, admin, expires_at="2020-01-01T00:00:00Z")
    res = await _register(async_client, "alice", USER_PASSWORD, code["code"])
    assert res.status_code == 400 and "expired" in res.json()["detail"]
    # 作废码。
    code = await _make_invite(async_client, admin)
    res = await async_client.post(f"/api/admin/invite-codes/{code['id']}/revoke", headers=admin)
    assert res.status_code == 200 and res.json()["item"]["status"] == "revoked"
    res = await _register(async_client, "alice", USER_PASSWORD, code["code"])
    assert res.status_code == 400 and "revoked" in res.json()["detail"]
    # 用完的码（max_uses=1，用一次后再注册失败）。
    code = await _make_invite(async_client, admin)
    res = await _register(async_client, "alice", USER_PASSWORD, code["code"])
    assert res.status_code == 201
    res = await _register(async_client, "bob", USER_PASSWORD, code["code"])
    assert res.status_code == 400 and "fully used" in res.json()["detail"]
    # 无邀请码不开放注册：inviteCode 为空即 422。
    res = await async_client.post("/api/auth/register",
                                  json={"username": "carol", "password": USER_PASSWORD, "inviteCode": ""})
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_register_rejects_duplicate_username(async_client: AsyncClient):
    admin = await _setup_admin(async_client)
    code = await _make_invite(async_client, admin, max_uses=5)
    res = await _register(async_client, "alice", USER_PASSWORD, code["code"])
    assert res.status_code == 201
    res = await _register(async_client, "alice", "OtherPass2026!", code["code"])
    assert res.status_code == 409


@pytest.mark.asyncio
async def test_invite_code_multi_use(async_client: AsyncClient):
    admin = await _setup_admin(async_client)
    code = await _make_invite(async_client, admin, max_uses=2)
    assert (await _register(async_client, "alice", USER_PASSWORD, code["code"])).status_code == 201
    assert (await _register(async_client, "bob", USER_PASSWORD, code["code"])).status_code == 201
    assert (await _register(async_client, "carol", USER_PASSWORD, code["code"])).status_code == 400


# ---- 管理员用户列表与详情 ----

@pytest.mark.asyncio
async def test_admin_users_list_and_detail(async_client: AsyncClient):
    admin = await _setup_admin(async_client)
    code = await _make_invite(async_client, admin, max_uses=5)
    await _register_user(async_client, admin, "alice", code["code"])
    alice_headers = await _register_user(async_client, admin, "alice2", code["code"])

    res = await async_client.get("/api/admin/users", headers=admin)
    assert res.status_code == 200
    users = {u["username"]: u for u in res.json()["items"]}
    assert set(users) == {"admin", "alice", "alice2"}
    assert users["admin"]["role"] == "admin"
    assert users["alice"]["role"] == "user" and users["alice"]["accountCount"] == 0

    # alice2 建一个账户+绑定，跟单摘要随之变化。
    ids = await _make_lead_account_binding(async_client, alice_headers, admin)
    res = await async_client.get("/api/admin/users", headers=admin)
    users = {u["username"]: u for u in res.json()["items"]}
    assert users["alice2"]["accountCount"] == 1
    assert users["alice2"]["bindingCount"] == 1
    assert users["alice2"]["enabledBindingCount"] == 1

    alice2_id = users["alice2"]["id"]
    res = await async_client.get(f"/api/admin/users/{alice2_id}", headers=admin)
    assert res.status_code == 200
    detail = res.json()
    assert detail["accounts"][0]["name"] == "Acct-1"
    assert detail["bindings"][0]["name"] == "Lead -> Acct"
    assert detail["bindings"][0]["isEnabled"] is True
    # 非 admin 调管理员接口一律 403。
    res = await async_client.get("/api/admin/users", headers=alice_headers)
    assert res.status_code == 403
    res = await async_client.get(f"/api/admin/users/{alice2_id}", headers=alice_headers)
    assert res.status_code == 403


# ---- 删用户守门与连带软删 ----

@pytest.mark.asyncio
async def test_delete_user_blocked_by_enabled_binding(async_client: AsyncClient):
    admin = await _setup_admin(async_client)
    code = await _make_invite(async_client, admin, max_uses=5)
    alice_headers = await _register_user(async_client, admin, "alice", code["code"])
    ids = await _make_lead_account_binding(async_client, alice_headers, admin)

    res = await async_client.get("/api/admin/users", headers=admin)
    alice_id = next(u["id"] for u in res.json()["items"] if u["username"] == "alice")
    res = await async_client.delete(f"/api/admin/users/{alice_id}", headers=admin)
    assert res.status_code == 409, res.text
    assert f"绑定 #{ids['binding_id']}" in res.json()["detail"]
    assert "仍启用" in res.json()["detail"]


@pytest.mark.asyncio
async def test_delete_user_blocked_by_open_allocation(async_client: AsyncClient):
    admin = await _setup_admin(async_client)
    code = await _make_invite(async_client, admin, max_uses=5)
    alice_headers = await _register_user(async_client, admin, "alice", code["code"])
    ids = await _make_lead_account_binding(async_client, alice_headers, admin)
    # 先停用绑定，再模拟未平持仓。
    res = await async_client.post(f"/api/bindings/{ids['binding_id']}/toggle", headers=alice_headers)
    assert res.status_code == 200 and res.json()["item"]["isEnabled"] is False
    await _set_allocation(ids["binding_id"], ids["account_id"], 0.904)

    res = await async_client.get("/api/admin/users", headers=admin)
    alice_id = next(u["id"] for u in res.json()["items"] if u["username"] == "alice")
    res = await async_client.delete(f"/api/admin/users/{alice_id}", headers=admin)
    assert res.status_code == 409, res.text
    assert "ZECUSDT" in res.json()["detail"] and "0.904" in res.json()["detail"]


@pytest.mark.asyncio
async def test_delete_user_blocked_by_account_position(async_client: AsyncClient):
    admin = await _setup_admin(async_client)
    code = await _make_invite(async_client, admin, max_uses=5)
    alice_headers = await _register_user(async_client, admin, "alice", code["code"])
    ids = await _make_lead_account_binding(async_client, alice_headers, admin)
    await async_client.post(f"/api/bindings/{ids['binding_id']}/toggle", headers=alice_headers)
    # 账户里有孤儿仓（不在任何 allocation 里），同样挡删。
    await _set_account_positions(ids["account_id"], [
        {"instId": "BTCUSDT", "posSide": "long", "pos": 0.05, "symbol": "BTC"}])

    res = await async_client.get("/api/admin/users", headers=admin)
    alice_id = next(u["id"] for u in res.json()["items"] if u["username"] == "alice")
    res = await async_client.delete(f"/api/admin/users/{alice_id}", headers=admin)
    assert res.status_code == 409, res.text
    assert "BTCUSDT" in res.json()["detail"]


@pytest.mark.asyncio
async def test_delete_user_cascades_soft_delete_and_blocks_login(async_client: AsyncClient):
    admin = await _setup_admin(async_client)
    code = await _make_invite(async_client, admin, max_uses=5)
    alice_headers = await _register_user(async_client, admin, "alice", code["code"])
    ids = await _make_lead_account_binding(async_client, alice_headers, admin)
    exec_id = await _add_execution(ids["binding_id"], ids["lead_id"], ids["account_id"])
    await async_client.post(f"/api/bindings/{ids['binding_id']}/toggle", headers=alice_headers)

    res = await async_client.get("/api/admin/users", headers=admin)
    alice_id = next(u["id"] for u in res.json()["items"] if u["username"] == "alice")
    res = await async_client.delete(f"/api/admin/users/{alice_id}", headers=admin)
    assert res.status_code == 200, res.text

    async with get_session_factory()() as db:
        user = await db.get(User, alice_id)
        assert user.deleted_at is not None
        acct = await db.get(TradingAccount, ids["account_id"])
        assert acct.deleted_at is not None
        binding = await db.get(CopyBinding, ids["binding_id"])
        assert binding.deleted_at is not None
        # 账本原样保留。
        rec = await db.get(ExecutionRecord, exec_id)
        assert rec is not None and rec.status == "EXECUTED"
    # 列表里不再出现；已删用户登录按账号不存在处理。
    res = await async_client.get("/api/admin/users", headers=admin)
    assert {u["username"] for u in res.json()["items"]} == {"admin"}
    res = await async_client.post("/api/auth/login", json={"username": "alice", "password": USER_PASSWORD})
    assert res.status_code == 401
    # 已删用户的旧 token 也失效。
    res = await async_client.get("/api/auth/me", headers=alice_headers)
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_admin_cannot_delete_self_or_last_admin(async_client: AsyncClient):
    admin = await _setup_admin(async_client)
    res = await async_client.get("/api/admin/users", headers=admin)
    admin_id = res.json()["items"][0]["id"]
    res = await async_client.delete(f"/api/admin/users/{admin_id}", headers=admin)
    assert res.status_code == 400
    # 停用自己、把自己降级也都拒绝（自己是最后一个 admin）。
    res = await async_client.patch(f"/api/admin/users/{admin_id}", json={"isActive": False}, headers=admin)
    assert res.status_code == 400
    res = await async_client.patch(f"/api/admin/users/{admin_id}", json={"role": "user"}, headers=admin)
    assert res.status_code == 409


@pytest.mark.asyncio
async def test_patch_role_deactivate_and_reset_password(async_client: AsyncClient):
    admin = await _setup_admin(async_client)
    code = await _make_invite(async_client, admin, max_uses=5)
    alice_headers = await _register_user(async_client, admin, "alice", code["code"])
    res = await async_client.get("/api/admin/users", headers=admin)
    alice_id = next(u["id"] for u in res.json()["items"] if u["username"] == "alice")
    # 管理员重置密码后，旧 token 失效、新密码能登录。
    res = await async_client.post(f"/api/admin/users/{alice_id}/reset-password",
                                  json={"newPassword": "ResetPass2026!"}, headers=admin)
    assert res.status_code == 200
    res = await async_client.get("/api/auth/me", headers=alice_headers)
    assert res.status_code == 401
    res = await async_client.post("/api/auth/login", json={"username": "alice", "password": "ResetPass2026!"})
    assert res.status_code == 200
    async_client.cookies.clear()
    alice_headers = {"Authorization": f"Bearer {res.json()['token']}"}
    # 停用后登录被拒；再启用恢复。
    res = await async_client.patch(f"/api/admin/users/{alice_id}", json={"isActive": False}, headers=admin)
    assert res.status_code == 200 and res.json()["user"]["isActive"] is False
    res = await async_client.post("/api/auth/login", json={"username": "alice", "password": "ResetPass2026!"})
    assert res.status_code == 403
    res = await async_client.patch(f"/api/admin/users/{alice_id}", json={"isActive": True}, headers=admin)
    assert res.status_code == 200
    res = await async_client.post("/api/auth/login", json={"username": "alice", "password": "ResetPass2026!"})
    assert res.status_code == 200
    async_client.cookies.clear()
    alice_headers = {"Authorization": f"Bearer {res.json()['token']}"}
    # 升为 admin 后能调管理员接口。
    res = await async_client.patch(f"/api/admin/users/{alice_id}", json={"role": "admin"}, headers=admin)
    assert res.status_code == 200 and res.json()["user"]["role"] == "admin"
    res = await async_client.get("/api/admin/users", headers=alice_headers)
    assert res.status_code == 200


# ---- 权限隔离 ----

@pytest.mark.asyncio
async def test_non_admin_isolation(async_client: AsyncClient):
    admin = await _setup_admin(async_client)
    code = await _make_invite(async_client, admin, max_uses=5)
    alice_headers = await _register_user(async_client, admin, "alice", code["code"])
    bob_headers = await _register_user(async_client, admin, "bob", code["code"])
    ids = await _make_lead_account_binding(async_client, alice_headers, admin)

    # bob 的列表里看不到 alice 的账户与绑定。
    res = await async_client.get("/api/accounts", headers=bob_headers)
    assert res.status_code == 200 and res.json()["items"] == []
    res = await async_client.get("/api/bindings", headers=bob_headers)
    assert res.status_code == 200 and res.json()["items"] == []
    res = await async_client.get("/api/accounts", headers=alice_headers)
    assert len(res.json()["items"]) == 1
    res = await async_client.get("/api/bindings", headers=alice_headers)
    assert len(res.json()["items"]) == 1
    # admin 看全部。
    res = await async_client.get("/api/accounts", headers=admin)
    assert len(res.json()["items"]) == 1

    # bob 碰不了 alice 的账户与绑定。
    acct_id, binding_id = ids["account_id"], ids["binding_id"]
    res = await async_client.put(f"/api/accounts/{acct_id}", json={"name": "Hacked"}, headers=bob_headers)
    assert res.status_code == 403
    res = await async_client.delete(f"/api/accounts/{acct_id}", headers=bob_headers)
    assert res.status_code == 403
    res = await async_client.put(f"/api/bindings/{binding_id}", json={"riskScale": 2.0}, headers=bob_headers)
    assert res.status_code == 403
    res = await async_client.post(f"/api/bindings/{binding_id}/toggle", headers=bob_headers)
    assert res.status_code == 403
    res = await async_client.delete(f"/api/bindings/{binding_id}", headers=bob_headers)
    assert res.status_code == 403
    # bob 不能拿 alice 的账户再建绑定。
    res = await async_client.get("/api/leads", headers=bob_headers)
    lead_id = res.json()["items"][0]["id"]
    res = await async_client.post("/api/bindings", headers=bob_headers,
                                  json={"name": "Bob -> Alice acct", "lead_id": lead_id, "account_id": acct_id})
    assert res.status_code == 403
    # bob 看不到 alice 的执行记录与看板账户。
    res = await async_client.get("/api/dashboard/executions", headers=bob_headers)
    assert res.status_code == 200 and res.json()["total"] == 0
    res = await async_client.get("/api/dashboard/board", headers=bob_headers)
    assert res.status_code == 200 and res.json()["accounts"] == []
    res = await async_client.get("/api/dashboard/overview", headers=bob_headers)
    assert res.status_code == 200 and res.json()["kpis"]["totalAccounts"] == 0


@pytest.mark.asyncio
async def test_non_admin_can_manage_own_account_and_binding(async_client: AsyncClient):
    admin = await _setup_admin(async_client)
    code = await _make_invite(async_client, admin, max_uses=5)
    alice_headers = await _register_user(async_client, admin, "alice", code["code"])
    ids = await _make_lead_account_binding(async_client, alice_headers, admin)
    # 普通用户能建、改、停用自己的绑定。
    res = await async_client.put(f"/api/bindings/{ids['binding_id']}", json={"riskScale": 1.5},
                                 headers=alice_headers)
    assert res.status_code == 200 and res.json()["item"]["riskScale"] == 1.5
    res = await async_client.post(f"/api/bindings/{ids['binding_id']}/toggle", headers=alice_headers)
    assert res.status_code == 200 and res.json()["item"]["isEnabled"] is False
    # 自己的账户能改名。
    res = await async_client.put(f"/api/accounts/{ids['account_id']}", json={"name": "Alice-Acct"},
                                 headers=alice_headers)
    assert res.status_code == 200 and res.json()["item"]["name"] == "Alice-Acct"
    # 全局急停仍是 admin 专属。
    res = await async_client.post("/api/bindings/kill-switch", json={"enabled": True}, headers=alice_headers)
    assert res.status_code == 403
