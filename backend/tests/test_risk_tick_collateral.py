from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.services.engine.orchestrator import EngineOrchestrator


@pytest.fixture(autouse=True)
def setup_test_db():
    yield


@pytest.mark.asyncio
async def test_risk_tick_checks_position_guards_when_collateral_check_returns_false(monkeypatch):
    reason = "Invalid exchange collateral balance; entries suspended"
    binding = SimpleNamespace(
        id=1, lead_id=2, account_id=3,
        circuit_breaker_tripped=True, circuit_breaker_reason=reason,
    )
    lead = SimpleNamespace(id=2, status="error", snapshot_json="{}")
    account = SimpleNamespace(
        id=3, exchange="binance", encrypted_credentials="unused",
        proxy_id=None, environment="demo", margin_mode="cross",
        position_mode="hedge", clock_offset_ms=0, status="error",
    )
    positions = [{"instId": "BTCUSDT", "posSide": "long", "pos": 1}]
    instruments = {"BTCUSDT": object()}
    adapter = SimpleNamespace(
        get_positions=AsyncMock(return_value=positions),
        get_instruments=AsyncMock(return_value=instruments),
    )
    db = AsyncMock()
    db.__aenter__.return_value = db
    db.scalars.side_effect = [
        Mock(all=Mock(return_value=[binding.id])),
        Mock(all=Mock(return_value=[])),
    ]
    db.get.side_effect = [binding, lead, account]
    monkeypatch.setattr(
        "app.services.engine.orchestrator.get_session_factory",
        Mock(return_value=Mock(return_value=db)),
    )
    monkeypatch.setattr(
        "app.services.engine.orchestrator.create_exchange_adapter",
        Mock(return_value=adapter),
    )
    monkeypatch.setattr(
        "app.services.engine.orchestrator.ProxyManager.select_proxy_url",
        Mock(return_value=None),
    )
    engine = EngineOrchestrator.__new__(EngineOrchestrator)
    engine.vault = SimpleNamespace(decrypt_dict=Mock(return_value={}))
    engine.router = SimpleNamespace(
        _get_config=AsyncMock(return_value={"reconcile_interval_sec": 60}),
        check_position_guards=AsyncMock(),
        # 风控轮询新增的“在簿限价单”检查：本用例没有挂单。
        _has_resting=AsyncMock(return_value=False),
        reconcile_orphan_positions=AsyncMock(),
    )
    engine._last_reconcile = {}
    engine._check_account_equity = AsyncMock(return_value=False)

    await engine.risk_tick()

    engine._check_account_equity.assert_awaited_once_with(db, binding, account, adapter)
    engine.router.check_position_guards.assert_awaited_once_with(
        db=db, lead=lead, account=account, binding=binding, adapter=adapter,
        local_positions=positions, instruments=instruments,
    )
    assert binding.circuit_breaker_tripped is True
    assert binding.circuit_breaker_reason == reason
    assert account.status == "error"
    db.rollback.assert_not_awaited()
