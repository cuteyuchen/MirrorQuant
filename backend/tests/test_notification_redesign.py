"""Tests for notification system redesign: per-lead configs, multi-user subscriptions,
lead activity notifications, and append account execution.
"""
import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.database import DEFAULT_SYSTEM_CONFIG, get_session_factory
from app.core.security import CredentialVault
from app.models.domain import (
    CopyBinding,
    ExecutionRecord,
    LeadSource,
    NotificationOutbox,
    SystemConfig,
    TradingAccount,
    User,
    UserLeadSubscription,
    UserPreference,
)
from app.services.engine.router import CopyRouter
from app.services.notifier.outbox import NotificationOutboxDrainer, enqueue_notification
from app.services.notifier.service import NotificationService
from app.services.notifier.trade import (
    lead_only_message,
    merged_trade_message,
    resolve_display_unit,
    trade_message,
    trade_sections,
)


@pytest.mark.asyncio
async def test_notification_formatting_lead_only_and_appended():
    """Verify message composition for lead-only vs lead+follower."""
    source_order = {
        "symbol": "BTCUSDT",
        "side": "SELL",
        "positionSide": "SHORT",
        "qty": "0.1086",
        "avgPrice": "64083.5",
        "notional": "6959.46",
    }
    lead = LeadSource(id=1, name="Aoying", platform="binance_bapi", margin_balance=50000.0)

    # 1. Lead-only message: no follower execution
    lead_only = lead_only_message(source_order, lead.margin_balance, lead=lead, display_unit="both")
    assert "Aoying" in lead_only
    assert "BTCUSDT" in lead_only
    assert "做空" in lead_only
    assert "跟随" not in lead_only
    assert "我的账户" not in lead_only

    # 2. Appended message: with follower execution
    account = TradingAccount(id=3, name="Account #3", exchange="binance")
    record = ExecutionRecord(
        id=101,
        lead_id=1,
        account_id=3,
        symbol="BTCUSDT",
        side="SELL",
        position_side="SHORT",
        action="OPEN_SHORT",
        status="EXECUTED",
        lead_qty=0.1086,
        lead_price=64083.5,
        lead_notional=6959.46,
        my_qty=0.0543,
        my_price=64085.0,
        my_notional=3479.81,
    )
    full_msg = trade_message(record, account, source_order, lead.margin_balance, lead=lead, display_unit="both")
    assert "Aoying" in full_msg
    assert "做空" in full_msg
    assert "跟单账户 #3" in full_msg


@pytest.mark.asyncio
async def test_notification_preferences_and_subscriptions_api(async_client: AsyncClient):
    """Test user preferences and subscriptions CRUD endpoints."""
    # 1. Setup admin
    res = await async_client.post(
        "/api/auth/setup",
        json={"username": "superadmin", "password": "SecurePassword2026!"},
    )
    assert res.status_code == 201
    token = res.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 2. Get default preferences (falls back to system notifications)
    res = await async_client.get("/api/notifications/preferences", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert "notifications" in data
    assert data["hasCustomNotifications"] is False

    # 3. Update user preferences
    custom_notifications = {
        "dingtalk_enabled": True,
        "dingtalk_webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=custom-secret-token",
        "display_unit": "notional",
        "notify_on_lead_order": True,
        "notify_on_execution": True,
    }
    res = await async_client.put(
        "/api/notifications/preferences",
        headers=headers,
        json={"notifications": custom_notifications},
    )
    assert res.status_code == 200
    updated = res.json()
    assert updated["hasCustomNotifications"] is True
    assert updated["displayUnit"] == "notional"
    assert updated["notifications"]["dingtalk_webhook_url"] == "••••"

    # 4. Create a Lead with per-lead notification config
    res = await async_client.post(
        "/api/leads",
        headers=headers,
        json={
            "name": "Alpha-Lead",
            "platform": "binance_bapi",
            "portfolioId": "alpha123456",
            "notificationConfig": {
                "dingtalk_enabled": True,
                "dingtalk_webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=lead-channel-token",
                "notify_on_lead_order": True,
            },
        },
    )
    assert res.status_code == 201
    lead_id = res.json()["item"]["id"]
    assert res.json()["item"]["notificationConfig"]["dingtalk_webhook_url"] == "••••"

    # 5. List subscriptions (should include Alpha-Lead with is_enabled=False initially)
    res = await async_client.get("/api/notifications/subscriptions", headers=headers)
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) >= 1
    lead_sub = next(i for i in items if i["leadId"] == lead_id)
    assert lead_sub["leadName"] == "Alpha-Lead"
    assert lead_sub["isEnabled"] is False

    # 6. Subscribe to Alpha-Lead
    res = await async_client.put(
        "/api/notifications/subscriptions",
        headers=headers,
        json={"subscriptions": [{"leadId": lead_id, "isEnabled": True}]},
    )
    assert res.status_code == 200
    lead_sub = next(i for i in res.json()["items"] if i["leadId"] == lead_id)
    assert lead_sub["isEnabled"] is True


