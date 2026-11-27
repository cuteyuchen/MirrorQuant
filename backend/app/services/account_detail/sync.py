"""Idempotent, rate-limit-aware, per-account coalesced dataset sync and backfill."""
from __future__ import annotations

import asyncio
from decimal import Decimal
import json
import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session_factory
from app.core.security import CredentialVault
from app.models.domain import (
    AccountCashflowEntry,
    AccountDatasetSyncState,
    AccountEquitySnapshot,
    AccountOpenOrderSnapshot,
    AccountOrderHistory,
    AccountTradeFill,
    CopyBinding,
    ExecutionRecord,
    OrderIntent,
    ProxyNode,
    TradingAccount,
)
from app.services.adapters.base import (
    CashflowEntry,
    CommissionRateInfo,
    DailyAccountSnapshotEntry,
    DatasetPage,
    ExchangeAPIError,
    ForceOrderEntry,
    OpenOrderEntry,
    OrderHistoryEntry,
    TradeFill,
)
from app.services.adapters.factory import create_exchange_adapter
from app.services.account_detail.alerts import persist_account_alerts
from app.services.account_detail.attribution import reconcile_attribution
from app.services.account_detail.serializers import sanitize_error_text
from app.services.account_detail.snapshots import beijing_day_start_ms, capture_equity_snapshot
from app.services.proxy.manager import ProxyManager

logger = logging.getLogger("mirrorquant.account_detail.sync")

WINDOW_MS = 7 * 86_400_000 - 1
OVERLAP_MS = 86_400_000
DAY_MS = 86_400_000

DATASET_CADENCE_SEC = {
    "open_orders": 20,
    "cashflow": 300,
    "fills": 300,
    "orders": 300,
    "force_orders": 3600,
    "commission_rates": 3600,
    "daily_snapshots": 86400,
}

DATASET_MAX_BACKFILL_DAYS = {
    "cashflow": 365,
    "force_orders": 365,
    "orders": 90,
    "fills": 90,
    "daily_snapshots": 30,
}

# Account-detail history is useful but must never starve the trading path.
# Core live data (open orders / equity snapshots) keeps its cadence for every
# enabled account. Historical datasets run one-at-a-time, one window per
# background cycle, and inactive accounts (no enabled binding) use a longer
# cadence instead of being paused.
LOW_PRIORITY_DATASETS = (
    "cashflow",
    "orders",
    "fills",
    "force_orders",
    "commission_rates",
    "daily_snapshots",
)
INACTIVE_LOW_PRIORITY_MULTIPLIER = 6
RATE_LIMIT_BACKOFF_SEC = 900
OPEN_ORDERS_RATE_LIMIT_BACKOFF_SEC = 120
BACKGROUND_MAX_WINDOWS = 1
MANUAL_MAX_WINDOWS = 8
WINDOW_PAUSE_SEC = 0.2

_RATE_LIMIT_CODES = frozenset({"-1003", "-1015", "429", "50011", "50061", "10006", "10018", "RATE_LIMITED"})

# Per-account coalescing maps keyed by (loop_id, account_id, scope)
_in_flight_syncs: Dict[Tuple[int, int, str], asyncio.Task] = {}
_account_locks: Dict[Tuple[int, int], asyncio.Lock] = {}


def _is_rate_limit_error(exc: Exception) -> bool:
    if isinstance(exc, ExchangeAPIError) and str(exc.code) in _RATE_LIMIT_CODES:
        return True
    msg = str(exc).lower()
    return "429" in msg or "rate limit" in msg or "cooldown" in msg


def _error_code_of(exc: Exception) -> str:
    if isinstance(exc, ExchangeAPIError):
        return str(exc.code)[:64]
    return type(exc).__name__[:64]


def _get_account_lock(account_id: int) -> asyncio.Lock:
    loop_id = id(asyncio.get_running_loop())
    key = (loop_id, account_id)
    lock = _account_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _account_locks[key] = lock
    return lock


async def _account_has_enabled_binding(db: AsyncSession, account_id: int) -> bool:
    binding_id = await db.scalar(
        select(CopyBinding.id)
        .where(
            CopyBinding.account_id == account_id,
            CopyBinding.is_enabled == True,  # noqa: E712
            CopyBinding.deleted_at.is_(None),
        )
        .limit(1)
    )
    return binding_id is not None


def _effective_cadence_sec(dataset: str, *, active: bool) -> int:
    cadence = DATASET_CADENCE_SEC[dataset]
    if not active and dataset in LOW_PRIORITY_DATASETS:
        return cadence * INACTIVE_LOW_PRIORITY_MULTIPLIER
    return cadence


