"""Equity snapshot capture and idempotent hour/day downsampling."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import json
import time
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import delete, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time_util import BEIJING_TZ
from app.models.domain import AccountDatasetSyncState, AccountEquitySnapshot, TradingAccount
from app.services.adapters.base import AccountRiskInfo, BalanceInfo, safe_decimal

RAW_BUCKET_MS = 60_000
HOUR_BUCKET_MS = 3_600_000
RAW_RETENTION_DAYS = 90
HOUR_RETENTION_DAYS = 730
BALANCE_POS_SKEW_TOLERANCE_MS = 30_000


def beijing_day_start_ms(ts_ms: int) -> int:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(BEIJING_TZ)
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


def _position_attr(pos: Any, *names: str) -> Any:
    if isinstance(pos, dict):
        for name in names:
            if name in pos and pos[name] is not None:
                return pos[name]
        return None
    for name in names:
        val = getattr(pos, name, None)
        if val is not None:
            return val
    return None


def _position_currency(pos: Any) -> str:
    sym = str(_position_attr(pos, "symbol", "inst_id", "instId") or "").upper()
    if sym.endswith(("USDC", "USDC-SWAP")):
        return "USDC"
    return "USDT"


def _compute_position_metrics(positions: Optional[Sequence[Any]], currency: str) -> Dict[str, Any]:
    if positions is None:
        return {
            "total_notional": None,
            "long_notional": None,
            "short_notional": None,
            "net_notional": None,
            "initial_margin": None,
            "maintenance_margin": None,
            "position_count": None,
        }
    total_notional = Decimal("0")
    long_notional = Decimal("0")
    short_notional = Decimal("0")
    im_sum = Decimal("0")
    mm_sum = Decimal("0")
    has_im = False
    has_mm = False
    count = 0

    for pos in positions:
        if _position_currency(pos) != currency:
            continue
        raw_qty = safe_decimal(_position_attr(pos, "pos", "qty"))
        if raw_qty is None or abs(raw_qty) <= 0:
            continue
        count += 1
        notional = safe_decimal(_position_attr(pos, "notional_usd", "notionalUsd"))
        if notional is None:
            px = safe_decimal(_position_attr(pos, "cur_px", "markPx", "markPrice", "avg_px", "avgPx"))
            if px is not None and px > 0:
                notional = abs(raw_qty) * px
        if notional is not None:
            abs_notional = abs(notional)
            total_notional += abs_notional
            side = str(_position_attr(pos, "pos_side", "posSide", "positionSide") or "").lower()
            if side == "long" or (side in ("net", "both") and raw_qty > 0):
                long_notional += abs_notional
            elif side == "short" or (side in ("net", "both") and raw_qty < 0):
                short_notional += abs_notional

        im_val = safe_decimal(_position_attr(pos, "initial_margin", "initialMargin", "margin_usd", "marginUsd"))
        if im_val is not None:
            im_sum += im_val
            has_im = True
        mm_val = safe_decimal(_position_attr(pos, "maintenance_margin", "maintenanceMargin"))
        if mm_val is not None:
            mm_sum += mm_val
            has_mm = True

    return {
        "total_notional": total_notional,
        "long_notional": long_notional,
        "short_notional": short_notional,
        "net_notional": long_notional - short_notional,
        "initial_margin": im_sum if has_im else (Decimal("0") if count == 0 else None),
        "maintenance_margin": mm_sum if has_mm else (Decimal("0") if count == 0 else None),
        "position_count": count,
    }


def _extract_currency_rows(
    balance: Optional[BalanceInfo],
    risk_info: Optional[AccountRiskInfo],
) -> Dict[str, Dict[str, Optional[Decimal]]]:
    rows: Dict[str, Dict[str, Optional[Decimal]]] = {}
    if risk_info is not None and risk_info.asset_balances:
        for ccy, info in risk_info.asset_balances.items():
            if not isinstance(info, dict):
                continue
            eq = safe_decimal(info.get("equity"))
            wb = safe_decimal(info.get("wallet"))
            if eq is None:
                continue
            if ccy == "USDT" or (ccy == "USDC" and (eq != 0 or (wb is not None and wb != 0))):
                rows[ccy] = {
                    "equity": eq,
                    "available": safe_decimal(info.get("available")),
                    "wallet": wb,
                    "upl": safe_decimal(info.get("upl")),
                    "initial_margin": safe_decimal(info.get("initialMargin")),
                    "maintenance_margin": safe_decimal(info.get("maintenanceMargin")),
                }
        if "USDT" not in rows and risk_info.currency == "USDT" and risk_info.equity is not None:
            rows["USDT"] = {
                "equity": risk_info.equity,
                "available": risk_info.available_balance,
                "wallet": risk_info.wallet_balance,
                "upl": risk_info.unrealized_pnl,
                "initial_margin": risk_info.initial_margin,
                "maintenance_margin": risk_info.maintenance_margin,
            }
        return rows

    if balance is None:
        return rows
    assets = getattr(balance, "asset_balances", None) or {}
    if isinstance(assets, dict) and assets:
        for ccy in ("USDT", "USDC"):
            if ccy in assets and isinstance(assets[ccy], dict):
                item = assets[ccy]
                eq = safe_decimal(item.get("equity"))
                wb = safe_decimal(item.get("wallet"))
                if eq is None:
                    continue
                if ccy == "USDT" or (eq != 0 or (wb is not None and wb != 0)):
                    rows[ccy] = {
                        "equity": eq,
                        "available": safe_decimal(item.get("available")),
                        "wallet": wb,
                        "upl": safe_decimal(item.get("upl")),
                        "initial_margin": None,
                        "maintenance_margin": None,
                    }
    if "USDT" not in rows and getattr(balance, "currency", "USDT") == "USDT":
        eq = safe_decimal(getattr(balance, "eq", None))
        if eq is not None:
            rows["USDT"] = {
                "equity": eq,
                "available": safe_decimal(getattr(balance, "avail_eq", None)),
                "wallet": safe_decimal(getattr(balance, "cash_bal", None)),
                "upl": safe_decimal(getattr(balance, "upl", None)),
                "initial_margin": safe_decimal(getattr(balance, "initial_margin", None)),
                "maintenance_margin": safe_decimal(getattr(balance, "maintenance_margin", None)),
            }
    return rows


async def capture_equity_snapshot(
    db: AsyncSession,
    account: TradingAccount,
    balance: Optional[BalanceInfo],
    positions: Optional[Sequence[Any]],
    *,
    source: str = "account_sync",
    risk_info: Optional[AccountRiskInfo] = None,
    open_order_count: Optional[int] = None,
    balance_as_of_ms: Optional[int] = None,
    positions_as_of_ms: Optional[int] = None,
    now_ms: Optional[int] = None,
) -> List[AccountEquitySnapshot]:
    """Upsert 60-second bucketed equity & risk snapshots without affecting trading flow."""
    if not isinstance(db, AsyncSession) or inspect(account, raiseerr=False) is None:
        return []
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    bucket_start_ms = (now_ms // RAW_BUCKET_MS) * RAW_BUCKET_MS
    bal_ms = (
        balance_as_of_ms
        if balance_as_of_ms is not None
        else (getattr(risk_info, "as_of_ms", None) or getattr(balance, "as_of_ms", None) or now_ms)
    )
    pos_ms = positions_as_of_ms if positions_as_of_ms is not None else (now_ms if positions is not None else None)
    quality = (
        "ok"
        if (positions is not None and pos_ms is not None and abs(bal_ms - pos_ms) <= BALANCE_POS_SKEW_TOLERANCE_MS)
        else "partial"
    )
    ccy_rows = _extract_currency_rows(balance, risk_info)
    saved: List[AccountEquitySnapshot] = []
    cap_flags: Dict[str, Any] = {}
    src_obj = risk_info if risk_info is not None else balance
    if src_obj is not None:
        for attr_name, json_key in (
            ("can_trade", "canTrade"),
            ("can_deposit", "canDeposit"),
            ("can_withdraw", "canWithdraw"),
            ("multi_assets_margin", "multiAssetsMargin"),
            ("dual_side_position", "dualSidePosition"),
            ("margin_ratio_source", "marginRatioSource"),
        ):
            val = getattr(src_obj, attr_name, None)
            if val is not None:
                cap_flags[json_key] = val

    for currency, bal_data in ccy_rows.items():
        equity = bal_data["equity"]
        if equity is None:
            continue
        pos_metrics = _compute_position_metrics(positions, currency)
        im = bal_data["initial_margin"] if bal_data["initial_margin"] is not None else pos_metrics["initial_margin"]
        mm = bal_data["maintenance_margin"] if bal_data["maintenance_margin"] is not None else pos_metrics["maintenance_margin"]
        total_notional = pos_metrics["total_notional"]
        eff_lev = (total_notional / equity) if (total_notional is not None and equity > 0) else None
        ex_mr = (
            risk_info.margin_ratio
            if (currency == "USDT" and risk_info is not None and risk_info.margin_ratio is not None)
            else (
                safe_decimal(getattr(balance, "margin_ratio", None))
                if (currency == "USDT" and balance is not None)
                else None
            )
        )
        if ex_mr is not None:
            margin_ratio = ex_mr
            cap_flags.setdefault("marginRatioSource", getattr(risk_info, "margin_ratio_source", None) or "exchange")
        elif mm is not None and equity > 0:
            margin_ratio = mm / equity
            cap_flags.setdefault("marginRatioSource", "derived")
        else:
            margin_ratio = None

        with db.no_autoflush:
            existing = await db.scalar(
                select(AccountEquitySnapshot).where(
                    AccountEquitySnapshot.account_id == account.id,
                    AccountEquitySnapshot.currency == currency,
                    AccountEquitySnapshot.resolution == "raw",
                    AccountEquitySnapshot.bucket_start_ms == bucket_start_ms,
                )
            )
        if existing is None:
            row = AccountEquitySnapshot(
                account_id=account.id,
                currency=currency,
                captured_at_ms=now_ms,
                bucket_start_ms=bucket_start_ms,
                resolution="raw",
                source=source,
                equity=equity,
                equity_min=equity,
                equity_max=equity,
                wallet_balance=bal_data["wallet"],
                available_balance=bal_data["available"],
                unrealized_pnl=bal_data["upl"],
                total_notional=total_notional,
                long_notional=pos_metrics["long_notional"],
                short_notional=pos_metrics["short_notional"],
                net_notional=pos_metrics["net_notional"],
                initial_margin=im,
                maintenance_margin=mm,
                margin_ratio=margin_ratio,
                effective_leverage=eff_lev,
                position_count=pos_metrics["position_count"],
                open_order_count=open_order_count,
                balance_as_of_ms=bal_ms,
                positions_as_of_ms=pos_ms,
                data_quality=quality,
            )
            db.add(row)
            saved.append(row)
        else:
            existing.captured_at_ms = max(existing.captured_at_ms, now_ms)
            existing.source = source
            existing.equity = equity
            existing.equity_min = min(existing.equity_min if existing.equity_min is not None else existing.equity, equity)
            existing.equity_max = max(existing.equity_max if existing.equity_max is not None else existing.equity, equity)
            if bal_data["wallet"] is not None:
                existing.wallet_balance = bal_data["wallet"]
            if bal_data["available"] is not None:
                existing.available_balance = bal_data["available"]
            if bal_data["upl"] is not None:
                existing.unrealized_pnl = bal_data["upl"]
            if total_notional is not None:
                existing.total_notional = total_notional
                existing.long_notional = pos_metrics["long_notional"]
                existing.short_notional = pos_metrics["short_notional"]
                existing.net_notional = pos_metrics["net_notional"]
                existing.position_count = pos_metrics["position_count"]
            if im is not None:
                existing.initial_margin = im
            if mm is not None:
                existing.maintenance_margin = mm
            if margin_ratio is not None:
                existing.margin_ratio = margin_ratio
            if eff_lev is not None:
                existing.effective_leverage = eff_lev
            if open_order_count is not None:
                existing.open_order_count = open_order_count
            existing.balance_as_of_ms = bal_ms
            if pos_ms is not None:
                existing.positions_as_of_ms = pos_ms
            existing.data_quality = quality
            saved.append(existing)

    if saved:
        with db.no_autoflush:
            sync_state = await db.scalar(
                select(AccountDatasetSyncState).where(
                    AccountDatasetSyncState.account_id == account.id,
                    AccountDatasetSyncState.dataset == "equity",
                )
            )
        sync_status = "ok" if quality == "ok" else "partial"
        if sync_state is None:
            sync_state = AccountDatasetSyncState(
                account_id=account.id,
                dataset="equity",
                status=sync_status,
                coverage_start_ms=bucket_start_ms,
                coverage_end_ms=now_ms,
                cursor_json=json.dumps(cap_flags, separators=(",", ":")) if cap_flags else None,
                last_attempt_at_ms=now_ms,
                last_success_at_ms=now_ms,
                record_count=len(saved),
            )
            db.add(sync_state)
        else:
            existing_flags: Dict[str, Any] = {}
            if sync_state.cursor_json:
                try:
                    parsed_flags = json.loads(sync_state.cursor_json)
                    if isinstance(parsed_flags, dict):
                        existing_flags = parsed_flags
                except (ValueError, TypeError):
                    existing_flags = {}
            existing_flags.update(cap_flags)
            sync_state.status = sync_status
            sync_state.coverage_start_ms = (
                bucket_start_ms
                if sync_state.coverage_start_ms is None
                else min(sync_state.coverage_start_ms, bucket_start_ms)
            )
            sync_state.coverage_end_ms = max(sync_state.coverage_end_ms or 0, now_ms)
            if existing_flags:
                sync_state.cursor_json = json.dumps(existing_flags, separators=(",", ":"))
            sync_state.last_attempt_at_ms = now_ms
            sync_state.last_success_at_ms = now_ms
            sync_state.last_error_code = None
            sync_state.last_error_message = None
            sync_state.record_count = max(sync_state.record_count, 0) + len(saved)
    await db.flush()
    return saved


def _max_decimal(values: List[Optional[Decimal]]) -> Optional[Decimal]:
    finite = [v for v in values if v is not None]
    return max(finite) if finite else None


async def _upsert_aggregate_bucket(
    db: AsyncSession,
    *,
    account_id: int,
    currency: str,
    resolution: str,
    bucket_start_ms: int,
    rows: List[AccountEquitySnapshot],
) -> None:
    rows.sort(key=lambda r: (r.captured_at_ms, r.id))
    last = rows[-1]
    eq_min = min((r.equity_min if r.equity_min is not None else r.equity) for r in rows)
    eq_max = max((r.equity_max if r.equity_max is not None else r.equity) for r in rows)
    max_lev = _max_decimal([r.effective_leverage for r in rows])
    max_mr = _max_decimal([r.margin_ratio for r in rows])
    max_notional = _max_decimal([r.total_notional for r in rows])
    max_im = _max_decimal([r.initial_margin for r in rows])
    max_mm = _max_decimal([r.maintenance_margin for r in rows])
    qualities = {r.data_quality for r in rows}
    quality = "ok" if qualities == {"ok"} else ("estimated" if "estimated" in qualities and "ok" not in qualities else "partial")

    existing = await db.scalar(
        select(AccountEquitySnapshot).where(
            AccountEquitySnapshot.account_id == account_id,
            AccountEquitySnapshot.currency == currency,
            AccountEquitySnapshot.resolution == resolution,
            AccountEquitySnapshot.bucket_start_ms == bucket_start_ms,
        )
    )
    if existing is None:
        db.add(
            AccountEquitySnapshot(
                account_id=account_id,
                currency=currency,
                captured_at_ms=last.captured_at_ms,
                bucket_start_ms=bucket_start_ms,
                resolution=resolution,
                source=last.source,
                equity=last.equity,
                equity_min=eq_min,
                equity_max=eq_max,
                wallet_balance=last.wallet_balance,
                available_balance=last.available_balance,
                unrealized_pnl=last.unrealized_pnl,
                total_notional=max_notional if max_notional is not None else last.total_notional,
                long_notional=last.long_notional,
                short_notional=last.short_notional,
                net_notional=last.net_notional,
                initial_margin=max_im if max_im is not None else last.initial_margin,
                maintenance_margin=max_mm if max_mm is not None else last.maintenance_margin,
                margin_ratio=max_mr,
                effective_leverage=max_lev,
                position_count=last.position_count,
                open_order_count=last.open_order_count,
                balance_as_of_ms=last.balance_as_of_ms,
                positions_as_of_ms=last.positions_as_of_ms,
                data_quality=quality,
            )
        )
    else:
        existing.captured_at_ms = max(existing.captured_at_ms, last.captured_at_ms)
        existing.source = last.source
        existing.equity = last.equity
        existing.equity_min = min(existing.equity_min if existing.equity_min is not None else existing.equity, eq_min)
        existing.equity_max = max(existing.equity_max if existing.equity_max is not None else existing.equity, eq_max)
        existing.wallet_balance = last.wallet_balance
        existing.available_balance = last.available_balance
        existing.unrealized_pnl = last.unrealized_pnl
        existing.total_notional = _max_decimal([existing.total_notional, max_notional])
        existing.long_notional = last.long_notional
        existing.short_notional = last.short_notional
        existing.net_notional = last.net_notional
        existing.initial_margin = _max_decimal([existing.initial_margin, max_im])
        existing.maintenance_margin = _max_decimal([existing.maintenance_margin, max_mm])
        existing.margin_ratio = _max_decimal([existing.margin_ratio, max_mr])
        existing.effective_leverage = _max_decimal([existing.effective_leverage, max_lev])
        existing.position_count = last.position_count
        existing.open_order_count = last.open_order_count
        existing.data_quality = quality


async def downsample_equity_snapshots(db: AsyncSession, *, now_ms: Optional[int] = None) -> Dict[str, int]:
    """Idempotently downsample raw -> hour -> day and prune only verified-aggregated expired rows."""
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    raw_rows = list(
        (
            await db.scalars(
                select(AccountEquitySnapshot).where(AccountEquitySnapshot.resolution == "raw")
            )
        ).all()
    )
    hour_groups: Dict[tuple[int, str, int], List[AccountEquitySnapshot]] = defaultdict(list)
    day_groups: Dict[tuple[int, str, int], List[AccountEquitySnapshot]] = defaultdict(list)

    for row in raw_rows:
        h_bucket = (row.captured_at_ms // HOUR_BUCKET_MS) * HOUR_BUCKET_MS
        d_bucket = beijing_day_start_ms(row.captured_at_ms)
        hour_groups[(row.account_id, row.currency, h_bucket)].append(row)
        day_groups[(row.account_id, row.currency, d_bucket)].append(row)

    for (acct_id, ccy, h_bucket), group in hour_groups.items():
        await _upsert_aggregate_bucket(
            db, account_id=acct_id, currency=ccy, resolution="hour", bucket_start_ms=h_bucket, rows=group
        )

    hour_rows = list(
        (
            await db.scalars(
                select(AccountEquitySnapshot).where(AccountEquitySnapshot.resolution == "hour")
            )
        ).all()
    )
    for row in hour_rows:
        d_bucket = beijing_day_start_ms(row.captured_at_ms)
        key = (row.account_id, row.currency, d_bucket)
        if key not in day_groups:
            day_groups[key].append(row)

    for (acct_id, ccy, d_bucket), group in day_groups.items():
        await _upsert_aggregate_bucket(
            db, account_id=acct_id, currency=ccy, resolution="day", bucket_start_ms=d_bucket, rows=group
        )
    await db.flush()

    existing_hours = {
        (r[0], r[1], r[2])
        for r in (
            await db.execute(
                select(
                    AccountEquitySnapshot.account_id,
                    AccountEquitySnapshot.currency,
                    AccountEquitySnapshot.bucket_start_ms,
                ).where(AccountEquitySnapshot.resolution == "hour")
            )
        ).all()
    }
    existing_days = {
        (r[0], r[1], r[2])
        for r in (
            await db.execute(
                select(
                    AccountEquitySnapshot.account_id,
                    AccountEquitySnapshot.currency,
                    AccountEquitySnapshot.bucket_start_ms,
                ).where(AccountEquitySnapshot.resolution == "day")
            )
        ).all()
    }

    raw_cutoff = now_ms - RAW_RETENTION_DAYS * 86_400_000
    hour_cutoff = now_ms - HOUR_RETENTION_DAYS * 86_400_000
    raw_delete_ids = [
        r.id
        for r in raw_rows
        if r.captured_at_ms < raw_cutoff
        and (r.account_id, r.currency, (r.captured_at_ms // HOUR_BUCKET_MS) * HOUR_BUCKET_MS) in existing_hours
        and (r.account_id, r.currency, beijing_day_start_ms(r.captured_at_ms)) in existing_days
    ]
    hour_delete_ids = [
        r.id
        for r in hour_rows
        if r.captured_at_ms < hour_cutoff
        and (r.account_id, r.currency, beijing_day_start_ms(r.captured_at_ms)) in existing_days
    ]

    deleted_raw = 0
    deleted_hour = 0
    if raw_delete_ids:
        res = await db.execute(
            delete(AccountEquitySnapshot)
            .where(AccountEquitySnapshot.id.in_(raw_delete_ids))
            .execution_options(synchronize_session=False)
        )
        deleted_raw = int(res.rowcount or 0)
    if hour_delete_ids:
        res = await db.execute(
            delete(AccountEquitySnapshot)
            .where(AccountEquitySnapshot.id.in_(hour_delete_ids))
            .execution_options(synchronize_session=False)
        )
        deleted_hour = int(res.rowcount or 0)
    await db.flush()
    return {"deleted_raw": deleted_raw, "deleted_hour": deleted_hour}
