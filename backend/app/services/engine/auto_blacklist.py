"""Automatic symbol blacklisting for unowned exposure on a follower account.

The feature is scoped to one binding at a time. A symbol becomes eligible when
the account has a non-zero position or a pending order on it while *no* binding
owns any quantity of that symbol. Manually maintained blacklist entries are
never removed; only rows in ``auto_blacklist_entries`` may be auto-removed.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.domain import AutoBlacklistEntry, CopyBinding, PositionAllocation, TradingAccount, utcnow

logger = logging.getLogger("mirrorquant.auto_blacklist")

# Treat residual quantities below this as flat; matches the account-detail
# ownership view's dust tolerance.
_ZERO_TOLERANCE = Decimal("0.000001")


@dataclass(frozen=True)
class AutoBlacklistDecision:
    """Symbols to add to / remove from one binding's automatic blacklist."""

    add: tuple[str, ...]
    remove: tuple[str, ...]


def _parse_symbols(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item).strip().upper() for item in value if str(item).strip()]


def _symbol_key(value: str) -> str:
    """Collapse exchange and lead symbol spellings to one comparison key."""
    upper = (value or "").strip().upper()
    for suffix in ("-SWAP", "-PERP"):
        if upper.endswith(suffix):
            upper = upper[: -len(suffix)]
    return upper.replace("-", "").replace("_", "")


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() else None


def _owned_total(allocation: Optional[PositionAllocation]) -> Decimal:
    if allocation is None:
        return Decimal("0")
    raw = allocation.owned_qty_exact if allocation.owned_qty_exact is not None else allocation.owned_qty
    number = _decimal(raw)
    return number if number is not None and number > 0 else Decimal("0")


def _position_qty(position: Any) -> Decimal:
    value = getattr(position, "pos", None)
    if value is None and isinstance(position, dict):
        value = position.get("pos")
    number = _decimal(value)
    return abs(number) if number is not None else Decimal("0")


def _field(item: Any, *names: str) -> str:
    for name in names:
        if isinstance(item, dict):
            value = item.get(name)
        else:
            value = getattr(item, name, None)
        if value:
            return str(value)
    return ""


def _inst_id(item: Any) -> str:
    return _field(item, "inst_id", "instId", "symbol")


def _display_symbol(item: Any) -> str:
    """Prefer the lead-style symbol (BTCUSDT), not the exchange contract id."""
    return _field(item, "symbol", "inst_id", "instId").strip().upper()


def compute_auto_blacklist_changes(
    *,
    enabled: bool,
    current_blacklist: Sequence[str],
    auto_entries: Iterable[str],
    positions: Sequence[Any],
    open_orders: Sequence[Any],
    allocations: Sequence[PositionAllocation],
) -> AutoBlacklistDecision:
    """Compute one reconciliation step without touching the database.

    ``allocations`` must be scoped to the whole account, not one binding: the
    rule is that the account's symbol has no follow attribution at all.
    """
    if not enabled:
        return AutoBlacklistDecision(add=(), remove=())

    current_keys = {_symbol_key(item) for item in current_blacklist if _symbol_key(item)}
    auto_keys = {_symbol_key(item) for item in auto_entries if _symbol_key(item)}
    auto_keys &= current_keys  # safety: only markers still present are removable.

    owned_keys = {_symbol_key(allocation.inst_id) for allocation in allocations
                  if _owned_total(allocation) > _ZERO_TOLERANCE}

    physical: dict[str, str] = {}
    for position in positions:
        inst_id = _inst_id(position)
        if not inst_id or _position_qty(position) <= _ZERO_TOLERANCE:
            continue
        key = _symbol_key(inst_id)
        if key:
            physical.setdefault(key, _display_symbol(position) or inst_id.strip().upper())

    pending: dict[str, str] = {}
    for order in open_orders:
        inst_id = _inst_id(order)
        if not inst_id:
            continue
        key = _symbol_key(inst_id)
        if key:
            pending.setdefault(key, _display_symbol(order) or inst_id.strip().upper())

    exposed_keys = set(physical) | set(pending)
    unowned_keys = exposed_keys - owned_keys

    add: list[str] = []
    for key in sorted(unowned_keys):
        if key in current_keys or key in auto_keys:
            continue
        symbol = physical.get(key) or pending.get(key) or key
        if _symbol_key(symbol) and symbol not in add:
            add.append(symbol)

    remove: list[str] = []
    for key in sorted(auto_keys):
        if key in physical or key in pending:
            continue
        remove.append(key)

    return AutoBlacklistDecision(add=tuple(add), remove=tuple(remove))