def _adapter_cooldown_remaining(adapter: Any) -> float:
    try:
        until_map = getattr(adapter, "_rate_limit_until", None)
        key_fn = getattr(adapter, "_rate_limit_key", None)
        if not isinstance(until_map, dict) or not callable(key_fn):
            return 0.0
        return max(0.0, float(until_map.get(key_fn(True), 0.0)) - time.monotonic())
    except Exception:
        return 0.0


async def _get_or_create_sync_state(
    db: AsyncSession, account_id: int, dataset: str
) -> AccountDatasetSyncState:
    row = await db.scalar(
        select(AccountDatasetSyncState).where(
            AccountDatasetSyncState.account_id == account_id,
            AccountDatasetSyncState.dataset == dataset,
        )
    )
    if row is None:
        row = AccountDatasetSyncState(
            account_id=account_id,
            dataset=dataset,
            status="unavailable",
            record_count=0,
        )
        db.add(row)
        await db.flush()
    return row


async def _upsert_cashflows(db: AsyncSession, account_id: int, items: Sequence[CashflowEntry]) -> int:
    if not items:
        return 0
    existing_ext: Dict[Tuple[Optional[str], str], AccountCashflowEntry] = {}
    existing_dedupe: Dict[str, AccountCashflowEntry] = {}
    dedupe_keys = [item.dedupe_key for item in items]
    external_ids = [item.external_id for item in items if item.external_id is not None]
    filters = [AccountCashflowEntry.dedupe_key.in_(dedupe_keys)]
    if external_ids:
        filters.append(AccountCashflowEntry.external_id.in_(external_ids))
    rows = list(
        (
            await db.scalars(
                select(AccountCashflowEntry).where(
                    AccountCashflowEntry.account_id == account_id,
                    or_(*filters),
                )
            )
        ).all()
    )
    for row in rows:
        if row.external_id is not None:
            existing_ext[(row.external_id, row.raw_type)] = row
        existing_dedupe[row.dedupe_key] = row

    inserted = 0
    for item in items:
        if item.external_id is not None and (item.external_id, item.raw_type) in existing_ext:
            continue
        if item.dedupe_key in existing_dedupe:
            continue
        row = AccountCashflowEntry(
            account_id=account_id,
            exchange=item.exchange,
            external_id=item.external_id,
            dedupe_key=item.dedupe_key,
            asset=item.asset,
            raw_type=item.raw_type,
            category=item.category,
            signed_amount=item.signed_amount,
            symbol=item.symbol,
            occurred_at_ms=item.occurred_at_ms,
            source=item.source,
            metadata_json=json.dumps(item.metadata or {}, separators=(",", ":")),
        )
        db.add(row)
        if item.external_id is not None:
            existing_ext[(item.external_id, item.raw_type)] = row
        existing_dedupe[item.dedupe_key] = row
        inserted += 1
    await db.flush()
    return inserted


async def _upsert_fills(db: AsyncSession, account_id: int, items: Sequence[TradeFill]) -> int:
    if not items:
        return 0
    fill_ids = [item.exchange_fill_id for item in items]
    existing = {
        row.exchange_fill_id: row
        for row in (
            await db.scalars(
                select(AccountTradeFill).where(
                    AccountTradeFill.account_id == account_id,
                    AccountTradeFill.exchange_fill_id.in_(fill_ids),
                )
            )
        ).all()
    }
    inserted = 0
    for item in items:
        if item.exchange_fill_id in existing:
            continue
        row = AccountTradeFill(
            account_id=account_id,
            exchange=item.exchange,
            exchange_fill_id=item.exchange_fill_id,
            exchange_order_id=item.exchange_order_id,
            client_order_id=item.client_order_id,
            symbol=item.symbol,
            inst_id=item.inst_id,
            side=item.side,
            position_side=item.position_side,
            qty=item.qty,
            price=item.price,
            quote_qty=item.quote_qty,
            realized_pnl=item.realized_pnl,
            commission_amount=item.commission_amount,
            commission_asset=item.commission_asset,
            maker=item.maker,
            occurred_at_ms=item.occurred_at_ms,
            source_class="unknown",
            attribution_confidence="unknown",
            metadata_json=json.dumps(item.metadata or {}, separators=(",", ":")),
        )
        db.add(row)
        existing[item.exchange_fill_id] = row
        inserted += 1
    await db.flush()
    return inserted


