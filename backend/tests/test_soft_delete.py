"""Soft delete (logical delete) for trading accounts and copy bindings.

A disabled, flat binding/account with only terminal order history can be
soft-deleted: deleted_at is stamped, the row and its entire ledger (order
intents, execution records, allocations) stay in the database, and the row
disappears from every list / trading query. Live exposure, a guard, or a
non-terminal intent still blocks deletion with 409.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.api import accounts, exposure
from app.core.database import get_session_factory
from app.core.security import CredentialVault, create_access_token
from app.models.domain import (
    CopyBinding, ExecutionRecord, LeadSource, OrderIntent, PositionAllocation,
    ProxyNode, TradingAccount, User,
)


@pytest.fixture
async def soft_delete_state(monkeypatch):
    factory = get_session_factory()
    async with factory() as db:
        admin = User(username="sd_admin", password_hash="unused", role="admin")
        proxy = ProxyNode(name="proxy", url="http://proxy.invalid:8080", is_enabled=True, is_healthy=True)
        lead = LeadSource(name="lead", portfolio_id="portfolio", is_enabled=False)
        account = TradingAccount(
            name="account", exchange="binance", environment="demo", is_enabled=False,
            api_key_masked="****", encrypted_credentials=CredentialVault().encrypt_dict({
                "api_key": "synthetic-key", "secret_key": "synthetic-secret",
            }),
        )
        db.add_all([admin, proxy, lead, account])
        await db.flush()
        binding = CopyBinding(name="binding", lead_id=lead.id, account_id=account.id, is_enabled=False)
        db.add(binding)
        await db.flush()
        await db.commit()
        ids = {"lead": lead.id, "account": account.id, "binding": binding.id}
        token = create_access_token(admin.id, admin.username, admin.role)

    adapter = SimpleNamespace(
        get_positions=AsyncMock(return_value=[]), get_open_orders=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(accounts, "create_exchange_adapter", lambda *args, **kwargs: adapter)
    monkeypatch.setattr(exposure, "create_exchange_adapter", lambda *args, **kwargs: adapter)
    return SimpleNamespace(ids=ids, token=token)


async def _add_terminal_history(binding_id, account_id, lead_id, n_intents=3):
    factory = get_session_factory()
    async with factory() as db:
        states = ["FILLED", "FILLED", "CANCELED"][:n_intents]
        for i, state in enumerate(states):
            db.add(OrderIntent(
                id=f"intent-{binding_id}-{i}", binding_id=binding_id, account_id=account_id,
                inst_id="ZECUSDT", pos_side="short", source_event_id=f"evt-{i}", state=state,
                requested_qty="0.1", filled_qty="0.1", avg_px="750", reduce_only=False,
                target_usd="75", source_time_ms=1000 + i,
            ))
        for i in range(2):
            db.add(ExecutionRecord(
                binding_id=binding_id, lead_id=lead_id, account_id=account_id,
                lead_name="lead", account_name="account", symbol="ZECUSDT", inst_id="ZECUSDT",
                side="SELL", position_side="SHORT", action="CLOSE", status="FILLED",
                lead_qty=1.0, lead_price=750.0, lead_notional=750.0,
                my_qty=0.1, my_price=750.0, my_notional=75.0, slippage_pct=0.0,
            ))
        await db.commit()


async def _add_allocation(binding_id, account_id, qty=0.5):
    factory = get_session_factory()
    async with factory() as db:
        db.add(PositionAllocation(
            binding_id=binding_id, account_id=account_id, inst_id="ZECUSDT", pos_side="short",
            owned_qty=qty, entry_cost=qty * 750.0,
        ))
        await db.commit()


async def _add_intent(binding_id, account_id, state):
    factory = get_session_factory()
    async with factory() as db:
        db.add(OrderIntent(
            id=f"intent-{binding_id}-{state}", binding_id=binding_id, account_id=account_id,
            inst_id="ZECUSDT", pos_side="short", source_event_id=f"evt-{state}", state=state,
            requested_qty="0.1", filled_qty="0", avg_px="0", reduce_only=False,
            target_usd="75", source_time_ms=2000,
        ))
        await db.commit()


async def _set_binding_enabled(binding_id, enabled):
    factory = get_session_factory()
    async with factory() as db:
        binding = await db.get(CopyBinding, binding_id)
        binding.is_enabled = enabled
        await db.commit()


def _auth(state):
    return {"Authorization": f"Bearer {state.token}"}


async def _get_binding(binding_id):
    async with get_session_factory()() as db:
        return await db.get(CopyBinding, binding_id)


async def _get_account(account_id):
    async with get_session_factory()() as db:
        return await db.get(TradingAccount, account_id)


async def _count(model, **where):
    async with get_session_factory()() as db:
        q = select(model)
        for key, value in where.items():
            q = q.where(getattr(model, key) == value)
        return len(list((await db.scalars(q)).all()))


@pytest.mark.asyncio
async def test_soft_delete_binding_with_terminal_history(async_client, soft_delete_state):
    state = soft_delete_state
    ids = state.ids
    await _add_terminal_history(ids["binding"], ids["account"], ids["lead"])

    response = await async_client.delete(f"/api/bindings/{ids['binding']}", headers=_auth(state))
    assert response.status_code == 200
    assert response.json() == {"ok": True}

    binding = await _get_binding(ids["binding"])
    assert binding is not None  # row survives: logical delete, not physical
    assert binding.deleted_at is not None
    # Ledger is retained in full.
    assert await _count(OrderIntent, binding_id=ids["binding"]) == 3
    assert await _count(ExecutionRecord, binding_id=ids["binding"]) == 2

    listed = await async_client.get("/api/bindings", headers=_auth(state))
    assert listed.status_code == 200
    assert ids["binding"] not in [item["id"] for item in listed.json()["items"]]


@pytest.mark.asyncio
async def test_soft_delete_binding_blocked_when_enabled(async_client, soft_delete_state):
    state = soft_delete_state
    await _set_binding_enabled(state.ids["binding"], True)
    response = await async_client.delete(f"/api/bindings/{state.ids['binding']}", headers=_auth(state))
    assert response.status_code == 409
    binding = await _get_binding(state.ids["binding"])
    assert binding.deleted_at is None


@pytest.mark.asyncio
async def test_soft_delete_binding_blocked_by_nonzero_allocation(async_client, soft_delete_state):
    state = soft_delete_state
    await _add_allocation(state.ids["binding"], state.ids["account"], qty=0.5)
    response = await async_client.delete(f"/api/bindings/{state.ids['binding']}", headers=_auth(state))
    assert response.status_code == 409
    binding = await _get_binding(state.ids["binding"])
    assert binding.deleted_at is None


@pytest.mark.asyncio
async def test_soft_delete_binding_blocked_by_non_terminal_intent(async_client, soft_delete_state):
    state = soft_delete_state
    await _add_intent(state.ids["binding"], state.ids["account"], "SUBMITTING")
    response = await async_client.delete(f"/api/bindings/{state.ids['binding']}", headers=_auth(state))
    assert response.status_code == 409
    binding = await _get_binding(state.ids["binding"])
    assert binding.deleted_at is None


@pytest.mark.asyncio
async def test_soft_delete_account_with_flat_binding(async_client, soft_delete_state):
    state = soft_delete_state
    ids = state.ids
    await _add_terminal_history(ids["binding"], ids["account"], ids["lead"])

    response = await async_client.delete(f"/api/accounts/{ids['account']}", headers=_auth(state))
    assert response.status_code == 200
    assert response.json() == {"ok": True}

    account = await _get_account(ids["account"])
    assert account is not None
    assert account.deleted_at is not None
    binding = await _get_binding(ids["binding"])
    assert binding is not None
    assert binding.deleted_at is not None
    # Ledger is retained in full.
    assert await _count(OrderIntent, binding_id=ids["binding"]) == 3
    assert await _count(ExecutionRecord, binding_id=ids["binding"]) == 2

    accounts = await async_client.get("/api/accounts", headers=_auth(state))
    assert accounts.status_code == 200
    assert ids["account"] not in [item["id"] for item in accounts.json()["items"]]
    bindings = await async_client.get("/api/bindings", headers=_auth(state))
    assert bindings.status_code == 200
    assert ids["binding"] not in [item["id"] for item in bindings.json()["items"]]


@pytest.mark.asyncio
async def test_soft_delete_account_blocked_when_binding_enabled(async_client, soft_delete_state):
    state = soft_delete_state
    await _set_binding_enabled(state.ids["binding"], True)
    response = await async_client.delete(f"/api/accounts/{state.ids['account']}", headers=_auth(state))
    assert response.status_code == 409
    account = await _get_account(state.ids["account"])
    assert account.deleted_at is None
    binding = await _get_binding(state.ids["binding"])
    assert binding.deleted_at is None


@pytest.mark.asyncio
async def test_soft_delete_account_blocked_when_binding_has_live_exposure(async_client, soft_delete_state):
    state = soft_delete_state
    await _add_allocation(state.ids["binding"], state.ids["account"], qty=0.5)
    response = await async_client.delete(f"/api/accounts/{state.ids['account']}", headers=_auth(state))
    assert response.status_code == 409
    account = await _get_account(state.ids["account"])
    assert account.deleted_at is None


@pytest.mark.asyncio
async def test_lead_soft_delete_with_soft_deleted_binding_retains_ledger_and_allows_recreate(async_client, soft_delete_state):
    """A lead whose binding was soft-deleted can be soft-deleted while retaining the ledger,
    disappearing from active lists, and allowing re-creation with the same portfolioId."""
    state = soft_delete_state
    ids = state.ids
    await _add_terminal_history(ids["binding"], ids["account"], ids["lead"])
    assert (await async_client.delete(f"/api/bindings/{ids['binding']}", headers=_auth(state))).status_code == 200
    response = await async_client.delete(f"/api/leads/{ids['lead']}", headers=_auth(state))
    assert response.status_code == 200
    # Ledger still survives.
    assert await _count(OrderIntent, binding_id=ids["binding"]) == 3
    listed = await async_client.get("/api/leads", headers=_auth(state))
    assert ids["lead"] not in [item["id"] for item in listed.json()["items"]]
    recreated = await async_client.post("/api/leads", json={
        "name": "lead-recreated", "portfolioId": "portfolio", "platform": "binance_bapi",
    }, headers=_auth(state))
    assert recreated.status_code == 201
    assert recreated.json()["item"]["id"] != ids["lead"]


@pytest.mark.asyncio
async def test_create_binding_after_soft_delete_allowed(async_client, soft_delete_state):
    state = soft_delete_state
    ids = state.ids
    await _add_terminal_history(ids["binding"], ids["account"], ids["lead"])
    assert (await async_client.delete(f"/api/bindings/{ids['binding']}", headers=_auth(state))).status_code == 200

    response = await async_client.post("/api/bindings", json={
        "name": "binding-2", "leadId": ids["lead"], "accountId": ids["account"], "isEnabled": False,
    }, headers=_auth(state))
    assert response.status_code == 201
    new_id = response.json()["item"]["id"]
    assert new_id != ids["binding"]

    listed = await async_client.get("/api/bindings", headers=_auth(state))
    assert [item["id"] for item in listed.json()["items"]] == [new_id]


@pytest.mark.asyncio
async def test_auto_disable_and_force_delete_and_proxy_ghost_fix(async_client, soft_delete_state, monkeypatch):
    state = soft_delete_state
    ids = state.ids
    await _set_binding_enabled(ids["binding"], True)
    async with get_session_factory()() as db:
        acct = await db.get(TradingAccount, ids["account"])
        acct.proxy_id = 1
        acct.last_sync_at = acct.created_at
        await db.commit()
    # Proxy blocked while active account references it.
    assert (await async_client.delete("/api/proxies/1", headers=_auth(state))).status_code == 409
    # Enabled binding can be deleted with auto_disable=true when flat.
    assert (await async_client.delete(f"/api/bindings/{ids['binding']}?auto_disable=true", headers=_auth(state))).status_code == 200
    # Unreachable exchange check returns EXCHANGE_UNREACHABLE_CAN_FORCE, then force=true succeeds.
    broken_adapter = SimpleNamespace(
        get_positions=AsyncMock(side_effect=RuntimeError("401 unauthorized")),
        get_open_orders=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(exposure, "create_exchange_adapter", lambda *args, **kwargs: broken_adapter)
    await _add_terminal_history(ids["binding"], ids["account"], ids["lead"])
    res = await async_client.delete(f"/api/accounts/{ids['account']}?auto_disable=true", headers=_auth(state))
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "EXCHANGE_UNREACHABLE_CAN_FORCE"
    res2 = await async_client.delete(f"/api/accounts/{ids['account']}?auto_disable=true&force=true", headers=_auth(state))
    assert res2.status_code == 200
    # Proxy can now be deleted without ghost reference from soft-deleted account!
    assert (await async_client.delete("/api/proxies/1", headers=_auth(state))).status_code == 200


@pytest.mark.asyncio
async def test_executions_status_group_counts_and_soft_deleted_binding_info(async_client, soft_delete_state):
    state = soft_delete_state
    ids = state.ids
    await _add_terminal_history(ids["binding"], ids["account"], ids["lead"])
    async with get_session_factory()() as db:
        db.add(ExecutionRecord(
            binding_id=ids["binding"], lead_id=ids["lead"], account_id=ids["account"],
            lead_name="lead", account_name="account", symbol="ETHUSDT", inst_id="ETHUSDT",
            side="SELL", position_side="SHORT", action="PARTIAL_REDUCE", status="SKIPPED_NO_LOCAL_POS",
            lead_qty=1.0, lead_price=3000.0, lead_notional=3000.0,
            my_qty=0.0, my_price=0.0, my_notional=0.0, slippage_pct=0.0,
        ))
        await db.commit()
    assert (await async_client.delete(f"/api/bindings/{ids['binding']}", headers=_auth(state))).status_code == 200
    res = await async_client.get("/api/dashboard/executions?status_group=actionable&include_counts=true", headers=_auth(state))
    assert res.status_code == 200
    body = res.json()
    assert body["counts"] == {"all": 3, "actionable": 2, "success": 2, "failed": 0, "skipped": 1, "blocked": 0}
    assert body["total"] == 2
    assert len(body["items"]) == 2
    assert body["items"][0]["bindingName"] == "binding"
    assert body["items"][0]["bindingDeleted"] is True
    assert body["items"][0]["environment"] == "demo"
