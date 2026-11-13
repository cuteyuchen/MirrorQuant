from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, get_current_user, record_audit
from app.api.exposure import require_account_flat
from app.core.database import get_db
from app.core.security import CredentialVault
from app.core.secret_mask import mask_sensitive_payload, redact_proxy_userinfo
from app.models.domain import ProxyNode, TradingAccount, User
from app.schemas.api import AccountCreate, AccountUpdate
from app.services.adapters.factory import create_exchange_adapter
from app.services.engine.orchestrator import orchestrator
from app.services.engine.serialization import serialized_execution
from app.services.proxy.manager import ProxyEgressError, ProxyManager

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


def _account_out(a: TradingAccount) -> dict:
    positions = []
    if a.positions_json:
        try:
            positions = json.loads(a.positions_json)
        except Exception:
            pass
    return {
        "id": a.id,
        "name": a.name,
        "exchange": a.exchange,
        "environment": a.environment,
        "apiKeyMasked": a.api_key_masked,
        "proxyId": a.proxy_id,
        "isEnabled": a.is_enabled,
        "marginMode": a.margin_mode,
        "positionMode": a.position_mode,
        "usdtEquity": round(a.usdt_equity, 2),
        "availEquity": round(a.avail_equity, 2),
        "hwmEquity": round(a.hwm_equity, 2),
        "dayStartEquity": round(getattr(a, "day_start_equity", 0.0) or 0.0, 2),
        "clockOffsetMs": int(getattr(a, "clock_offset_ms", 0) or 0),
        "totalNotional": round(a.total_notional, 2),
        "unrealizedPnl": round(a.unrealized_pnl, 2),
        "positions": mask_sensitive_payload(positions),
        "lastSyncAt": a.last_sync_at.isoformat() if a.last_sync_at else None,
        "lastError": redact_proxy_userinfo(a.last_error) if a.last_error else None,
        "status": a.status,
    }


@router.get("")
async def list_accounts(db: AsyncSession = Depends(get_db), _user: User = Depends(get_current_user)):
    rows = list((await db.scalars(select(TradingAccount).order_by(TradingAccount.id))).all())
    return {"items": [_account_out(a) for a in rows]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_account(
    payload: AccountCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    if payload.exchange in ("okx", "bitget") and not (payload.passphrase or "").strip():
        raise HTTPException(
            status_code=400,
            detail=f"{payload.exchange.upper()} accounts require an API passphrase",
        )

    vault = CredentialVault()
    creds = {
        "api_key": payload.api_key.strip(),
        "secret_key": payload.secret_key.strip(),
        "passphrase": (payload.passphrase or "").strip(),
    }
    encrypted = vault.encrypt_dict(creds)
    acct = TradingAccount(
        name=payload.name.strip(),
        exchange=payload.exchange,
        environment=payload.environment,
        encrypted_credentials=encrypted,
        api_key_masked=vault.mask_secret(creds["api_key"]),
        proxy_id=payload.proxy_id,
        is_enabled=payload.is_enabled,
        margin_mode=payload.margin_mode,
        position_mode=payload.position_mode,
    )
    db.add(acct)
    await db.flush()
    await record_audit(
        db,
        "CREATE_ACCOUNT",
        "account",
        str(acct.id),
        f"Created {acct.exchange} ({acct.environment}) account {acct.name}",
        current_user,
        request,
    )
    await db.commit()
    await db.refresh(acct)
    return {"item": _account_out(acct)}


@router.put("/{account_id}")
@serialized_execution
async def update_account(
    account_id: int,
    payload: AccountUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    acct = await db.get(TradingAccount, account_id)
    if not acct:
        raise HTTPException(status_code=404, detail="Account not found")

    vault = CredentialVault()
    data = payload.model_dump(exclude_unset=True)
    identity_fields = {"environment", "api_key", "secret_key", "passphrase", "margin_mode", "position_mode"}
    if any(field in {"api_key", "secret_key", "passphrase"} or getattr(acct, field) != data[field]
           for field in identity_fields.intersection(data)):
        if acct.is_enabled:
            raise HTTPException(status_code=409, detail="Disable the account and drain exposure before changing account identity or mode")
        await require_account_flat(db, acct)
    if any(k in data and data[k] for k in ("api_key", "secret_key", "passphrase")):
        current_creds = vault.decrypt_dict(acct.encrypted_credentials)
        if data.get("api_key"):
            current_creds["api_key"] = data["api_key"].strip()
            acct.api_key_masked = vault.mask_secret(current_creds["api_key"])
        if data.get("secret_key"):
            current_creds["secret_key"] = data["secret_key"].strip()
        if data.get("passphrase") is not None:
            current_creds["passphrase"] = data["passphrase"].strip()
        acct.encrypted_credentials = vault.encrypt_dict(current_creds)

    for field in ("name", "environment", "proxy_id", "is_enabled", "margin_mode", "position_mode"):
        if field in data:
            setattr(acct, field, data[field])

    await record_audit(db, "UPDATE_ACCOUNT", "account", str(acct.id), f"Updated account {acct.name}", current_user, request)
    await db.commit()
    await db.refresh(acct)
    return {"item": _account_out(acct)}


@router.delete("/{account_id}")
@serialized_execution
async def delete_account(
    account_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    acct = await db.get(TradingAccount, account_id)
    if not acct:
        raise HTTPException(status_code=404, detail="Account not found")
    if acct.is_enabled:
        raise HTTPException(status_code=409, detail="Disable the account and drain exposure before deletion")
    await require_account_flat(db, acct, deleting=True)
    name = acct.name
    await db.delete(acct)
    await record_audit(db, "DELETE_ACCOUNT", "account", str(account_id), f"Deleted account {name}", current_user, request)
    await db.commit()
    return {"ok": True}


@router.post("/{account_id}/test")
async def test_account_connectivity(
    account_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_admin),
):
    acct = await db.get(TradingAccount, account_id)
    if not acct:
        raise HTTPException(status_code=404, detail="Account not found")

    vault = CredentialVault()
    creds = vault.decrypt_dict(acct.encrypted_credentials)
    proxies = list((await db.scalars(select(ProxyNode).order_by(ProxyNode.priority))).all())
    try:
        proxy_url = ProxyManager.select_proxy_url(proxies, acct.proxy_id)
    except ProxyEgressError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    adapter = create_exchange_adapter(
        exchange=acct.exchange,
        credentials=creds,
        environment=acct.environment,
        proxy_url=proxy_url,
        margin_mode=acct.margin_mode,
        position_mode=acct.position_mode,
    )
    try:
        res = await adapter.test_connection()
        acct.status = "connected"
        acct.last_error = None
        acct.usdt_equity = float(res.get("usdtEquity") or 0.0)
        acct.avail_equity = float(res.get("availEquity") or 0.0)
        if acct.usdt_equity > acct.hwm_equity:
            acct.hwm_equity = acct.usdt_equity
        await db.commit()
        return mask_sensitive_payload(res)
    except Exception as exc:
        acct.status = "error"
        acct.last_error = redact_proxy_userinfo(str(exc))[:480]
        await db.commit()
        return {"ok": False, "error": redact_proxy_userinfo(str(exc))}


@router.post("/{account_id}/sync")
async def sync_account_now(
    account_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_admin),
):
    acct = await db.get(TradingAccount, account_id)
    if not acct:
        raise HTTPException(status_code=404, detail="Account not found")
    res = await orchestrator.sync_account(db, acct)
    await db.refresh(acct)
    return mask_sensitive_payload({"item": _account_out(acct), **res})