async def _upsert_orders(
    db: AsyncSession, account_id: int, items: Sequence[OrderHistoryEntry | ForceOrderEntry]
) -> int:
    if not items:
        return 0
    dedupe_keys = [item.dedupe_key for item in items]
    order_ids = [item.exchange_order_id for item in items if item.exchange_order_id]
    algo_ids = [item.algo_id for item in items if getattr(item, "algo_id", None)]
    filters = [AccountOrderHistory.dedupe_key.in_(dedupe_keys)]
    if order_ids:
        filters.append(AccountOrderHistory.exchange_order_id.in_(order_ids))
    if algo_ids:
        filters.append(AccountOrderHistory.algo_id.in_(algo_ids))
    rows = list(
        (
            await db.scalars(
                select(AccountOrderHistory).where(
                    AccountOrderHistory.account_id == account_id,
                    or_(*filters),
                )
            )
        ).all()
    )
    by_dedupe = {r.dedupe_key: r for r in rows}
    by_oid = {r.exchange_order_id: r for r in rows if r.exchange_order_id is not None}
    by_aid = {r.algo_id: r for r in rows if r.algo_id is not None}
    changed = 0

    for item in items:
        if isinstance(item, ForceOrderEntry):
            existing = (
                by_oid.get(item.exchange_order_id) if item.exchange_order_id else None
            ) or by_dedupe.get(item.dedupe_key)
            if existing is None:
                row = AccountOrderHistory(
                    account_id=account_id,
                    exchange=item.exchange,
                    dedupe_key=item.dedupe_key,
                    exchange_order_id=item.exchange_order_id,
                    client_order_id=None,
                    algo_id=None,
                    client_algo_id=None,
                    category="force_order",
                    symbol=item.symbol,
                    inst_id=item.inst_id,
                    side=item.side,
                    position_side=item.position_side,
                    order_type=item.order_type,
                    status=item.status,
                    qty=item.qty,
                    filled_qty=item.filled_qty,
                    avg_price=item.avg_price,
                    limit_price=None,
                    trigger_price=None,
                    reduce_only=True,
                    close_position=None,
                    working_type=None,
                    source_class="unknown",
                    created_at_ms=item.occurred_at_ms,
                    updated_at_ms=item.occurred_at_ms,
                    metadata_json=json.dumps(item.metadata or {}, separators=(",", ":")),
                )
                db.add(row)
                by_dedupe[item.dedupe_key] = row
                if item.exchange_order_id:
                    by_oid[item.exchange_order_id] = row
                changed += 1
            else:
                existing.category = "force_order"
                existing.status = item.status
                existing.filled_qty = item.filled_qty
                existing.avg_price = item.avg_price
                existing.updated_at_ms = max(existing.updated_at_ms, item.occurred_at_ms)
            continue

        existing = (
            (by_oid.get(item.exchange_order_id) if item.exchange_order_id else None)
            or (by_aid.get(item.algo_id) if item.algo_id else None)
            or by_dedupe.get(item.dedupe_key)
        )
        if existing is None:
            row = AccountOrderHistory(
                account_id=account_id,
                exchange=item.exchange,
                dedupe_key=item.dedupe_key,
                exchange_order_id=item.exchange_order_id,
                client_order_id=item.client_order_id,
                algo_id=item.algo_id,
                client_algo_id=item.client_algo_id,
                category=item.category,
                symbol=item.symbol,
                inst_id=item.inst_id,
                side=item.side,
                position_side=item.position_side,
                order_type=item.order_type,
                status=item.status,
                qty=item.qty,
                filled_qty=item.filled_qty,
                avg_price=item.avg_price,
                limit_price=item.limit_price,
                trigger_price=item.trigger_price,
                reduce_only=item.reduce_only,
                close_position=item.close_position,
                working_type=item.working_type,
                source_class="unknown",
                created_at_ms=item.created_at_ms,
                updated_at_ms=item.updated_at_ms,
                metadata_json=json.dumps(item.metadata or {}, separators=(",", ":")),
            )
            db.add(row)
            by_dedupe[item.dedupe_key] = row
            if item.exchange_order_id:
                by_oid[item.exchange_order_id] = row
            if item.algo_id:
                by_aid[item.algo_id] = row
            changed += 1
        elif item.updated_at_ms >= existing.updated_at_ms:
            existing.status = item.status
            existing.filled_qty = item.filled_qty
            existing.avg_price = item.avg_price
            existing.limit_price = item.limit_price
            existing.trigger_price = item.trigger_price
            existing.updated_at_ms = item.updated_at_ms
            if item.metadata:
                existing.metadata_json = json.dumps(item.metadata, separators=(",", ":"))
    await db.flush()
    return changed