@pytest.mark.asyncio
async def test_lead_test_notification_endpoint(async_client: AsyncClient, monkeypatch):
    """Test POST /api/leads/{id}/test-notify endpoint."""
    res = await async_client.post(
        "/api/auth/setup",
        json={"username": "superadmin", "password": "SecurePassword2026!"},
    )
    token = res.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Create lead with notification config
    res = await async_client.post(
        "/api/leads",
        headers=headers,
        json={
            "name": "Test-Lead-Notify",
            "platform": "binance_bapi",
            "portfolioId": "testleadnotify123",
            "notificationConfig": {
                "webhook_enabled": True,
                "webhook_url": "https://webhook.test/lead",
            },
        },
    )
    lead_id = res.json()["item"]["id"]

    dispatch_mock = AsyncMock(return_value={"webhook": True})
    monkeypatch.setattr(NotificationService, "dispatch", dispatch_mock)

    res = await async_client.post(f"/api/leads/{lead_id}/test-notify", headers=headers)
    assert res.status_code == 200
    assert res.json()["ok"] is True
    dispatch_mock.assert_awaited_once()
    called_cfg = dispatch_mock.call_args[0][0]
    assert called_cfg["webhook_url"] == "https://webhook.test/lead"


@pytest.mark.asyncio
async def test_lead_activity_notifications_without_bindings(monkeypatch):
    """When a lead has NO bindings, lead trade activity still triggers notifications:
    - Lead's own channel (if configured)
    - Subscribed users (lead-only, no follow info)
    - System fallback (if notify_on_lead_order is enabled)
    """
    dispatch_mock = AsyncMock(return_value={"mock": True})
    monkeypatch.setattr(NotificationService, "dispatch", dispatch_mock)

    async with get_session_factory()() as db:
        # Create user
        user = User(username="subscriber1", password_hash="dummy", role="user")
        db.add(user)
        await db.flush()

        # User preferences: webhook enabled
        user_pref = UserPreference(
            user_id=user.id,
            notifications_json=json.dumps({
                "webhook_enabled": True,
                "webhook_url": "https://subscriber1.test/hook",
                "notify_on_lead_order": True,
            }),
        )
        db.add(user_pref)

        # Create lead with its own notification config
        lead = LeadSource(
            name="Aoying",
            platform="binance_bapi",
            portfolio_id="aoying-123",
            seen_order_keys_json=json.dumps(["historical_key_1"]),
            notification_config_json=json.dumps({
                "webhook_enabled": True,
                "webhook_url": "https://aoying-lead.test/hook",
                "notify_on_lead_order": True,
            }),
        )
        db.add(lead)
        await db.flush()

        # User subscribes to Aoying
        sub = UserLeadSubscription(user_id=user.id, lead_id=lead.id, is_enabled=True)
        db.add(sub)
        await db.commit()

        router = CopyRouter(CredentialVault())

        now_ms = int(time.time() * 1000)
        snapshot = {
            "profile": {"marginBalance": 100000},
            "openPositions": [],
            "openPositionsMeta": {
                "source": "api",
                "authoritative": True,
                "complete": True,
                "positionShow": True,
                "asOf": now_ms,
            },
            "sourceTimeMs": now_ms,
        }
        lead.snapshot_json = json.dumps(snapshot)
        await db.commit()

        # Simulate new lead event (new trade)
        raw_orders = [{
            "orderId": 999991,
            "orderTime": now_ms,
            "orderUpdateTime": now_ms,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "positionSide": "SHORT",
            "type": "MARKET",
            "executedQty": "0.5",
            "avgPrice": "65000",
        }]

        # Process events with 0 bindings
        await router.process_lead_events(db, lead, raw_orders, [], snapshot, [])

    # Verify Outbox rows
    async with get_session_factory()() as db:
        outbox_rows = list((await db.scalars(select(NotificationOutbox))).all())
        assert len(outbox_rows) == 2  # 1 for lead channel, 1 for subscribed user

        lead_row = next(r for r in outbox_rows if r.destination_type == "lead")
        assert lead_row.lead_id == lead.id
        payload_lead = json.loads(lead_row.payload)
        assert "Aoying" in payload_lead["message"]
        assert "做空" in payload_lead["message"]
        assert "跟随" not in payload_lead["message"]

        user_row = next(r for r in outbox_rows if r.destination_type == "user")
        assert user_row.owner_user_id == user.id
        payload_user = json.loads(user_row.payload)
        assert "Aoying" in payload_user["message"]
        assert "做空" in payload_user["message"]
        assert "跟随" not in payload_user["message"]

    # Drain and verify delivery URLs
    drainer = NotificationOutboxDrainer(poll_interval=1.0)
    await drainer.drain_once()
    assert dispatch_mock.await_count == 2
    urls = [call[0][0]["webhook_url"] for call in dispatch_mock.call_args_list]
    assert "https://aoying-lead.test/hook" in urls
    assert "https://subscriber1.test/hook" in urls


