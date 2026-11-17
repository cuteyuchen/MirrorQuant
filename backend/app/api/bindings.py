from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, get_current_user, record_audit
from app.api.exposure import require_binding_flat
from app.core.database import DEFAULT_SYSTEM_CONFIG, get_db
from app.core.secret_mask import redact_error_urls, redact_proxy_userinfo
from app.core.security import CredentialVault
from app.models.domain import (
    BindingDebt,
    CopyBinding,
    LeadSource,
    ProxyNode,
    SystemConfig,
    TradingAccount,
    User,
)
from app.schemas.api import BindingCreate, BindingUpdate, KillSwitchRequest
from app.services.adapters.factory import create_exchange_adapter
from app.services.engine.orchestrator import orchestrator
from app.services.engine.risk import RiskEngine
from app.services.engine.serialization import serialized_execution
from app.services.proxy.manager import ProxyEgressError, ProxyManager

router = APIRouter(prefix="/api/bindings", tags=["bindings"])


async def _binding_out(db: AsyncSession, b: CopyBinding) -> dict:
    lead = await db.get(LeadSource, b.lead_id)
    acct = await db.get(TradingAccount, b.account_id)
    debts = list((await db.scalars(select(BindingDebt).where(BindingDebt.binding_id == b.id))).all())
    total_debt = sum(d.pending_usd for d in debts)
    return {
        "id": b.id,
        "name": b.name,
        "leadId": b.lead_id,
        "leadName": lead.name if lead else f"Lead #{b.lead_id}",
        "accountId": b.account_id,
        "accountName": acct.name if acct else f"Account #{b.account_id}",
        "accountExchange": acct.exchange if acct else "",
        "accountEnvironment": acct.environment if acct else "",
        "isEnabled": b.is_enabled,
        "enableCircuitBreaker": b.enable_circuit_breaker,
        "enableLeverageCap": b.enable_leverage_cap,
        "riskScale": b.risk_scale,
        "maxTotalLeverage": b.max_total_leverage,
        "maxSingleLeverage": b.max_single_leverage,
        "maxInstrumentLeverage": b.max_instrument_leverage,
        "maxDrawdownPct": b.max_drawdown_pct,
        "dailyMaxLossPct": getattr(b, "daily_max_loss_pct", 10.0),
        "maxSlippagePct": b.max_slippage_pct,
        "maxSpreadPct": getattr(b, "max_spread_pct", 0.3),
        "iocOffsetPct": b.ioc_offset_pct,
        "orderMode": b.order_mode,
        "slippageAction": getattr(b, "slippage_action", "block"),
        "limitOrderTtlSec": getattr(b, "limit_order_ttl_sec", 120),
        "tpSlMode": getattr(b, "tp_sl_mode", "follow_lead"),
        "enableStopLoss": bool(getattr(b, "enable_stop_loss", False)),
        "stopLossPct": getattr(b, "stop_loss_pct", 5.0),
        "enableTakeProfit": bool(getattr(b, "enable_take_profit", False)),
        "takeProfitPct": getattr(b, "take_profit_pct", 10.0),
        "enableTrailingStop": bool(getattr(b, "enable_trailing_stop", False)),
        "trailingCallbackPct": getattr(b, "trailing_callback_pct", 2.0),
        "hardStopLossPct": getattr(b, "hard_stop_loss_pct", 15.0),
        "symbolWhitelist": json.loads(b.symbol_whitelist_json or "[]"),
        "symbolBlacklist": json.loads(b.symbol_blacklist_json or "[]"),
        "symbolMap": json.loads(b.symbol_map_json or "{}"),
        "circuitBreakerTripped": b.circuit_breaker_tripped,
        "circuitBreakerReason": redact_error_urls(b.circuit_breaker_reason) if b.circuit_breaker_reason else None,
        "consecutiveFailures": getattr(b, "consecutive_failures", 0),
        "baselineSeeded": b.baseline_seeded,
        "adoptExistingPositions": getattr(b, "adopt_existing_positions", True),
        "adoptTolerancePct": getattr(b, "adopt_tolerance_pct", 10.0),
        "adoptionCompleted": getattr(b, "adoption_completed", False),
        "pendingDebtUsd": round(total_debt, 2),
        "debts": [
            {"symbol": d.symbol, "positionSide": d.position_side, "pendingUsd": round(d.pending_usd, 2)}
            for d in debts
            if d.pending_usd > 0
        ],
    }