async def _upsert_daily_snapshots(
    db: AsyncSession, account_id: int, items: Sequence[DailyAccountSnapshotEntry]
) -> int:
    if not items:
        return 0
    buckets = [beijing_day_start_ms(item.captured_at_ms or item.day_start_ms) for item in items]
    existing_days = {
        (r.currency, r.bucket_start_ms): r
        for r in (
            await db.scalars(
                select(AccountEquitySnapshot).where(
                    AccountEquitySnapshot.account_id == account_id,
                    AccountEquitySnapshot.resolution == "day",
                    AccountEquitySnapshot.bucket_start_ms.in_(buckets),
                )
            )
        ).all()
    }
    inserted = 0
    for item in items:
        day_bucket = beijing_day_start_ms(item.captured_at_ms or item.day_start_ms)
        existing = existing_days.get((item.currency, day_bucket))
        if existing is not None and existing.source != "exchange_daily":
            continue  # Never overwrite live-captured snapshots with exchange daily snapshots.
        if existing is None:
            row = AccountEquitySnapshot(
                account_id=account_id,
                currency=item.currency,
                captured_at_ms=item.captured_at_ms or day_bucket,
                bucket_start_ms=day_bucket,
                resolution="day",
                source="exchange_daily",
                equity=item.equity,
                equity_min=item.equity,
                equity_max=item.equity,
                wallet_balance=item.wallet_balance,
                unrealized_pnl=item.unrealized_pnl,
                data_quality="partial",
            )
            db.add(row)
            existing_days[(item.currency, day_bucket)] = row
            inserted += 1
        else:
            existing.equity = item.equity
            existing.equity_min = item.equity
            existing.equity_max = item.equity
            existing.wallet_balance = item.wallet_balance
            existing.unrealized_pnl = item.unrealized_pnl
    await db.flush()
    return inserted


async def _discover_symbols(db: AsyncSession, account: TradingAccount) -> List[str]:
    symbols: set[str] = set()
    if account.positions_json:
        try:
            for p in json.loads(account.positions_json):
                if isinstance(p, dict):
                    sym = str(p.get("symbol") or p.get("instId") or "").upper()
                    if sym:
                        symbols.add(sym)
        except (ValueError, TypeError):
            pass
    for sym in (
        await db.scalars(
            select(AccountCashflowEntry.symbol).where(
                AccountCashflowEntry.account_id == account.id,
                AccountCashflowEntry.symbol.is_not(None),
            )
        )
    ).all():
        if sym:
            symbols.add(sym.upper())
    for sym in (
        await db.scalars(
            select(AccountOrderHistory.inst_id).where(AccountOrderHistory.account_id == account.id)
        )
    ).all():
        if sym:
            symbols.add(sym.upper())
    for sym in (
        await db.scalars(
            select(ExecutionRecord.inst_id).where(ExecutionRecord.account_id == account.id)
        )
    ).all():
        if sym:
            symbols.add(sym.upper())
    for sym in (
        await db.scalars(
            select(OrderIntent.inst_id).where(OrderIntent.account_id == account.id)
        )
    ).all():
        if sym:
            symbols.add(sym.upper())
    snap = await db.scalar(
        select(AccountOpenOrderSnapshot).where(AccountOpenOrderSnapshot.account_id == account.id)
    )
    if snap and snap.orders_json:
        try:
            for o in json.loads(snap.orders_json):
                if isinstance(o, dict) and o.get("instId"):
                    symbols.add(str(o["instId"]).upper())
        except (ValueError, TypeError):
            pass
    for binding in (
        await db.scalars(
            select(CopyBinding).where(CopyBinding.account_id == account.id, CopyBinding.deleted_at.is_(None))
        )
    ).all():
        try:
            for s in json.loads(binding.symbol_whitelist_json or "[]"):
                if s:
                    symbols.add(str(s).upper())
            for mapped in json.loads(binding.symbol_map_json or "{}").values():
                if mapped:
                    symbols.add(str(mapped).upper())
        except (ValueError, TypeError):
            pass
    return sorted(symbols)