@pytest.mark.asyncio
async def test_lead_initial_sync_does_not_fire_historical_notifications():
    """Initial sync of a new lead seeds keys but does NOT emit notifications for historical orders."""
    async with get_session_factory()() as db:
        lead = LeadSource(
            name="New-Lead",
            platform="binance_bapi",
            portfolio_id="newlead999",
            seen_order_keys_json=None,  # Not yet seeded
            notification_config_json=json.dumps({
                "webhook_enabled": True,
                "webhook_url": "https://newlead.test/hook",
            }),
        )
        db.add(lead)
        await db.commit()

        now_ms = int(time.time() * 1000)
        snapshot = {
            "profile": {"marginBalance": 50000},
            "openPositions": [],
            "openPositionsMeta": {
                "source": "api",
                "authoritative": True,
                "complete": True,
                "positionShow": True,
                "asOf": now_ms,
            },
            "sourceTimeMs": now_ms,
        }
        lead.snapshot_json = json.dumps(snapshot)
        await db.commit()

        router = CopyRouter(CredentialVault())
        historical_orders = [{
            "orderId": 111,
            "orderTime": now_ms,
            "orderUpdateTime": now_ms,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "positionSide": "LONG",
            "type": "MARKET",
            "executedQty": "1.0",
            "avgPrice": "60000",
        }]

        await router.process_lead_events(db, lead, historical_orders, [], snapshot, [])

    async with get_session_factory()() as db:
        count = await db.scalar(select(NotificationOutbox))
        assert count is None  # Zero notifications enqueued on baseline seeding