@router.get("")
async def list_bindings(db: AsyncSession = Depends(get_db), _user: User = Depends(get_current_user)):
    rows = list((await db.scalars(select(CopyBinding).order_by(CopyBinding.id))).all())
    items = [await _binding_out(db, b) for b in rows]
    ks = await db.scalar(select(SystemConfig).where(SystemConfig.key == "global_kill_switch"))
    return {
        "items": items,
        "globalKillSwitch": json.loads(ks.value_json) if ks else False,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_binding(
    payload: BindingCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    lead = await db.get(LeadSource, payload.lead_id)
    acct = await db.get(TradingAccount, payload.account_id)
    if not lead or not acct:
        raise HTTPException(status_code=404, detail="Lead or TradingAccount not found")

    dup = await db.scalar(
        select(CopyBinding).where(
            CopyBinding.lead_id == payload.lead_id,
            CopyBinding.account_id == payload.account_id,
        )
    )
    if dup:
        raise HTTPException(status_code=409, detail="Binding for this Lead and Account already exists")

    binding = CopyBinding(
        name=payload.name.strip(),
        lead_id=payload.lead_id,
        account_id=payload.account_id,
        is_enabled=payload.is_enabled,
        enable_circuit_breaker=payload.enable_circuit_breaker,
        enable_leverage_cap=payload.enable_leverage_cap,
        risk_scale=payload.risk_scale,
        max_total_leverage=payload.max_total_leverage,
        max_single_leverage=payload.max_single_leverage,
        max_instrument_leverage=payload.max_instrument_leverage,
        max_drawdown_pct=payload.max_drawdown_pct,
        daily_max_loss_pct=payload.daily_max_loss_pct,
        max_slippage_pct=payload.max_slippage_pct,
        max_spread_pct=payload.max_spread_pct,
        ioc_offset_pct=payload.ioc_offset_pct,
        order_mode=payload.order_mode,
        slippage_action=payload.slippage_action,
        limit_order_ttl_sec=payload.limit_order_ttl_sec,
        adopt_existing_positions=payload.adopt_existing_positions,
        adopt_tolerance_pct=payload.adopt_tolerance_pct,
        tp_sl_mode=payload.tp_sl_mode,
        enable_stop_loss=payload.enable_stop_loss,
        stop_loss_pct=payload.stop_loss_pct,
        enable_take_profit=payload.enable_take_profit,
        take_profit_pct=payload.take_profit_pct,
        enable_trailing_stop=payload.enable_trailing_stop,
        trailing_callback_pct=payload.trailing_callback_pct,
        hard_stop_loss_pct=payload.hard_stop_loss_pct,
        symbol_whitelist_json=json.dumps([s.upper() for s in payload.symbol_whitelist]),
        symbol_blacklist_json=json.dumps([s.upper() for s in payload.symbol_blacklist]),
        symbol_map_json=json.dumps(payload.symbol_map),
    )
    db.add(binding)
    await db.flush()
    await record_audit(
        db, "CREATE_BINDING", "binding", str(binding.id), f"Bound {lead.name} -> {acct.name}", current_user, request
    )
    await db.commit()
    await db.refresh(binding)
    return {"item": await _binding_out(db, binding)}


@router.put("/{binding_id}")
@serialized_execution
async def update_binding(
    binding_id: int,
    payload: BindingUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    binding = await db.get(CopyBinding, binding_id)
    if not binding:
        raise HTTPException(status_code=404, detail="Binding not found")

    data = payload.model_dump(exclude_unset=True)
    protection_fields = {"symbol_map", "tp_sl_mode", "enable_stop_loss", "stop_loss_pct", "enable_take_profit",
                         "take_profit_pct", "enable_trailing_stop", "trailing_callback_pct", "hard_stop_loss_pct"}
    changed_protection = any(
        (json.loads(binding.symbol_map_json or "{}") if field == "symbol_map" else getattr(binding, field)) != data[field]
        for field in protection_fields.intersection(data)
    )
    if changed_protection:
        await require_binding_flat(db, binding)
    if "symbol_whitelist" in data:
        binding.symbol_whitelist_json = json.dumps([s.upper() for s in (data.pop("symbol_whitelist") or [])])
    if "symbol_blacklist" in data:
        binding.symbol_blacklist_json = json.dumps([s.upper() for s in (data.pop("symbol_blacklist") or [])])
    if "symbol_map" in data:
        binding.symbol_map_json = json.dumps(data.pop("symbol_map") or {})
    for k, v in data.items():
        setattr(binding, k, v)
    RiskEngine.clear_disabled_loss_breaker(binding)

    await record_audit(db, "UPDATE_BINDING", "binding", str(binding.id), f"Updated binding {binding.name}", current_user, request)
    await db.commit()
    await db.refresh(binding)
    return {"item": await _binding_out(db, binding)}


@router.delete("/{binding_id}")
@serialized_execution
async def delete_binding(
    binding_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    binding = await db.get(CopyBinding, binding_id)
    if not binding:
        raise HTTPException(status_code=404, detail="Binding not found")
    if binding.is_enabled:
        raise HTTPException(status_code=409, detail="Disable the binding and drain exposure before deletion")
    await require_binding_flat(db, binding, deleting=True)
    name = binding.name
    await db.delete(binding)
    await record_audit(db, "DELETE_BINDING", "binding", str(binding_id), f"Deleted binding {name}", current_user, request)
    await db.commit()
    return {"ok": True}


@router.post("/{binding_id}/toggle")
@serialized_execution
async def toggle_binding(
    binding_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    binding = await db.get(CopyBinding, binding_id)
    if not binding:
        raise HTTPException(status_code=404, detail="Binding not found")
    binding.is_enabled = not binding.is_enabled
    await record_audit(
        db, "TOGGLE_BINDING", "binding", str(binding.id), f"Set enabled={binding.is_enabled}", current_user, request
    )
    await db.commit()
    return {"item": await _binding_out(db, binding)}


@router.post("/{binding_id}/reset-breaker")
@serialized_execution  # 与引擎下单共用锁，避免重置覆盖并发失败计数。
async def reset_circuit_breaker(
    binding_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    binding = await db.get(CopyBinding, binding_id)
    if not binding:
        raise HTTPException(status_code=404, detail="Binding not found")
    binding.circuit_breaker_tripped = False
    binding.circuit_breaker_reason = None
    binding.consecutive_failures = 0
    await record_audit(db, "RESET_BREAKER", "binding", str(binding.id), "Circuit breaker reset", current_user, request)
    await db.commit()
    return {"item": await _binding_out(db, binding)}


@router.post("/{binding_id}/reset-debt")
@serialized_execution  # 债务浮点展示值和精确值在同一执行边界清零。
async def reset_debt(
    binding_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    binding = await db.get(CopyBinding, binding_id)
    if not binding:
        raise HTTPException(status_code=404, detail="Binding not found")
    debts = list((await db.scalars(select(BindingDebt).where(BindingDebt.binding_id == binding_id))).all())
    for d in debts:
        d.pending_usd = 0.0
        d.pending_usd_exact = "0"
    await record_audit(db, "RESET_DEBT", "binding", str(binding.id), "Pending debt reset", current_user, request)
    await db.commit()
    return {"item": await _binding_out(db, binding)}


@router.post("/{binding_id}/adopt-existing")
@serialized_execution  # 与引擎下单共用锁，认领记账不与成交记账交错。
async def adopt_existing_positions(
    binding_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    """手动认领：把账户里与带单员持仓一致的已有仓位记为跟单仓位，只改账，不下单。"""
    binding = await db.get(CopyBinding, binding_id)
    if not binding:
        raise HTTPException(status_code=404, detail="Binding not found")
    account = await db.get(TradingAccount, binding.account_id)
    lead = await db.get(LeadSource, binding.lead_id)
    if not account or not lead:
        raise HTTPException(status_code=404, detail="Lead or TradingAccount not found")
    if not binding.is_enabled or not account.is_enabled:
        raise HTTPException(status_code=409, detail="Binding or account is disabled")
    try:
        snapshot = json.loads(lead.snapshot_json or "{}")
    except (ValueError, TypeError):
        snapshot = {}
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("openPositions"), list):
        raise HTTPException(status_code=409, detail="Lead snapshot unavailable; wait for the next sync")
    vault = CredentialVault()
    creds = vault.decrypt_dict(account.encrypted_credentials)
    proxies = list((await db.scalars(select(ProxyNode).order_by(ProxyNode.priority))).all())
    try:
        proxy_url = ProxyManager.select_proxy_url(proxies, account.proxy_id, exchange=account.exchange)
    except ProxyEgressError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    adapter = create_exchange_adapter(
        account_id=account.id,
        exchange=account.exchange,
        credentials=creds,
        environment=account.environment,
        proxy_url=proxy_url,
        margin_mode=account.margin_mode,
        position_mode=account.position_mode,
    )
    try:
        instruments = await adapter.get_instruments()
        notify_cfg = await orchestrator.router._get_config(db, "notifications", DEFAULT_SYSTEM_CONFIG["notifications"])
        lead_margin_balance = (snapshot.get("profile") or {}).get("marginBalance") or lead.margin_balance or 0
        adopted = await orchestrator.router._adopt_matching_positions(
            db, binding, lead, account, adapter, instruments, snapshot,
            lead_margin_balance, int(time.time() * 1000), notify_cfg)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=redact_proxy_userinfo(str(exc))[:400]) from exc
    await record_audit(db, "ADOPT_EXISTING", "binding", str(binding.id),
                       f"Adopted {len(adopted)} existing position(s)", current_user, request)
    await db.commit()
    return {"item": await _binding_out(db, binding), "adopted": adopted}


@router.post("/kill-switch")
@serialized_execution
async def set_global_kill_switch(
    payload: KillSwitchRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    row = await db.scalar(select(SystemConfig).where(SystemConfig.key == "global_kill_switch"))
    if row is None:
        row = SystemConfig(key="global_kill_switch", value_json=json.dumps(payload.enabled))
        db.add(row)
    else:
        row.value_json = json.dumps(payload.enabled)
    await record_audit(
        db,
        "GLOBAL_KILL_SWITCH",
        "system",
        "global_kill_switch",
        f"Global kill switch set to {payload.enabled}",
        current_user,
        request,
    )
    await db.commit()
    return {"globalKillSwitch": payload.enabled}