async def _sync_windowed_dataset(
    db: AsyncSession,
    account: TradingAccount,
    adapter: Any,
    dataset: str,
    *,
    now_ms: int,
    max_windows: int,
    fetcher,
    persister,
) -> Tuple[str, Optional[str], List[Any]]:
    caps = adapter.get_dataset_capabilities() if hasattr(adapter, "get_dataset_capabilities") else {}
    cap = caps.get(dataset)
    state = await _get_or_create_sync_state(db, account.id, dataset)
    state.last_attempt_at_ms = now_ms

    if cap is not None and not cap.supported:
        state.status = "unsupported"
        state.last_error_code = "UNSUPPORTED_DATASET"
        state.last_error_message = sanitize_error_text(cap.note or "Dataset unsupported on this exchange")
        await db.commit()
        return state.status, state.last_error_message, []

    cooldown = _adapter_cooldown_remaining(adapter)
    if cooldown > 0:
        state.status = "rate_limited"
        state.last_error_code = "RATE_LIMITED"
        state.last_error_message = sanitize_error_text(
            f"Exchange cooldown active for {cooldown:.2f}s; detail sync deferred"
        )
        await db.commit()
        return state.status, state.last_error_message, []

    max_days = DATASET_MAX_BACKFILL_DAYS[dataset]
    if cap is not None and cap.retention_days:
        max_days = min(max_days, cap.retention_days)
    target_start = max(0, now_ms - max_days * DAY_MS)
    all_fetched: List[Any] = []
    windows_used = 0
    partial_flag = bool(cap is not None and not cap.complete)

    try:
        # 1. Forward incremental sync with 24h overlap
        if state.coverage_end_ms is not None:
            inc_start = max(state.coverage_start_ms or (state.coverage_end_ms - OVERLAP_MS), state.coverage_end_ms - OVERLAP_MS)
            if inc_start <= now_ms:
                await db.commit()
                page: DatasetPage = await fetcher(inc_start, now_ms)
                windows_used += 1
                if not page.supported:
                    state.status = "unsupported"
                    state.last_error_code = page.error_code or "UNSUPPORTED_DATASET"
                    state.last_error_message = sanitize_error_text(page.error_message)
                    await db.commit()
                    return state.status, state.last_error_message, []
                inserted = await persister(db, account.id, page.items)
                all_fetched.extend(page.items)
                state.record_count += inserted
                state.coverage_end_ms = max(state.coverage_end_ms or 0, page.coverage_end_ms or now_ms)
                if state.coverage_start_ms is None:
                    state.coverage_start_ms = page.coverage_start_ms or inc_start
                if not page.complete:
                    partial_flag = True
                await db.commit()

        # 2. Recent-first contiguous backward backfill
        cur_end = (state.coverage_start_ms - 1) if state.coverage_start_ms is not None else now_ms
        while cur_end >= target_start and windows_used < max_windows:
            chunk_start = max(target_start, cur_end - WINDOW_MS)
            await db.commit()
            page = await fetcher(chunk_start, cur_end)
            windows_used += 1
            if not page.supported:
                state.status = "unsupported"
                state.last_error_code = page.error_code or "UNSUPPORTED_DATASET"
                state.last_error_message = sanitize_error_text(page.error_message)
                await db.commit()
                return state.status, state.last_error_message, all_fetched
            inserted = await persister(db, account.id, page.items)
            all_fetched.extend(page.items)
            state.record_count += inserted
            eff_start = page.coverage_start_ms if page.coverage_start_ms is not None else chunk_start
            eff_end = page.coverage_end_ms if page.coverage_end_ms is not None else cur_end
            state.coverage_start_ms = (
                eff_start if state.coverage_start_ms is None else min(state.coverage_start_ms, eff_start)
            )
            state.coverage_end_ms = max(state.coverage_end_ms or 0, eff_end)
            if not page.complete:
                partial_flag = True
            await db.commit()
            if eff_start > chunk_start:
                # Exchange retention boundary reached cleanly.
                partial_flag = True
                break
            cur_end = chunk_start - 1
            if windows_used < max_windows:
                await asyncio.sleep(WINDOW_PAUSE_SEC)

        more_backfill = (state.coverage_start_ms is None) or (state.coverage_start_ms > target_start)
        if cap is not None and cap.retention_days and DATASET_MAX_BACKFILL_DAYS[dataset] > cap.retention_days:
            partial_flag = True
        state.status = "partial" if (partial_flag or more_backfill) else "ok"
        state.last_success_at_ms = now_ms
        state.last_error_code = None
        state.last_error_message = None
        await db.commit()
        return state.status, None, all_fetched
    except Exception as exc:
        await db.rollback()
        state = await _get_or_create_sync_state(db, account.id, dataset)
        state.last_attempt_at_ms = now_ms
        rate_limited = _is_rate_limit_error(exc)
        state.status = "rate_limited" if rate_limited else ("partial" if state.coverage_start_ms is not None else "unavailable")
        state.last_error_code = "RATE_LIMITED" if rate_limited else _error_code_of(exc)
        state.last_error_message = sanitize_error_text(str(exc))
        await db.commit()
        return state.status, state.last_error_message, all_fetched


async def sync_open_orders_snapshot(
    db: AsyncSession,
    account: TradingAccount,
    adapter: Any,
    *,
    now_ms: Optional[int] = None,
) -> Dict[str, Any]:
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    state = await _get_or_create_sync_state(db, account.id, "open_orders")
    state.last_attempt_at_ms = now_ms
    await db.commit()
    try:
        orders: List[OpenOrderEntry] = await adapter.get_open_orders_normalized()
        snap = await db.scalar(
            select(AccountOpenOrderSnapshot).where(AccountOpenOrderSnapshot.account_id == account.id)
        )
        encoded = json.dumps([o.to_dict() for o in orders], separators=(",", ":"))
        if snap is None:
            snap = AccountOpenOrderSnapshot(
                account_id=account.id,
                fetched_at_ms=now_ms,
                complete=True,
                order_count=len(orders),
                orders_json=encoded,
            )
            db.add(snap)
        else:
            snap.fetched_at_ms = now_ms
            snap.complete = True
            snap.order_count = len(orders)
            snap.orders_json = encoded
        state = await _get_or_create_sync_state(db, account.id, "open_orders")
        state.status = "ok"
        state.coverage_end_ms = now_ms
        state.last_attempt_at_ms = now_ms
        state.last_success_at_ms = now_ms
        state.last_error_code = None
        state.last_error_message = None
        state.record_count = len(orders)
        await db.commit()
        return {"status": "ok", "count": len(orders), "fetchedAtMs": now_ms}
    except Exception as exc:
        await db.rollback()
        state = await _get_or_create_sync_state(db, account.id, "open_orders")
        state.last_attempt_at_ms = now_ms
        rate_limited = _is_rate_limit_error(exc)
        state.status = "rate_limited" if rate_limited else "unavailable"
        state.last_error_code = "RATE_LIMITED" if rate_limited else _error_code_of(exc)
        state.last_error_message = sanitize_error_text(str(exc))
        await db.commit()
        return {"status": state.status, "error": state.last_error_message}