async def apply_auto_blacklist_changes(
    db: AsyncSession,
    binding: CopyBinding,
    *,
    decisions: AutoBlacklistDecision,
    notify_cfg: Optional[dict[str, Any]] = None,
) -> AutoBlacklistDecision:
    """Persist decisions, keeping the JSON blacklist and marker table in sync.

    The marker row is the sole authority for automatic removal: a manually
    added symbol has no marker and is therefore never auto-removed. The
    notification joins the caller's transaction via the durable outbox.
    """
    if not decisions.add and not decisions.remove:
        return decisions

    current = _parse_symbols(binding.symbol_blacklist_json)
    current_by_key = {_symbol_key(item): item for item in current if _symbol_key(item)}
    auto_rows = list((await db.scalars(select(AutoBlacklistEntry).where(
        AutoBlacklistEntry.binding_id == binding.id
    ))).all())
    auto_by_key = {_symbol_key(row.symbol): row for row in auto_rows if _symbol_key(row.symbol)}

    applied_add: list[str] = []
    applied_remove: list[str] = []

    for symbol in decisions.add:
        normalized = str(symbol).strip().upper()
        key = _symbol_key(normalized)
        if not normalized or not key or key in current_by_key:
            continue
        current.append(normalized)
        current_by_key[key] = normalized
        if key not in auto_by_key:
            row = AutoBlacklistEntry(binding_id=binding.id, symbol=normalized, added_at=utcnow())
            db.add(row)
            auto_by_key[key] = row
        applied_add.append(normalized)

    for symbol in decisions.remove:
        key = _symbol_key(symbol)
        row = auto_by_key.get(key)
        if row is None:
            continue
        current = [item for item in current if _symbol_key(item) != key]
        current_by_key.pop(key, None)
        await db.delete(row)
        auto_by_key.pop(key, None)
        applied_remove.append(str(symbol).strip().upper())

    if not applied_add and not applied_remove:
        return AutoBlacklistDecision(add=(), remove=())

    binding.symbol_blacklist_json = json.dumps(sorted(current_by_key.values()), ensure_ascii=False)

    for symbol in applied_add:
        logger.info(
            "Auto-blacklisted %s for binding %s: unowned position or open order",
            symbol, binding.id,
        )
    for symbol in applied_remove:
        logger.info(
            "Removed auto-blacklist entry %s for binding %s: position flat and no open orders",
            symbol, binding.id,
        )

    if notify_cfg is not None:
        from app.services.notifier.outbox import enqueue_notification

        parts = []
        if applied_add:
            parts.append("自动加入黑名单 / auto-blacklisted: " + ", ".join(applied_add))
        if applied_remove:
            parts.append("自动移除黑名单 / auto-removed: " + ", ".join(applied_remove))
        enqueue_notification(
            db,
            notify_cfg,
            "MirrorQuant",
            f"绑定「{binding.name}」品种黑名单已更新 / Binding blacklist updated\n" + "\n".join(parts),
            event_type="AUTO_BLACKLIST",
            event_key=f"auto-blacklist:{binding.id}:{','.join(applied_add)}:{','.join(applied_remove)}",
            lead_id=binding.lead_id,
            owner_user_id=binding.owner_user_id,
            destination_type="system",
        )

    return AutoBlacklistDecision(add=tuple(applied_add), remove=tuple(applied_remove))


async def reconcile_auto_blacklist(
    db: AsyncSession,
    binding: CopyBinding,
    account: TradingAccount,
    *,
    positions: Sequence[Any],
    open_orders: Sequence[Any],
    notify_cfg: Optional[dict[str, Any]] = None,
) -> AutoBlacklistDecision:
    """Load account-wide ownership and apply the pure decision for one binding."""
    if not bool(getattr(binding, "auto_blacklist_manual", True)):
        return AutoBlacklistDecision(add=(), remove=())

    allocations = list((await db.scalars(select(PositionAllocation).where(
        PositionAllocation.account_id == account.id,
    ))).all())
    auto_rows = list((await db.scalars(select(AutoBlacklistEntry).where(
        AutoBlacklistEntry.binding_id == binding.id
    ))).all())

    decisions = compute_auto_blacklist_changes(
        enabled=True,
        current_blacklist=_parse_symbols(binding.symbol_blacklist_json),
        auto_entries=[row.symbol for row in auto_rows],
        positions=positions,
        open_orders=open_orders,
        allocations=allocations,
    )
    return await apply_auto_blacklist_changes(db, binding, decisions=decisions, notify_cfg=notify_cfg)
