"""Admin user management: list (with follow summary), detail, role/status,
password reset, and guarded soft delete.

Delete gate (user-approved): a user cannot be deleted while any of their
bindings is still enabled, any follow allocation is non-zero, or any of
their accounts still holds an open position. The admin must first close all
copy trading. On success the user is soft-deleted together with their
accounts and bindings; the ledger (intents, executions, deliveries) is kept,
same as the 1.3.1 account/binding soft delete.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_admin, record_audit
from app.core.database import get_db
from app.core.security import hash_password_async
from app.models.domain import (
    CopyBinding,
    LeadSource,
    PositionAllocation,
    TradingAccount,
    User,
    UserLeadSubscription,
    utcnow,
)
from app.schemas.api import AdminUserSummary, ResetPasswordRequest, UserPatchRequest

router = APIRouter(prefix="/api/admin/users", tags=["admin-users"])


def _side_label(pos_side: str) -> str:
    return "多" if str(pos_side).lower().startswith("long") else "空"


def _fmt_qty(qty: float) -> str:
    return f"{abs(qty):g}"


async def _follow_summary(db: AsyncSession, user_id: int) -> dict:
    """Follow-activity summary for one user (non-deleted accounts/bindings only)."""
    account_count = await db.scalar(
        select(func.count(TradingAccount.id)).where(
            TradingAccount.owner_user_id == user_id, TradingAccount.deleted_at.is_(None)))
    binding_count = await db.scalar(
        select(func.count(CopyBinding.id)).where(
            CopyBinding.owner_user_id == user_id, CopyBinding.deleted_at.is_(None)))
    enabled_binding_count = await db.scalar(
        select(func.count(CopyBinding.id)).where(
            CopyBinding.owner_user_id == user_id, CopyBinding.deleted_at.is_(None),
            CopyBinding.is_enabled == True))  # noqa: E712
    open_position_count = await db.scalar(
        select(func.count(PositionAllocation.id))
        .join(CopyBinding, CopyBinding.id == PositionAllocation.binding_id)
        .where(CopyBinding.owner_user_id == user_id, CopyBinding.deleted_at.is_(None),
               PositionAllocation.owned_qty != 0))
    total_equity = await db.scalar(
        select(func.coalesce(func.sum(TradingAccount.usdt_equity), 0.0)).where(
            TradingAccount.owner_user_id == user_id, TradingAccount.deleted_at.is_(None)))
    return {
        "account_count": int(account_count or 0),
        "binding_count": int(binding_count or 0),
        "enabled_binding_count": int(enabled_binding_count or 0),
        "open_position_count": int(open_position_count or 0),
        "total_equity": round(float(total_equity or 0.0), 2),
    }


def _summary_out(user: User, summary: dict) -> dict:
    return AdminUserSummary(
        id=user.id,
        username=user.username,
        role=user.role,
        is_active=user.is_active,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
        account_count=summary["account_count"],
        binding_count=summary["binding_count"],
        enabled_binding_count=summary["enabled_binding_count"],
        open_position_count=summary["open_position_count"],
        total_equity=summary["total_equity"],
    ).model_dump(mode="json", by_alias=True)


async def _get_live_user(db: AsyncSession, user_id: int) -> User:
    user = await db.get(User, user_id)
    if user is None or user.deleted_at is not None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


async def _admin_count(db: AsyncSession) -> int:
    return int(await db.scalar(
        select(func.count(User.id)).where(
            User.role == "admin", User.is_active == True, User.deleted_at.is_(None))) or 0)  # noqa: E712


async def _delete_blockers(db: AsyncSession, user_id: int) -> list[str]:
    """Human-readable list of live follow activity blocking a user delete."""
    blockers: list[str] = []
    enabled = list((await db.scalars(select(CopyBinding).where(
        CopyBinding.owner_user_id == user_id, CopyBinding.deleted_at.is_(None),
        CopyBinding.is_enabled == True))).all())  # noqa: E712
    for binding in enabled:
        blockers.append(f"绑定 #{binding.id}（{binding.name}）仍启用")
    allocations = list((await db.scalars(
        select(PositionAllocation)
        .join(CopyBinding, CopyBinding.id == PositionAllocation.binding_id)
        .where(CopyBinding.owner_user_id == user_id, CopyBinding.deleted_at.is_(None),
               PositionAllocation.owned_qty != 0))).all())
    for alloc in allocations:
        blockers.append(
            f"{alloc.inst_id} {_side_label(alloc.pos_side)} {_fmt_qty(alloc.owned_qty)} 未平")
    accounts = list((await db.scalars(select(TradingAccount).where(
        TradingAccount.owner_user_id == user_id, TradingAccount.deleted_at.is_(None)))).all())
    for account in accounts:
        positions = []
        if account.positions_json:
            try:
                positions = [p for p in json.loads(account.positions_json) if isinstance(p, dict)]
            except Exception:
                positions = []
        for pos in positions:
            try:
                qty = float(pos.get("pos") or 0)
            except (TypeError, ValueError):
                continue
            if qty != 0:
                blockers.append(
                    f"账户「{account.name}」仍有 {pos.get('instId')} "
                    f"{_side_label(str(pos.get('posSide') or ''))} {_fmt_qty(qty)} 未平仓")
    return blockers


@router.get("")
async def list_users(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_admin),
):
    rows = list((await db.scalars(select(User).where(
        User.deleted_at.is_(None)).order_by(User.id))).all())
    user_ids = [user.id for user in rows]
    acct_stats: dict[int, tuple[int, float]] = {}
    bind_stats: dict[int, tuple[int, int]] = {}
    pos_stats: dict[int, int] = {}
    if user_ids:
        for uid, cnt, eq in (await db.execute(
            select(
                TradingAccount.owner_user_id,
                func.count(TradingAccount.id),
                func.coalesce(func.sum(TradingAccount.usdt_equity), 0.0),
            )
            .where(TradingAccount.owner_user_id.in_(user_ids), TradingAccount.deleted_at.is_(None))
            .group_by(TradingAccount.owner_user_id)
        )).all():
            if uid is not None:
                acct_stats[uid] = (int(cnt or 0), round(float(eq or 0.0), 2))
        for uid, cnt, enabled_cnt in (await db.execute(
            select(
                CopyBinding.owner_user_id,
                func.count(CopyBinding.id),
                func.coalesce(func.sum(case((CopyBinding.is_enabled.is_(True), 1), else_=0)), 0),
            )
            .where(CopyBinding.owner_user_id.in_(user_ids), CopyBinding.deleted_at.is_(None))
            .group_by(CopyBinding.owner_user_id)
        )).all():
            if uid is not None:
                bind_stats[uid] = (int(cnt or 0), int(enabled_cnt or 0))
        for uid, cnt in (await db.execute(
            select(CopyBinding.owner_user_id, func.count(PositionAllocation.id))
            .join(CopyBinding, CopyBinding.id == PositionAllocation.binding_id)
            .where(
                CopyBinding.owner_user_id.in_(user_ids),
                CopyBinding.deleted_at.is_(None),
                PositionAllocation.owned_qty != 0,
            )
            .group_by(CopyBinding.owner_user_id)
        )).all():
            if uid is not None:
                pos_stats[uid] = int(cnt or 0)
    items = []
    for user in rows:
        acct_cnt, eq = acct_stats.get(user.id, (0, 0.0))
        bind_cnt, enabled_cnt = bind_stats.get(user.id, (0, 0))
        items.append(_summary_out(user, {
            "account_count": acct_cnt,
            "binding_count": bind_cnt,
            "enabled_binding_count": enabled_cnt,
            "open_position_count": pos_stats.get(user.id, 0),
            "total_equity": eq,
        }))
    return {"items": items}


@router.get("/{user_id}")
async def get_user_detail(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_admin),
):
    target = await _get_live_user(db, user_id)
    summary = await _follow_summary(db, target.id)
    accounts = list((await db.scalars(select(TradingAccount).where(
        TradingAccount.owner_user_id == target.id, TradingAccount.deleted_at.is_(None))
        .order_by(TradingAccount.id))).all())
    bindings = list((await db.scalars(select(CopyBinding).where(
        CopyBinding.owner_user_id == target.id, CopyBinding.deleted_at.is_(None))
        .order_by(CopyBinding.id))).all())
    binding_ids = [b.id for b in bindings]
    allocations = list((await db.scalars(select(PositionAllocation).where(
        PositionAllocation.binding_id.in_(binding_ids),
        PositionAllocation.owned_qty != 0))).all()) if binding_ids else []
    alloc_by_binding: dict[int, list[dict]] = {}
    for alloc in allocations:
        alloc_by_binding.setdefault(alloc.binding_id, []).append({
            "instId": alloc.inst_id, "posSide": alloc.pos_side, "qty": alloc.owned_qty,
        })
    leads = {lead.id: lead for lead in
             list((await db.scalars(select(LeadSource))).all())}
    accounts_by_id = {a.id: a for a in accounts}
    subscriptions = list((await db.scalars(select(UserLeadSubscription).where(
        UserLeadSubscription.user_id == target.id))).all())
    return {
        "user": _summary_out(target, summary),
        "accounts": [
            {"id": a.id, "name": a.name, "exchange": a.exchange, "environment": a.environment,
             "status": a.status, "isEnabled": a.is_enabled, "equity": round(a.usdt_equity, 2)}
            for a in accounts
        ],
        "bindings": [
            {"id": b.id, "name": b.name, "leadId": b.lead_id,
             "leadName": leads[b.lead_id].name if b.lead_id in leads else f"Lead #{b.lead_id}",
             "accountId": b.account_id,
             "accountName": accounts_by_id[b.account_id].name if b.account_id in accounts_by_id else f"Account #{b.account_id}",
             "isEnabled": b.is_enabled,
             "openPositions": alloc_by_binding.get(b.id, [])}
            for b in bindings
        ],
        "subscriptions": [
            {"leadId": s.lead_id,
             "leadName": leads[s.lead_id].name if s.lead_id in leads else f"Lead #{s.lead_id}",
             "isEnabled": s.is_enabled}
            for s in subscriptions
        ],
    }


@router.patch("/{user_id}")
async def update_user(
    user_id: int,
    payload: UserPatchRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    target = await _get_live_user(db, user_id)
    if payload.role is None and payload.is_active is None:
        raise HTTPException(status_code=400, detail="Nothing to update")
    if payload.role is not None and payload.role not in ("user", "admin"):
        raise HTTPException(status_code=400, detail="Role must be 'user' or 'admin'")
    # 永远不能留下零个管理员；停用自己也一律拒绝。
    if payload.is_active is False and target.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot deactivate your own account")
    if (payload.role == "user" and target.role == "admin") or (
            payload.is_active is False and target.role == "admin"):
        if await _admin_count(db) <= 1:
            raise HTTPException(status_code=409, detail="Cannot remove the last administrator")
    changes = []
    if payload.role is not None and payload.role != target.role:
        changes.append(f"role {target.role} -> {payload.role}")
        target.role = payload.role
    if payload.is_active is not None and payload.is_active != target.is_active:
        changes.append(f"is_active -> {payload.is_active}")
        target.is_active = payload.is_active
        if payload.is_active is False:
            # 停用即吊销现有会话。
            target.session_version += 1
    if not changes:
        return {"user": _summary_out(target, await _follow_summary(db, target.id))}
    await record_audit(db, "UPDATE_USER", "user", str(target.id),
                       f"Updated {target.username}: {'; '.join(changes)}", current_user, request)
    await db.commit()
    await db.refresh(target)
    return {"user": _summary_out(target, await _follow_summary(db, target.id))}


@router.post("/{user_id}/reset-password")
async def reset_user_password(
    user_id: int,
    payload: ResetPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    target = await _get_live_user(db, user_id)
    if target.id == current_user.id:
        raise HTTPException(
            status_code=400, detail="Use change-password for your own account")
    target.password_hash = await hash_password_async(payload.new_password)
    # 吊销该用户现有会话，改密后须重新登录。
    target.session_version += 1
    await record_audit(db, "RESET_PASSWORD", "user", str(target.id),
                       f"Admin reset password for {target.username}", current_user, request)
    await db.commit()
    return {"ok": True}


@router.delete("/{user_id}")
async def delete_user(
    user_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    target = await _get_live_user(db, user_id)
    if target.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    if target.role == "admin" and await _admin_count(db) <= 1:
        raise HTTPException(status_code=409, detail="Cannot delete the last administrator")
    blockers = await _delete_blockers(db, target.id)
    if blockers:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="用户仍有未关闭的跟单：请先关闭全部跟单再删除；" + "；".join(blockers),
        )
    now = utcnow()
    target.deleted_at = now
    # 吊销该用户现有会话。
    target.session_version += 1
    accounts = list((await db.scalars(select(TradingAccount).where(
        TradingAccount.owner_user_id == target.id, TradingAccount.deleted_at.is_(None)))).all())
    for account in accounts:
        account.deleted_at = now
    bindings = list((await db.scalars(select(CopyBinding).where(
        CopyBinding.owner_user_id == target.id, CopyBinding.deleted_at.is_(None)))).all())
    for binding in bindings:
        binding.deleted_at = now
    await record_audit(db, "DELETE_USER", "user", str(target.id),
                       f"Soft-deleted user {target.username} with {len(accounts)} account(s) "
                       f"and {len(bindings)} binding(s)", current_user, request)
    await db.commit()
    return {"ok": True}
