"""Catch-up policy persistence, bounds, and compatibility with pinned legacy values."""
import json

import pytest
from sqlalchemy import select

from app.core.database import DEFAULT_SYSTEM_CONFIG, get_session_factory, init_db
from app.models.domain import SystemConfig
from app.schemas.api import ResilienceSettings
from app.services.engine.risk import RiskEngine
from app.services.engine.router import CopyRouter


@pytest.fixture
async def settings_client(async_client):
    response = await async_client.post(
        "/api/auth/setup",
        json={"username": "settings_admin", "password": "settings-test-password"},
    )
    assert response.status_code == 201
    return async_client


@pytest.mark.asyncio
@pytest.mark.parametrize("max_age, max_drift", [(900, 0.5), (120, 0.8), (10, 0.01), (11, 0.05), (3600, 10)])
async def test_catchup_settings_round_trip(settings_client, max_age, max_drift):
    payload = {"missed_order_max_age_sec": max_age, "catchup_max_price_dev_pct": max_drift}
    response = await settings_client.put("/api/settings", json={"resilience": payload})
    assert response.status_code == 200
    for key, value in payload.items():
        assert response.json()["resilience"][key] == value

    response = await settings_client.get("/api/settings")
    assert response.status_code == 200
    async with get_session_factory()() as db:
        row = await db.scalar(select(SystemConfig).where(SystemConfig.key == "resilience"))
        stored = json.loads(row.value_json)
        routed = await CopyRouter._get_config(db, "resilience", DEFAULT_SYSTEM_CONFIG["resilience"])
        for key, value in payload.items():
            assert response.json()["resilience"][key] == stored[key] == routed[key] == value


@pytest.mark.asyncio
@pytest.mark.parametrize("field, value", [
    ("missed_order_max_age_sec", 9),
    ("missed_order_max_age_sec", 3601),
    ("missed_order_max_age_sec", 10.5),
    ("catchup_max_price_dev_pct", 0),
    ("catchup_max_price_dev_pct", -0.1),
    ("catchup_max_price_dev_pct", 10.1),
    ("catchup_max_price_dev_pct", "NaN"),
    ("catchup_max_price_dev_pct", "Infinity"),
])
async def test_catchup_settings_reject_values_outside_schema(settings_client, field, value):
    response = await settings_client.put("/api/settings", json={"resilience": {field: value}})
    assert response.status_code == 422


def test_catchup_schema_and_database_defaults_agree():
    settings = ResilienceSettings()
    assert settings.missed_order_max_age_sec == DEFAULT_SYSTEM_CONFIG["resilience"]["missed_order_max_age_sec"] == 900
    assert settings.catchup_max_price_dev_pct == DEFAULT_SYSTEM_CONFIG["resilience"]["catchup_max_price_dev_pct"] == 0.5


@pytest.mark.asyncio
async def test_partial_put_preserves_operator_pinned_catchup_values(settings_client):
    """A partial PUT must not reset keys the operator never sent back to defaults."""
    pinned = {
        **DEFAULT_SYSTEM_CONFIG["resilience"],
        "missed_order_max_age_sec": 120,
        "catchup_max_price_dev_pct": 0.8,
        "reconcile_interval_sec": 45,
    }
    async with get_session_factory()() as db:
        row = await db.scalar(select(SystemConfig).where(SystemConfig.key == "resilience"))
        row.value_json = json.dumps(pinned)
        await db.commit()

    # Operator edits only the age ceiling.
    response = await settings_client.put("/api/settings", json={"resilience": {"missed_order_max_age_sec": 300}})
    assert response.status_code == 200
    returned = response.json()["resilience"]
    assert returned["missed_order_max_age_sec"] == 300
    # Untouched keys keep the pinned values instead of snapping to 900 / 0.5.
    assert returned["catchup_max_price_dev_pct"] == 0.8
    assert returned["reconcile_interval_sec"] == 45

    async with get_session_factory()() as db:
        row = await db.scalar(select(SystemConfig).where(SystemConfig.key == "resilience"))
        stored = json.loads(row.value_json)
        assert stored["missed_order_max_age_sec"] == 300
        assert stored["catchup_max_price_dev_pct"] == 0.8
        assert stored["reconcile_interval_sec"] == 45
        routed = await CopyRouter._get_config(db, "resilience", DEFAULT_SYSTEM_CONFIG["resilience"])
        assert routed["catchup_max_price_dev_pct"] == 0.8
        assert routed["missed_order_max_age_sec"] == 300


@pytest.mark.asyncio
async def test_legacy_catchup_settings_are_not_rewritten_and_keep_their_policy(settings_client):
    legacy = {
        **DEFAULT_SYSTEM_CONFIG["resilience"],
        "missed_order_max_age_sec": 120,
        "catchup_max_price_dev_pct": 0.8,
        "max_consecutive_failures": 9,
        "flip_close_first": True,
    }
    raw = json.dumps(legacy, indent=2)
    async with get_session_factory()() as db:
        row = await db.scalar(select(SystemConfig).where(SystemConfig.key == "resilience"))
        row.value_json = raw
        await db.commit()

    response = await settings_client.get("/api/settings")
    assert response.status_code == 200
    assert response.json()["resilience"] == legacy
    async with get_session_factory()() as db:
        policy = await CopyRouter._get_config(db, "resilience", DEFAULT_SYSTEM_CONFIG["resilience"])
        assert policy == legacy
        row = await db.scalar(select(SystemConfig).where(SystemConfig.key == "resilience"))
        assert row.value_json == raw

    # Startup may fill missing settings but must never migrate an operator's pinned policy.
    await init_db()
    async with get_session_factory()() as db:
        row = await db.scalar(select(SystemConfig).where(SystemConfig.key == "resilience"))
        assert row.value_json == raw

    now_ms = 1_000_000

    def check(age_sec, ref_price):
        return RiskEngine.check_missed_order_catchup(
            order_time_ms=now_ms - age_sec * 1000,
            now_ms=now_ms,
            max_age_sec=policy["missed_order_max_age_sec"],
            lead_avg_price=100,
            ref_price=ref_price,
            catchup_max_price_dev_pct=policy["catchup_max_price_dev_pct"],
        )

    assert check(150, 100).code == "SKIPPED_STALE_MISSED_ORDER"
    assert check(60, 100.6).allowed
    assert check(60, 100.9).code == "BLOCKED_CATCHUP_DRIFT"