async def _run_account_detail_sync(
    db: AsyncSession,
    account_id: int,
    *,
    datasets: Optional[Sequence[str]] = None,
    manual: bool = False,
    adapter_override: Any = None,
) -> Dict[str, Any]:
    now_ms = int(time.time() * 1000)
    account = await db.get(TradingAccount, account_id)
    if account is None or account.deleted_at is not None:
        return {"ok": False, "error": "Account not found", "datasets": {}}

    target_sets = set(datasets) if datasets else set(DATASET_CADENCE_SEC)
    lock = _get_account_lock(account_id)
    async with lock:
        if adapter_override is not None:
            adapter = adapter_override
        else:
            creds = CredentialVault().decrypt_dict(account.encrypted_credentials)
            proxies = list((await db.scalars(select(ProxyNode).order_by(ProxyNode.priority))).all())
            proxy_url = ProxyManager.select_proxy_url(proxies, account.proxy_id, exchange=account.exchange)
            adapter = create_exchange_adapter(
                account_id=account.id,
                exchange=account.exchange,
                credentials=creds,
                environment=account.environment,
                proxy_url=proxy_url,
                margin_mode=account.margin_mode,
                position_mode=account.position_mode,
            )
            adapter._time_offset_ms = int(getattr(account, "clock_offset_ms", 0) or 0)

        results: Dict[str, Dict[str, Any]] = {}
        max_windows = MANUAL_MAX_WINDOWS if manual else BACKGROUND_MAX_WINDOWS
        if not manual:
            # Background detail sync is deliberately incremental: live open
            # orders may run every cycle, but only one historical dataset is
            # advanced per cycle so backfill cannot monopolize exchange quota.
            low_priority = [name for name in LOW_PRIORITY_DATASETS if name in target_sets]
            target_sets = {"open_orders"} & target_sets
            if low_priority:
                target_sets.add(low_priority[0])

        # 1. Open orders & risk/equity snapshot
        open_order_count: Optional[int] = None
        if "open_orders" in target_sets or manual:
            oo_res = await sync_open_orders_snapshot(db, account, adapter, now_ms=now_ms)
            results["open_orders"] = oo_res
            if oo_res.get("status") == "ok":
                open_order_count = oo_res.get("count")

        if manual:
            try:
                risk_info = await adapter.get_account_risk_snapshot()
                positions = await adapter.get_positions()
                if hasattr(adapter, "get_adl_quantiles"):
                    try:
                        adl = await adapter.get_adl_quantiles()
                        for pos in positions:
                            q = adl.get((pos.symbol.upper(), pos.pos_side.lower()))
                            if q is None:
                                q = adl.get((pos.symbol.upper(), "both"))
                            if q is not None:
                                pos.adl_quantile = q
                    except Exception:
                        pass
                await capture_equity_snapshot(
                    db,
                    account,
                    None,
                    positions,
                    source="manual_sync",
                    risk_info=risk_info,
                    open_order_count=open_order_count,
                    balance_as_of_ms=risk_info.as_of_ms or now_ms,
                    positions_as_of_ms=now_ms,
                    now_ms=now_ms,
                )
                await db.commit()
                results["equity"] = {"status": "ok"}
            except Exception as exc:
                await db.rollback()
                logger.warning("Detail equity snapshot failed for account %s: %s", account_id, sanitize_error_text(str(exc)))
                results["equity"] = {"status": "unavailable", "error": sanitize_error_text(str(exc))}

        # 2. Cashflow
        saw_liquidation_flow = False
        if "cashflow" in target_sets:
            st, err, fetched_cf = await _sync_windowed_dataset(
                db, account, adapter, "cashflow", now_ms=now_ms, max_windows=max_windows,
                fetcher=adapter.get_cashflow_history, persister=_upsert_cashflows,
            )
            results["cashflow"] = {"status": st, "error": err}
            saw_liquidation_flow = any(
                c.category in ("LIQUIDATION_FEE", "INSURANCE_CLEAR") for c in fetched_cf
            )

        # 3. Orders (run before fills so clientOrderId and symbols are discovered)
        symbols = await _discover_symbols(db, account)
        if "orders" in target_sets:
            st, err, _ = await _sync_windowed_dataset(
                db, account, adapter, "orders", now_ms=now_ms, max_windows=max_windows,
                fetcher=lambda s, e: adapter.get_order_history(s, e, symbols=symbols),
                persister=_upsert_orders,
            )
            results["orders"] = {"status": st, "error": err}
            symbols = await _discover_symbols(db, account)

        # 4. Fills
        if "fills" in target_sets:
            st, err, _ = await _sync_windowed_dataset(
                db, account, adapter, "fills", now_ms=now_ms, max_windows=max_windows,
                fetcher=lambda s, e: adapter.get_trade_fills(symbols, s, e),
                persister=_upsert_fills,
            )
            results["fills"] = {"status": st, "error": err}

        # 5. Force orders
        if "force_orders" in target_sets or saw_liquidation_flow:
            st, err, _ = await _sync_windowed_dataset(
                db, account, adapter, "force_orders", now_ms=now_ms, max_windows=max_windows,
                fetcher=adapter.get_force_orders, persister=_upsert_orders,
            )
            results["force_orders"] = {"status": st, "error": err}

        # 6. Commission rates
        if "commission_rates" in target_sets and symbols:
            state = await _get_or_create_sync_state(db, account.id, "commission_rates")
            state.last_attempt_at_ms = now_ms
            caps = adapter.get_dataset_capabilities() if hasattr(adapter, "get_dataset_capabilities") else {}
            if caps.get("commission_rates") and not caps["commission_rates"].supported:
                state.status = "unsupported"
                state.last_error_code = "UNSUPPORTED_DATASET"
                state.last_error_message = sanitize_error_text(caps["commission_rates"].note)
                await db.commit()
                results["commission_rates"] = {"status": "unsupported"}
            else:
                await db.commit()
                try:
                    rates: List[CommissionRateInfo] = await adapter.get_commission_rates(
                        symbols[:20 if manual else 8]
                    )
                    state = await _get_or_create_sync_state(db, account.id, "commission_rates")
                    state.status = "ok"
                    state.last_success_at_ms = now_ms
                    state.last_error_code = None
                    state.last_error_message = None
                    state.record_count = len(rates)
                    state.cursor_json = json.dumps(
                        [
                            {
                                "symbol": r.symbol,
                                "instId": r.inst_id,
                                "makerRate": str(r.maker_rate) if r.maker_rate is not None else None,
                                "takerRate": str(r.taker_rate) if r.taker_rate is not None else None,
                            }
                            for r in rates
                        ],
                        separators=(",", ":"),
                    )
                    await db.commit()
                    results["commission_rates"] = {"status": "ok", "count": len(rates)}
                except Exception as exc:
                    await db.rollback()
                    state = await _get_or_create_sync_state(db, account.id, "commission_rates")
                    state.last_attempt_at_ms = now_ms
                    state.status = "rate_limited" if _is_rate_limit_error(exc) else "unavailable"
                    state.last_error_code = "RATE_LIMITED" if _is_rate_limit_error(exc) else _error_code_of(exc)
                    state.last_error_message = sanitize_error_text(str(exc))
                    await db.commit()
                    results["commission_rates"] = {"status": state.status, "error": state.last_error_message}

        # 7. Daily account snapshots (SAPI live only when supported)
        if "daily_snapshots" in target_sets:
            state = await _get_or_create_sync_state(db, account.id, "daily_snapshots")
            state.last_attempt_at_ms = now_ms
            caps = adapter.get_dataset_capabilities() if hasattr(adapter, "get_dataset_capabilities") else {}
            if caps.get("daily_snapshots") and not caps["daily_snapshots"].supported:
                state.status = "unsupported"
                state.last_error_code = "UNSUPPORTED_DATASET"
                state.last_error_message = sanitize_error_text(caps["daily_snapshots"].note)
                await db.commit()
                results["daily_snapshots"] = {"status": "unsupported"}
            else:
                await db.commit()
                try:
                    page = await adapter.get_daily_account_snapshots(
                        max(0, now_ms - DATASET_MAX_BACKFILL_DAYS["daily_snapshots"] * DAY_MS), now_ms
                    )
                    state = await _get_or_create_sync_state(db, account.id, "daily_snapshots")
                    if not page.supported:
                        state.status = "unsupported"
                        state.last_error_code = page.error_code or "UNSUPPORTED_DATASET"
                        state.last_error_message = sanitize_error_text(page.error_message)
                        await db.commit()
                        results["daily_snapshots"] = {"status": "unsupported", "error": state.last_error_message}
                    else:
                        inserted = await _upsert_daily_snapshots(db, account.id, page.items)
                        state.status = "ok"
                        state.last_success_at_ms = now_ms
                        state.coverage_start_ms = page.coverage_start_ms
                        state.coverage_end_ms = page.coverage_end_ms
                        state.record_count += inserted
                        state.last_error_code = None
                        state.last_error_message = None
                        await db.commit()
                        results["daily_snapshots"] = {"status": "ok", "count": inserted}
                except Exception as exc:
                    await db.rollback()
                    state = await _get_or_create_sync_state(db, account.id, "daily_snapshots")
                    state.last_attempt_at_ms = now_ms
                    state.status = "rate_limited" if _is_rate_limit_error(exc) else "unavailable"
                    state.last_error_code = "RATE_LIMITED" if _is_rate_limit_error(exc) else _error_code_of(exc)
                    state.last_error_message = sanitize_error_text(str(exc))
                    await db.commit()
                    results["daily_snapshots"] = {"status": state.status, "error": state.last_error_message}

        # 8. Reconcile attribution & alert states
        try:
            attr_res = await reconcile_attribution(db, account_id, now_ms=now_ms)
            await persist_account_alerts(db, account_id, now_ms=now_ms, attribution_conflicts=attr_res["conflicts"])
            await db.commit()
        except Exception as exc:
            await db.rollback()
            logger.warning("Detail attribution/alert update failed for account %s: %s", account_id, sanitize_error_text(str(exc)))

    sync_states = list(
        (
            await db.scalars(
                select(AccountDatasetSyncState).where(AccountDatasetSyncState.account_id == account_id)
            )
        ).all()
    )
    return {
        "ok": True,
        "accountId": account_id,
        "syncedAtMs": now_ms,
        "datasets": {
            s.dataset: {
                "status": s.status,
                "coverageStartMs": s.coverage_start_ms,
                "coverageEndMs": s.coverage_end_ms,
                "lastAttemptAtMs": s.last_attempt_at_ms,
                "lastSuccessAtMs": s.last_success_at_ms,
                "errorCode": s.last_error_code,
                "errorMessage": sanitize_error_text(s.last_error_message),
                "recordCount": s.record_count,
            }
            for s in sync_states
        },
        "results": results,
    }