@pytest.mark.asyncio
async def test_append_account_execution_and_tenant_isolation(monkeypatch):
    """When a user copies a lead, the notification merges the lead trade AND their account execution.
    A different subscribed user without a binding receives ONLY the lead trade.
    """
    dispatch_mock = AsyncMock(return_value={"mock": True})
    monkeypatch.setattr(NotificationService, "dispatch", dispatch_mock)

    async with get_session_factory()() as db:
        user_a = User(username="user_copier", password_hash="dummy", role="user")
        user_b = User(username="user_watcher", password_hash="dummy", role="user")
        db.add_all([user_a, user_b])
        await db.flush()

        # User A has custom webhook
        pref_a = UserPreference(
            user_id=user_a.id,
            notifications_json=json.dumps({
                "webhook_enabled": True,
                "webhook_url": "https://copier.test/hook",
                "notify_on_execution": True,
            }),
        )
        # User B has custom webhook
        pref_b = UserPreference(
            user_id=user_b.id,
            notifications_json=json.dumps({
                "webhook_enabled": True,
                "webhook_url": "https://watcher.test/hook",
                "notify_on_lead_order": True,
            }),
        )
        db.add_all([pref_a, pref_b])

        # Lead Aoying
        lead = LeadSource(
            name="Aoying",
            platform="binance_bapi",
            portfolio_id="aoying-multi",
            seen_order_keys_json=json.dumps(["seeded_key"]),
        )
        db.add(lead)
        await db.flush()

        # User B is subscribed to Aoying (no binding)
        sub_b = UserLeadSubscription(user_id=user_b.id, lead_id=lead.id, is_enabled=True)
        # User A is also subscribed to Aoying (has binding)
        sub_a = UserLeadSubscription(user_id=user_a.id, lead_id=lead.id, is_enabled=True)

        # User A has an account and binding
        account_a = TradingAccount(
            name="User A Account #1",
            exchange="binance",
            api_key_masked="***",
            encrypted_credentials="test-placeholder",
            owner_user_id=user_a.id,
        )
        db.add(account_a)
        await db.flush()

        binding_a = CopyBinding(
            name="User A Binding",
            lead_id=lead.id,
            account_id=account_a.id,
            owner_user_id=user_a.id,
            baseline_seeded=True,
        )
        db.add_all([sub_a, sub_b, binding_a])
        await db.commit()

        router = CopyRouter(CredentialVault())

        # Simulate delivery and execution for User A
        event_id = "test_event_100:0.2"
        from app.models.domain import BindingDelivery
        delivery_a = BindingDelivery(
            binding_id=binding_a.id,
            source_event_id=event_id,
            source_sequence=1700000000000,
            payload_json=json.dumps({
                "symbol": "BTCUSDT",
                "side": "SELL",
                "positionSide": "SHORT",
                "qty": "0.2",
                "avgPrice": "64000",
                "_event_id": event_id,
            }),
            state="PENDING",
        )
        db.add(delivery_a)
        await db.flush()

        db.info["delivery"] = delivery_a
        db.info["merge_row_ids"] = {}

        # Record User A's execution
        await router._record_execution(
            db, binding_a, lead, account_a,
            "BTCUSDT", "BTCUSDT", "SELL", "SHORT", "OPEN_SHORT", "EXECUTED",
            0.2, 64000, 12800,
            my_qty=0.1, my_price=64002, my_notional=6400.2,
            notify_cfg=json.loads(pref_a.notifications_json),
        )

        # Dispatch lead activity notifications for this event
        new_events = {
            event_id: {
                "symbol": "BTCUSDT",
                "side": "SELL",
                "positionSide": "SHORT",
                "qty": "0.2",
                "avgPrice": "64000",
            }
        }
        await router._dispatch_lead_activity_notifications(
            db, lead, new_events, 50000, DEFAULT_SYSTEM_CONFIG["notifications"]
        )
        await router._seal_merged_notifications(db)

    # Now verify outbox content
    async with get_session_factory()() as db:
        rows = list((await db.scalars(select(NotificationOutbox))).all())
        assert len(rows) == 2

        user_a_row = next(r for r in rows if r.owner_user_id == user_a.id)
        user_b_row = next(r for r in rows if r.owner_user_id == user_b.id)

        # User A's row has follower section
        p_a = json.loads(user_a_row.payload)
        assert p_a.get("follower_section") is not None
        assert "跟单账户 #1" in p_a["follower_section"]

        # User B's row has NO follower section
        p_b = json.loads(user_b_row.payload)
        assert p_b.get("follower_section") is None
        assert "跟随" not in p_b["message"]

    # Drain once and check that dispatch delivered merged messages correctly to both webhooks
    drainer = NotificationOutboxDrainer(poll_interval=1.0)
    await drainer.drain_once()

    assert dispatch_mock.await_count == 2
    calls = {call[0][0]["webhook_url"]: call[0][2] for call in dispatch_mock.call_args_list}

    msg_a = calls["https://copier.test/hook"]
    msg_b = calls["https://watcher.test/hook"]

    # User A's notification has both lead section and execution result appended
    assert "Aoying" in msg_a
    assert "跟单账户 #1" in msg_a

    # User B's notification contains ONLY lead activity
    assert "Aoying" in msg_b
    assert "跟单账户 #1" not in msg_b
