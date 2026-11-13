import pytest

from app.core.database import get_session_factory
from app.core.security import CredentialVault, create_access_token
from app.models.domain import TradingAccount, User


@pytest.fixture
async def account_client(async_client):
    async with get_session_factory()() as db:
        user = User(username="account-admin", password_hash="unused", role="admin")
        account = TradingAccount(
            name="Original account", exchange="binance", environment="demo",
            encrypted_credentials=CredentialVault().encrypt_dict({"api_key": "synthetic-key", "secret_key": "synthetic-secret"}),
            api_key_masked="masked", is_enabled=False,
        )
        db.add_all([user, account])
        await db.commit()
        token = create_access_token(user.id, user.username, user.role)
        account_id = account.id
    async_client.headers["Authorization"] = f"Bearer {token}"
    return async_client, account_id


@pytest.mark.parametrize("field", ["name", "environment", "apiKey", "secretKey", "isEnabled", "marginMode", "positionMode"])
@pytest.mark.asyncio
async def test_account_update_rejects_explicit_null(account_client, field):
    client, account_id = account_client
    response = await client.put(f"/api/accounts/{account_id}", json={field: None})
    assert response.status_code == 422
    assert "cannot be null" in response.text
    async with get_session_factory()() as db:
        account = await db.get(TradingAccount, account_id)
        assert account.name == "Original account" and account.environment == "demo"
        assert CredentialVault().decrypt_dict(account.encrypted_credentials)["api_key"] == "synthetic-key"


@pytest.mark.asyncio
async def test_account_partial_update_keeps_omitted_values(account_client):
    client, account_id = account_client
    assert (await client.put(f"/api/accounts/{account_id}", json={})).status_code == 200
    response = await client.put(f"/api/accounts/{account_id}", json={"name": "Renamed account"})
    assert response.status_code == 200
    assert response.json()["item"]["name"] == "Renamed account"
    async with get_session_factory()() as db:
        account = await db.get(TradingAccount, account_id)
        assert account.environment == "demo" and account.is_enabled is False
        assert CredentialVault().decrypt_dict(account.encrypted_credentials) == {
            "api_key": "synthetic-key", "secret_key": "synthetic-secret",
        }


@pytest.mark.asyncio
async def test_account_nullable_proxy_can_be_explicitly_cleared(account_client):
    client, account_id = account_client
    response = await client.put(f"/api/accounts/{account_id}", json={"proxyId": None})
    assert response.status_code == 200
    assert response.json()["item"]["proxyId"] is None