async def sync_account_detail(
    db: Optional[AsyncSession],
    account_id: int,
    *,
    datasets: Optional[Sequence[str]] = None,
    manual: bool = False,
    adapter_override: Any = None,
) -> Dict[str, Any]:
    """Coalesce concurrent detail syncs per account so exchanges are never double-polled."""
    loop_id = id(asyncio.get_running_loop())
    scope = "full" if (manual or not datasets or len(datasets) > 1) else datasets[0]
    full_key = (loop_id, account_id, "full")
    key = (loop_id, account_id, scope)

    existing = _in_flight_syncs.get(full_key) or _in_flight_syncs.get(key)
    if existing is not None and not existing.done():
        return await asyncio.shield(existing)

    async def _runner() -> Dict[str, Any]:
        if db is not None and adapter_override is not None:
            return await _run_account_detail_sync(
                db, account_id, datasets=datasets, manual=manual, adapter_override=adapter_override
            )
        factory = get_session_factory()
        async with factory() as own_db:
            return await _run_account_detail_sync(
                own_db, account_id, datasets=datasets, manual=manual, adapter_override=adapter_override
            )

    task = asyncio.create_task(_runner(), name=f"acct-detail-sync-{account_id}-{scope}")
    _in_flight_syncs[key] = task
    try:
        return await asyncio.shield(task)
    finally:
        if _in_flight_syncs.get(key) is task:
            _in_flight_syncs.pop(key, None)


async def due_detail_datasets(db: AsyncSession, account_id: int, now_ms: int) -> List[str]:
    states = {
        s.dataset: s
        for s in (
            await db.scalars(
                select(AccountDatasetSyncState).where(AccountDatasetSyncState.account_id == account_id)
            )
        ).all()
    }
    active = await _account_has_enabled_binding(db, account_id)
    due: List[str] = []
    for dataset in DATASET_CADENCE_SEC:
        st = states.get(dataset)
        if st is None:
            due.append(dataset)
            continue
        if st.status == "unsupported":
            # Retry unsupported rarely (daily) in case API permissions changed.
            last = st.last_attempt_at_ms or 0
            if now_ms - last >= 86_400_000:
                due.append(dataset)
            continue
        cadence_sec = _effective_cadence_sec(dataset, active=active)
        if st.status == "rate_limited":
            backoff = (
                OPEN_ORDERS_RATE_LIMIT_BACKOFF_SEC
                if dataset == "open_orders"
                else RATE_LIMIT_BACKOFF_SEC
            )
            cadence_sec = max(cadence_sec, backoff)
        last_try = st.last_attempt_at_ms or st.last_success_at_ms or 0
        if now_ms - last_try >= cadence_sec * 1000:
            due.append(dataset)
    return due
