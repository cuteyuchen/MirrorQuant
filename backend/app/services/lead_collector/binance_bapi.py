from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import math
import random
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

import httpx

from app.core.secret_mask import redact_proxy_userinfo
from app.services.lead_collector.base import BaseLeadCollector
from app.services.engine.sizer import exact
from app.services.lead_collector.snapshot_clock import closed_interval, event_interval, event_timestamp
from app.services.proxy.manager import ProxyManager

FRIENDLY_ROOT = "https://www.binance.com/bapi/futures/v1/friendly/future"
FRIENDLY_COPY = f"{FRIENDLY_ROOT}/copy-trade"
PUBLIC_COPY = "https://www.binance.com/bapi/futures/v1/public/future/copy-trade"
FAPI_PRIMARY = "https://www.binance.com"
FAPI_FALLBACK = "https://fapi.binance.com"

BUSY_CODE = "11012005"
RATE_LIMIT_CODE = "90801003"
MAX_STALE_SNAPSHOT_SEC = 60
DETAIL_DEGRADED_WARN_INTERVAL = 300.0
HISTORY_RETRY_BUDGET = 10.0
BATCH_GAP_MS = 30 * 60 * 1000
STORE_MAX = 6000
BRACKET_TTL_S = 6 * 3600
logger = logging.getLogger("mirrorquant.binance_bapi")


def describe_exception(exc: Optional[BaseException], response: Optional[httpx.Response] = None) -> str:
    """Keep empty transport errors diagnosable without exposing proxy userinfo."""
    cause = exc
    seen = set()
    while response is None and cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        response = getattr(cause, "response", None)
        cause = cause.__cause__
    status = getattr(response, "status_code", None)
    request_id = None
    if response is not None:
        for header in ("x-mbx-request-id", "x-request-id", "x-amzn-requestid"):
            value = response.headers.get(header)
            if isinstance(value, str) and value:
                request_id = value
                break
    message = str(exc).strip() if exc is not None else ""
    return redact_proxy_userinfo(
        f"{type(exc).__name__} status={status} reqid={request_id} {message or '(empty message)'}"
    )


class BapiBusyError(RuntimeError):
    pass


class BapiRateLimitError(RuntimeError):
    pass


def is_retryable_exception(exc: BaseException) -> bool:
    """Inspect the cause of a wrapped request error, including its HTTP status."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            if status == 429 or 500 <= status < 600:
                return True
        if isinstance(exc, (httpx.TimeoutException, httpx.TransportError, httpx.NetworkError,
                            BapiBusyError, BapiRateLimitError)):
            return True
        exc = exc.__cause__
    return False


def fnum(val: Any) -> Optional[float]:
    try:
        if val is None or val == "":
            return None
        number = float(val)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def order_dedup_key(o: Dict[str, Any]) -> str:
    """Order IDs identify cumulative snapshots; ID-less BAPI rows identify fills.

    Only the confirmed executedQty/avgPrice fields extend an ID-less identity.
    Update timestamps and unverified IDs must never affect replay identity.
    """
    identity = [o.get("orderId"), o.get("orderTime"), o.get("symbol"),
                o.get("side"), o.get("positionSide"), o.get("type")]
    if source_fill_kind(o) == "individual":
        identity[0] = None
        identity.extend([decimal_identity(o.get("executedQty")), decimal_identity(o.get("avgPrice"))])
    raw = json.dumps(identity, sort_keys=True).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:24]


def source_fill_kind(o: Dict[str, Any]) -> str:
    return "cumulative" if o.get("orderId") not in (None, "") else "individual"


def decimal_identity(value: Any) -> str:
    # Decimal.normalize() can round long values under the active context.
    value = format(exact(value), "f")
    return value.rstrip("0").rstrip(".") if "." in value else value


def validate_source_fill(q: Any, p: Any) -> Tuple[Decimal, Decimal]:
    quantity, price = exact(q), exact(p)
    if not math.isfinite(float(quantity)) or not math.isfinite(float(price)):
        raise ValueError("Non-finite source fill or price")
    if quantity <= 0 or price <= 0:
        raise ValueError("Non-positive source fill or price")
    return quantity, price


def check_source_fill_update(previous: Dict[str, Any], record: Dict[str, Any]) -> None:
    old_q, old_p = validate_source_fill(previous.get("q"), previous.get("p"))
    q, p = validate_source_fill(record.get("q"), record.get("p"))
    if record.get("oid") not in (None, ""):
        if q < old_q:
            raise RuntimeError("Regressing cumulative lead fill")
    elif (q, p) != (old_q, old_p):
        raise RuntimeError("Conflicting individual lead fill identity")


def row_hash(d: Dict[str, Any], keys: Sequence[str]) -> str:
    raw = json.dumps([d.get(k) for k in keys], sort_keys=True).encode("utf-8")
    return hashlib.md5(raw).hexdigest()


def is_open_leg(side: Optional[str], ps: Optional[str]) -> bool:
    return (ps == "SHORT" and side == "SELL") or (ps == "LONG" and side == "BUY")


def direction_label(side: Optional[str], ps: Optional[str], closing: bool) -> str:
    if ps == "LONG":
        return "OPEN_LONG" if side == "BUY" else ("CLOSE_LONG" if closing else "REDUCE_LONG")
    if ps == "SHORT":
        return "OPEN_SHORT" if side == "SELL" else ("CLOSE_SHORT" if closing else "REDUCE_SHORT")
    return side or "UNKNOWN"


def _realized_pnl(value: Any) -> Optional[Decimal]:
    # 收益字段只作展示证据；损坏、溢出或浮点下溢均不补成零。
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        pnl = exact(value)
        displayed = float(pnl)
    except (ValueError, TypeError, ArithmeticError, OverflowError):
        return None
    return pnl if math.isfinite(displayed) and (pnl == 0 or displayed != 0) else None


def slim_order(o: Dict[str, Any]) -> Dict[str, Any]:
    t = o.get("orderUpdateTime") or o.get("orderTime") or 0
    ot = o.get("orderTime") or t
    q, p = validate_source_fill(o.get("executedQty"), o.get("avgPrice"))
    # 收益的占位零与真实零分开；布尔值不是有效的财务证据。
    pnl = _realized_pnl(o.get("totalPnl")) if o.get("pnlKnown", True) is True else None
    result = {
        "h": order_dedup_key(o),
        "oid": o.get("orderId"),
        "fillKind": source_fill_kind(o),
        "t": t,
        "ot": ot,
        "sym": o.get("symbol"),
        "side": o.get("side"),
        "ps": o.get("positionSide"),
        "ty": o.get("type"),
        "q": str(q),
        "p": str(p),
        "pnl": float(pnl) if pnl is not None else 0.0,
        "pnlKnown": pnl is not None,  # 看板区分真实零收益与历史缺失值。
    }
    legacy_hash = hashlib.sha1(json.dumps([
        ot, o.get("symbol"), o.get("side"), o.get("positionSide"), o.get("type"), float(q), float(p),
    ], sort_keys=True).encode()).hexdigest()[:24]
    result["legacyKeys"] = [key for key in dict.fromkeys([
        *(o.get("_legacy_keys") or []), legacy_hash,
    ]) if key != result["h"]]
    result["legacySourceKeys"] = list(o.get("_legacy_source_keys") or [])
    return result


def expand_slim_order(o: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize slim stored orders back to Binance order-history schema."""
    if "symbol" in o and "executedQty" in o:
        # 原始 API 明确给出的零收益是已知，显式否认的证据不得反向提升。
        pnl = _realized_pnl(o.get("totalPnl")) if o.get("pnlKnown", True) is True else None
        return {**o, "totalPnl": o.get("totalPnl") if pnl is not None else None, "pnlKnown": pnl is not None}
    # 旧缓存只有零值存在占位歧义；非零收益不能在迁移时被抹掉。
    pnl = _realized_pnl(o.get("pnl"))
    pnl_known = pnl is not None and (o.get("pnlKnown") is True or ("pnlKnown" not in o and pnl != 0))
    return {
        "orderId": o.get("oid"),
        "symbol": o.get("sym"),
        "side": o.get("side"),
        "positionSide": o.get("ps"),
        "type": o.get("ty"),
        "orderTime": o.get("ot") or o.get("t") or 0,
        "orderUpdateTime": o.get("t") or o.get("ot") or 0,
        "executedQty": o.get("q") or 0.0,
        "avgPrice": o.get("p") or 0.0,
        "totalPnl": float(pnl) if pnl_known else None,  # 缓存重写不补造收益证据。
        "pnlKnown": pnl_known,
        "_legacy_keys": o.get("legacyKeys") or [],
        "_legacy_source_keys": o.get("legacySourceKeys") or [],
    }


def slim_closed_position(p: Dict[str, Any]) -> Dict[str, Any]:
    opened = p.get("opened")
    closed = p.get("closed")
    # 闭仓周期也保留真实零与缺失收益的区别，供持久化看板投影。
    pnl = _realized_pnl(p.get("closingPnl")) if p.get("pnlKnown", True) is True else None
    return {
        "id": str(p.get("positionId")),
        "symbol": p.get("symbol"),
        "side": p.get("side"),
        "leverage": p.get("leverage"),
        "isolated": p.get("isolated"),
        "avgCost": fnum(p.get("avgCost")),
        "avgClosePrice": fnum(p.get("avgClosePrice")),
        "maxQty": fnum(p.get("maxOpenInterest")),
        "pnl": float(pnl) if pnl is not None else None,
        "pnlKnown": pnl is not None,
        "roi": fnum(p.get("roi")),
        "opened": opened,
        "closed": closed,
        "heldMs": (interval[1] - interval[0]) if (interval := closed_interval(p)) is not None else None,
    }


def reconstruct_open_positions(
    store: Dict[str, Dict[str, Any]],
    closed_rows: Sequence[Dict[str, Any]] = (),
) -> List[Dict[str, Any]]:
    """Walk net position chronologically per (symbol, positionSide) and reconcile with position-history."""
    orders = sorted(store.values(), key=lambda o: (o.get("ot") or o.get("t") or 0, o.get("h") or ""))
    net: Dict[Tuple[str, str], Decimal] = defaultdict(Decimal)
    pv: Dict[Tuple[str, str], Decimal] = defaultdict(Decimal)
    legs: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    open_time: Dict[Tuple[str, str], int] = {}

    for o in orders:
        sym = o.get("sym")
        ps = o.get("ps")
        if not sym or not ps:
            continue
        k = (sym, ps)
        q, p = validate_source_fill(o.get("q"), o.get("p"))
        t = int(o.get("ot") or o.get("t") or 0)
        if is_open_leg(o.get("side"), ps):
            if net[k] == 0:
                open_time[k] = t
                legs[k] = []
            net[k] += q
            pv[k] += q * p
            legs[k].append({"t": t, "q": float(q), "p": float(p)})
        else:
            previous = net[k]
            removed = min(q, previous)
            if previous > 0:
                pv[k] *= (previous - removed) / previous
            net[k] = max(Decimal("0"), previous - q)
            if net[k] == 0:
                pv[k] = Decimal("0")
                legs[k] = []
                open_time.pop(k, None)

    out: List[Dict[str, Any]] = []
    for k, q in net.items():
        if q <= 0:
            continue
        sym, ps = k
        ot = open_time.get(k)
        stale = False
        if ot:
            for c in closed_rows:
                interval = closed_interval(c)
                if (
                    c.get("symbol") == sym
                    and str(c.get("side") or "").upper() == ps
                    and interval is not None and interval[0] <= ot <= interval[1]
                ):
                    stale = True
                    break
        if stale:
            continue
        avg = pv[k] / q if q else None
        out.append(
            {
                "symbol": sym,
                "baseAsset": sym[:-4] if sym.endswith(("USDT", "USDC")) else sym,  # 只移除结算币后缀。
                "positionSide": ps,
                "qty": float(q),
                "avgEntry": float(avg) if avg else None,
                "notional": float(q * avg) if avg else None,
                "openTime": ot,
                "entryOrders": sorted(legs[k], key=lambda x: x["t"]),
                "source": "derived",
            }
        )
    out.sort(key=lambda x: x.get("openTime") or 0)
    return out


def _parse_holdings_leverage(value: Any) -> Optional[int]:
    """Parse a direct-holdings leverage; invalid/missing values become None, never an error.

    Public leaders publish leverage on their positions (e.g. 49); the value is
    informational only, so a bad value must never invalidate the whole snapshot.
    """
    try:
        lev = int(float(value)) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None
    return lev if lev is not None and lev > 0 else None


def normalize_holdings(holdings: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize direct positions; an unknown row invalidates the entire snapshot."""
    positions: List[Dict[str, Any]] = []
    seen = set()
    for row in holdings:
        if not isinstance(row, dict):
            raise ValueError("Malformed direct lead position")
        symbol = row.get("symbol")
        amount = next((row[key] for key in ("positionAmount", "positionAmt", "qty") if row.get(key) is not None), None)
        if not symbol or amount is None:
            raise ValueError("Lead position lacks symbol or quantity")
        qty = exact(amount)
        if qty == 0:
            continue
        side = str(row.get("positionSide") or "").upper()
        if side == "BOTH":
            side = "LONG" if qty > 0 else "SHORT"
        if side not in ("LONG", "SHORT"):
            raise ValueError("Unknown lead position side")
        key = (symbol, side)
        if key in seen:
            raise ValueError("Duplicate direct lead position")
        seen.add(key)
        entry = exact(next((row[key] for key in ("entryPrice", "avgEntry", "avgCost") if row.get(key) is not None), 0))
        if entry <= 0:
            raise ValueError("Invalid lead entry price")
        positions.append({
            "symbol": symbol,
            "baseAsset": symbol[:-4] if symbol.endswith(("USDT", "USDC")) else symbol,
            "positionSide": side,
            "qty": float(abs(qty)),
            "qtyExact": str(abs(qty)),
            "avgEntry": float(entry),
            "notional": float(abs(qty) * entry),
            "openTime": row.get("openTime") or row.get("opened"),
            "entryOrders": [],
            "leverage": _parse_holdings_leverage(row.get("leverage")),
            "source": "api",
        })
    return positions


def snapshot_positions_observed_ms(snapshot: Dict[str, Any]) -> Optional[float]:
    """Never renew a fallback's holdings age using its publication time."""
    meta = snapshot.get("openPositionsMeta") or {}
    observed = meta.get("asOf")
    if observed is None:
        if "lastNonEmptyAsOf" in meta:
            observed = meta["lastNonEmptyAsOf"]
        elif meta.get("source") != "stale-fallback":
            observed = (snapshot.get("meta") or {}).get("generatedAt")
    return event_timestamp(observed) if type(observed) in (int, float) else None


def _closed_row_identifies_position(row: Dict[str, Any], position: Dict[str, Any], qty: Decimal) -> bool:
    """A closed-history row identifies the very cycle that held a vanished leg.

    The holdings endpoint can lag a real close: the last snapshot still shows
    the leg after it closed, so the close predates the observation cutoff and
    the time window alone rejects it. In that case the row's average cost
    identifies the cycle — it must match the leg's average entry to within
    float noise and its peak quantity must cover the leg. An old cycle reopened
    at a different price cannot match, and a row without an average cost or a
    peak quantity cannot identify the leg at all.
    """
    entry = fnum(position.get("avgEntry"))
    cost = fnum(row.get("avgCost"))
    peak = fnum(row.get("maxQty"))
    if entry is None or cost is None or peak is None:
        return False
    if not math.isclose(cost, entry, rel_tol=1e-6, abs_tol=1e-9):
        return False
    return exact(peak) >= qty


def positions_have_close_evidence(
    positions: Sequence[Dict[str, Any]],
    orders: Sequence[Dict[str, Any]],
    closed_rows: Sequence[Dict[str, Any]],
    observed_ms: Optional[float] = None,
) -> bool:
    """Require a complete closure for every leg, after its last known exposure.

    Old cycles, partial reductions and another symbol's close cannot explain an
    empty account. Use order creation time so a cumulative partial close updated
    after the snapshot cannot count its previously executed quantity again. A
    closed row may also identify the leg by average cost when the holdings
    endpoint lagged the real close (see _closed_row_identifies_position).
    """
    if not positions:
        return False
    for position in positions:
        symbol = position.get("symbol")
        side = str(position.get("positionSide") or "").upper()
        opened = event_timestamp(position.get("openTime"))
        cutoff = event_timestamp(observed_ms) if observed_ms is not None else opened
        qty = exact(position.get("qtyExact", position.get("qty")) or 0)
        if (not symbol or side not in ("LONG", "SHORT") or qty <= 0 or cutoff is None or
                (position.get("openTime") is not None and opened is None) or
                (opened is not None and opened > cutoff)):
            return False
        if any(
            row.get("symbol") == symbol
            and str(row.get("side") or row.get("positionSide") or "").upper() == side
            and (interval := closed_interval(row)) is not None
            and (opened is None or interval[0] <= opened)
            and (interval[1] > cutoff or _closed_row_identifies_position(row, position, qty))
            for row in closed_rows
        ):
            continue
        closed_qty = Decimal("0")
        added_qty = Decimal("0")
        intervals = []
        for order in orders:
            if (order.get("symbol", order.get("sym")) != symbol
                    or str(order.get("positionSide", order.get("ps")) or "").upper() != side):
                continue
            interval = event_interval(order.get("orderTime", order.get("ot", order.get("time", order.get("t")))),
                                      order.get("orderUpdateTime", order.get("t")))
            if interval is None:
                return False
            timestamp = interval[0]
            if timestamp <= cutoff:
                continue
            if any(max(timestamp, previous[0]) <= min(interval[1], previous[1]) for previous in intervals):
                return False
            intervals.append(interval)
            order_side = str(order.get("side") or "").upper()
            if order_side not in ("BUY", "SELL"):
                continue
            amount = exact(order.get("executedQty", order.get("q", order.get("qty"))) or 0)
            if amount <= 0:
                continue
            if is_open_leg(order_side, side):
                added_qty += amount
            else:
                closed_qty += amount
        if closed_qty < qty + added_qty:
            return False
    return True


def is_authoritative_snapshot(snapshot: Dict[str, Any], now_ms: Optional[int] = None, max_age_ms: int = 30000) -> bool:
    """Only fresh, complete public holdings can authorize orphan cleanup."""
    from app.services.lead_collector.snapshot_clock import snapshot_times
    if snapshot_times(snapshot, now_ms, max_age_ms) is None:
        return False
    meta = snapshot.get("openPositionsMeta") or {}
    if meta.get("partial") is True or (snapshot.get("meta") or {}).get("detailOk") is False:
        return False
    if meta.get("error") or meta.get("emptyVerified") is False:
        return False
    if meta.get("source") != "api" or meta.get("complete") is not True or meta.get("authoritative") is not True or meta.get("positionShow") is not True:
        return False
    observed = meta.get("asOf")
    if type(observed) not in (int, float) or not math.isfinite(observed):
        return False
    age = (int(time.time() * 1000) if now_ms is None else now_ms) - observed
    return 0 <= age <= max_age_ms and isinstance(snapshot.get("openPositions"), list)


def is_routable_snapshot(snapshot: Dict[str, Any], now_ms: Optional[int] = None, max_age_ms: int = 30000) -> bool:
    """Allow order following from healthy reconstructed holdings, including hidden positions."""
    from app.services.lead_collector.snapshot_clock import snapshot_times
    if snapshot_times(snapshot, now_ms, max_age_ms) is None:
        return False
    if is_authoritative_snapshot(snapshot, now_ms=now_ms, max_age_ms=max_age_ms):
        return True
    meta = snapshot.get("openPositionsMeta") or {}
    snapshot_meta = snapshot.get("meta") or {}
    if meta.get("partial") is True or snapshot_meta.get("detailOk") is False:
        return False
    if meta.get("error"):
        return False
    if not (meta.get("derived") is True or meta.get("reconstructed") is True
            or meta.get("source") in ("derived-from-order-history", "derived")
            or meta.get("positionShow") is False):
        return False
    if not isinstance(snapshot.get("openPositions"), list):
        return False
    observed = meta.get("asOf")
    if observed is None:
        observed = snapshot_meta.get("generatedAt")
    if observed is None:
        observed = snapshot_meta.get("collectionStartedAtMs")
    if type(observed) not in (int, float) or not math.isfinite(observed):
        return False
    age = (int(time.time() * 1000) if now_ms is None else now_ms) - observed
    return 0 <= age <= max_age_ms


def group_orders_into_batches(orders: Sequence[Dict[str, Any]], gap_ms: int = BATCH_GAP_MS) -> List[Dict[str, Any]]:
    """Group contiguous same-symbol/side/positionSide orders within gap_ms."""
    normalized = [expand_slim_order(o) for o in orders]
    asc = sorted(normalized, key=lambda o: (o.get("orderTime") or 0, order_dedup_key(o)))
    batches: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None

    for o in asc:
        sym = o.get("symbol") or ""
        side = o.get("side") or ""
        ps = o.get("positionSide") or ""
        ot = int(o.get("orderTime") or 0)
        ut = int(o.get("orderUpdateTime") or ot)
        q = exact(o.get("executedQty") or 0)
        p = exact(o.get("avgPrice") or 0)
        # 合并不能把未知收益当零；数量和来源键仍按原始成交字段计算。
        pnl = _realized_pnl(o.get("totalPnl")) if o.get("pnlKnown") is True else None
        item = {"time": ut, "orderTime": ot, "qty": float(q), "price": float(p),
                "pnl": float(pnl) if pnl is not None else None, "pnlKnown": pnl is not None,
                "sourceKey": order_dedup_key(o)}
        if q <= 0 or p <= 0:
            continue
        closing = not is_open_leg(side, ps)

        if (
            cur
            and cur["symbol"] == sym
            and cur["positionSide"] == ps
            and cur["side"] == side
            and ut - cur["endTime"] <= gap_ms
        ):
            cur["endTime"] = max(ut, cur["endTime"])
            cur["orders"] += 1
            cur["qty"] += q
            cur["_pv"] += q * p
            cur["pnl"] += pnl if pnl is not None else Decimal("0")
            cur["pnlKnown"] &= pnl is not None
            cur["items"].append(item)
        else:
            if cur:
                batches.append(cur)
            cur = {
                "key": hashlib.sha1(f"{sym}|{ps}|{side}|{ot}".encode()).hexdigest()[:20],
                "time": ut,
                "startTime": ot,
                "endTime": ut,
                "symbol": sym,
                "positionSide": ps,
                "side": side,
                "type": o.get("type"),
                "orders": 1,
                "qty": q,
                "_pv": q * p,
                "pnl": pnl if pnl is not None else Decimal("0"),
                "pnlKnown": pnl is not None,
                "isClose": closing,
                "items": [item],
            }
    if cur:
        batches.append(cur)

    for b in batches:
        pv = b.pop("_pv")
        b["avgPriceExact"] = str(pv / b["qty"]) if b["qty"] else None
        b["notionalExact"] = str(pv) if b["qty"] else None
        b["qtyExact"] = str(b["qty"])
        b["avgPrice"] = float(pv / b["qty"]) if b["qty"] else None
        b["notional"] = float(pv) if b["qty"] else None
        b["dir"] = direction_label(b["side"], b["positionSide"], b["isClose"])
        b["key"] = hashlib.sha1("|".join(item["sourceKey"] for item in b["items"]).encode()).hexdigest()[:20]
        b["orderCount"] = b["orders"]
        b["qty"] = float(b["qty"])
        # 每一笔证据完整且合计可表示时才发布总收益。
        pnl = _realized_pnl(b["pnl"]) if b["pnlKnown"] else None
        b["pnlKnown"] = pnl is not None
        b["pnl"] = float(pnl) if pnl is not None else None
        b["totalPnl"] = b["pnl"]

    batches.sort(key=lambda x: (x.get("startTime", 0), x.get("endTime", 0), x.get("key", "")))
    return batches


def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    hours = seconds // 3600
    mins = (seconds % 3600) // 60
    if hours < 24:
        return f"{hours}h {mins}m"
    days = hours // 24
    rem_h = hours % 24
    return f"{days}d {rem_h}h"


def reconstruct_trade_cycles(orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Reconstruct coherent trade cycles/rounds (同轮开平仓与K线复盘归组) per (symbol, positionSide)."""
    chronological = sorted(orders, key=lambda x: x.get("t") or x.get("time") or 0)
    groups: Dict[tuple[str, str], List[Dict[str, Any]]] = {}
    for o in chronological:
        sym = o.get("sym") or o.get("symbol")
        ps = o.get("ps") or o.get("positionSide")
        if sym and ps:
            groups.setdefault((sym, ps), []).append(o)

    cycles: List[Dict[str, Any]] = []
    for (sym, ps), ords in groups.items():
        cycle_idx = 1
        current_cycle: Optional[Dict[str, Any]] = None
        current_net_qty = 0.0

        for o in ords:
            side = o.get("side") or ""
            qty = fnum(o.get("q") or o.get("executedQty")) or 0.0
            px = fnum(o.get("p") or o.get("avgPrice")) or 0.0
            pnl = fnum(o.get("pnl") or o.get("totalPnl")) or 0.0
            t = int(o.get("t") or o.get("time") or o.get("orderTime") or 0)
            is_open = is_open_leg(side, ps)

            if current_cycle is None:
                if not is_open:
                    continue
                current_cycle = {
                    "cycleId": f"{sym}-{ps} #{cycle_idx}",
                    "symbol": sym,
                    "positionSide": ps,
                    "status": "OPEN",
                    "startTime": t,
                    "endTime": t,
                    "durationSec": 0,
                    "durationFormatted": "0s",
                    "entryCount": 1,
                    "exitCount": 0,
                    "totalEntryQty": qty,
                    "totalExitQty": 0.0,
                    "entryNotional": round(qty * px, 2),
                    "exitNotional": 0.0,
                    "avgEntryPrice": px,
                    "avgExitPrice": 0.0,
                    "realizedPnl": 0.0,
                    "scaleInCount": 0,
                    "orderCount": 1,
                }
                current_net_qty = qty
            else:
                current_cycle["endTime"] = t
                dur = max(0, int((t - current_cycle["startTime"]) / 1000))
                current_cycle["durationSec"] = dur
                current_cycle["durationFormatted"] = format_duration(dur)
                current_cycle["orderCount"] += 1

                if is_open:
                    current_cycle["entryCount"] += 1
                    current_cycle["scaleInCount"] += 1
                    current_cycle["totalEntryQty"] += qty
                    current_cycle["entryNotional"] = round(current_cycle["entryNotional"] + (qty * px), 2)
                    if current_cycle["totalEntryQty"] > 0:
                        current_cycle["avgEntryPrice"] = round(
                            current_cycle["entryNotional"] / current_cycle["totalEntryQty"], 6
                        )
                    current_net_qty += qty
                else:
                    current_cycle["exitCount"] += 1
                    current_cycle["totalExitQty"] += qty
                    current_cycle["exitNotional"] = round(current_cycle["exitNotional"] + (qty * px), 2)
                    current_cycle["realizedPnl"] = round(current_cycle["realizedPnl"] + pnl, 2)
                    if current_cycle["totalExitQty"] > 0:
                        current_cycle["avgExitPrice"] = round(
                            current_cycle["exitNotional"] / current_cycle["totalExitQty"], 6
                        )
                    current_net_qty -= qty

                if current_net_qty <= 1e-6 and current_cycle["totalExitQty"] > 0:
                    current_cycle["status"] = "CLOSED"
                    current_cycle["netRemainingQty"] = 0.0
                    cycles.append(current_cycle)
                    current_cycle = None
                    current_net_qty = 0.0
                    cycle_idx += 1

        if current_cycle is not None:
            current_cycle["status"] = "OPEN"
            current_cycle["netRemainingQty"] = round(current_net_qty, 6)
            cycles.append(current_cycle)

    cycles.sort(key=lambda c: c.get("endTime") or c.get("startTime") or 0, reverse=True)
    return cycles


class BinanceBapiCollector(BaseLeadCollector):
    """Async collector for Binance Futures public copy-trading bapi endpoints."""

    platform = "binance_bapi"
    exchange = "binance"
    _http_clients: dict = {}  # 公共采集连接按事件循环和代理复用。

    @classmethod
    async def close_clients(cls) -> None:
        # 摘除连接后统一清理，个别异常不能阻止释放其他出口。
        clients = list(cls._http_clients.values())
        cls._http_clients.clear()
        await ProxyManager.close_clients(clients)

    @classmethod
    async def close_proxy_clients(cls, proxy_url: Optional[str]) -> None:
        # 代理修改或删除后释放旧出口，不能无限保留失效连接。
        clients = []
        for key in list(cls._http_clients):
            if key[1] == proxy_url:
                clients.append(cls._http_clients.pop(key))
        await ProxyManager.close_clients(clients)

    def _client(self, proxy_url, timeout):
        key = (asyncio.get_running_loop(), proxy_url)
        client = self._http_clients.get(key)
        if client is None or client.is_closed:
            client = ProxyManager.build_client(proxy_url=proxy_url, timeout=timeout, exchange=self.exchange)
            self._http_clients[key] = client
        return client

    def __init__(self, pa_analyzer=None):
        self.pa_analyzer = pa_analyzer
        self._brackets_map: Dict[str, Any] = {}
        self._brackets_ts: float = 0.0
        self._marks_cache: Dict[str, Dict[str, Any]] = {}
        self._perf_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._holdings_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
        self._chart_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
        self._alloc_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._detail_degraded_log_at: Dict[str, float] = {}

    @staticmethod
    def _headers(portfolio_id: str, time_range: str = "7D") -> Dict[str, str]:
        return {
            "User-Agent": ProxyManager.DEFAULT_HEADERS["User-Agent"],
            "Content-Type": "application/json",
            "lang": "zh-CN",
            "clienttype": "web",
            "origin": "https://www.binance.com",
            "referer": f"https://www.binance.com/zh-CN/copy-trading/lead-details/{portfolio_id}?timeRange={time_range}",
        }

    async def _http_json(
        self,
        url: str,
        portfolio_id: str = "",
        body: Optional[Dict[str, Any]] = None,
        proxy_url: Optional[str] = None,
        time_range: str = "7D",
        retries: int = 3,
        timeout: float = 8.0,
        retry_budget: Optional[float] = None,
        retry_backoff_cap: float = 2.4,
        retry_jitter: float = 0.0,
    ) -> Any:
        headers = self._headers(portfolio_id, time_range)
        last_err: Optional[Exception] = None
        last_response = None
        try:
            client = self._client(proxy_url, timeout)
            async with asyncio.timeout(retry_budget):
                for attempt in range(retries):
                    last_response = None
                    try:
                        if body is not None:
                            resp = await client.post(url, json=body, headers=headers, timeout=timeout)
                        else:
                            resp = await client.get(url, headers=headers, timeout=timeout)
                        last_response = resp
                        if resp.status_code != 200:
                            raise httpx.HTTPStatusError(
                                f"HTTP {resp.status_code}: {redact_proxy_userinfo(resp.text)[:160]}",
                                request=None, response=resp,
                            )
                        payload = resp.json()
                        code = payload.get("code")
                        if str(code) == BUSY_CODE:
                            raise BapiBusyError(f"bapi busy {BUSY_CODE}")
                        if str(code) == RATE_LIMIT_CODE:
                            raise BapiRateLimitError(f"bapi rate limit {RATE_LIMIT_CODE}: {payload.get('message')}")
                        if code not in ("000000", None) or payload.get("data") is None:
                            raise RuntimeError(f"bapi code={code} msg={payload.get('message')}")
                        return payload["data"]
                    except Exception as exc:
                        last_err = exc
                        if not is_retryable_exception(exc):
                            break
                        if attempt + 1 < retries:
                            delay = 0.8 * 2 ** attempt
                            if retry_jitter:
                                delay += random.uniform(0, retry_jitter)
                            await asyncio.sleep(min(delay, retry_backoff_cap))
        except TimeoutError as exc:
            last_err = exc
            last_response = None
        raise RuntimeError(
            f"Binance bapi failed ({redact_proxy_userinfo(url)}): {describe_exception(last_err, last_response)}"
        ) from last_err

    async def _fapi_get(
        self,
        path_and_query: str,
        proxy_url: Optional[str] = None,
        timeout: float = 10.0,
        retries: int = 4,
    ) -> Any:
        urls = [f"{FAPI_PRIMARY}{path_and_query}", f"{FAPI_FALLBACK}{path_and_query}"]
        last_err: Optional[Exception] = None
        last_response = None
        # 行情回补与 bapi 同为公共请求，复用出口连接但保留本次超时。
        client = self._client(proxy_url, timeout)
        for attempt in range(retries):
            last_response = None
            target = urls[attempt % len(urls)] if attempt >= 2 else urls[0]
            try:
                resp = await client.get(target, timeout=timeout)
                last_response = resp
                if resp.status_code != 200:
                    raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=None, response=resp)
                return resp.json()
            except Exception as exc:
                last_err = exc
                if not is_retryable_exception(exc):
                    break
                if attempt + 1 < retries:
                    await asyncio.sleep(0.3 * (attempt + 1))
        raise RuntimeError(
            f"fapi request failed ({redact_proxy_userinfo(path_and_query)}): {describe_exception(last_err, last_response)}"
        ) from last_err

    async def fetch_detail(self, portfolio_id: str, proxy_url: Optional[str] = None) -> Dict[str, Any]:
        # An independent retry budget avoids multiplying the generic HTTP retries.
        for attempt in range(3):
            try:
                return await self._http_json(
                    f"{FRIENDLY_COPY}/lead-portfolio/detail?portfolioId={quote(portfolio_id)}",
                    portfolio_id=portfolio_id,
                    proxy_url=proxy_url,
                    retries=1,
                )
            except Exception as exc:
                if attempt == 2 or not is_retryable_exception(exc):
                    raise
                await asyncio.sleep(min(0.5 * 2 ** attempt, 4.0))

    async def fetch_order_history(
        self,
        portfolio_id: str,
        page_number: int = 1,
        page_size: int = 100,
        proxy_url: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        safe_size = min(100, max(1, page_size))
        data = await self._http_json(
            f"{FRIENDLY_COPY}/lead-portfolio/order-history",
            portfolio_id=portfolio_id,
            body={"portfolioId": portfolio_id, "pageNumber": page_number, "pageSize": safe_size},
            proxy_url=proxy_url,
            retries=4,
            retry_budget=HISTORY_RETRY_BUDGET,
        )
        return (data or {}).get("list") or []

    async def fetch_position_history(
        self,
        portfolio_id: str,
        page_number: int = 1,
        page_size: int = 100,
        proxy_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        safe_size = min(100, max(1, page_size))
        return await self._http_json(
            f"{FRIENDLY_COPY}/lead-portfolio/position-history",
            portfolio_id=portfolio_id,
            body={"portfolioId": portfolio_id, "pageNumber": page_number, "pageSize": safe_size},
            proxy_url=proxy_url,
            retries=4,
            retry_budget=HISTORY_RETRY_BUDGET,
        )

    async def collect_closed_history(self, portfolio_id, closed_cache, previous_snapshot=None,
                                     proxy_url=None, max_pages=25):
        # 游标只在连续取回全部增量后前移；触顶不能用本轮残片制造下一轮的连续性。
        def key(row):
            identity = row.get("positionId", row.get("id"))
            interval = closed_interval(row)
            if identity in (None, "", "None") or interval is None:
                return None
            return [str(identity), row.get("symbol"), row.get("side"), *interval]

        meta = (previous_snapshot or {}).get("meta") or {}
        prior = sorted(closed_cache, key=lambda row: row.get("closed") or 0, reverse=True)
        anchor = meta.get("closedHistoryCursor") if "closedHistoryCursor" in meta else (
            next((value for row in prior if (value := key(row)) is not None), None))
        rows, seen = [], set()
        complete, total, newest = False, None, None
        last_closed = None
        for page_number in range(1, max_pages + 1):
            data = await self.fetch_position_history(
                portfolio_id, page_number=page_number, page_size=100, proxy_url=proxy_url)
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise ValueError("Malformed closed-position history page")
            page = data["list"]
            raw_total = data.get("total")
            if raw_total is not None:
                if isinstance(raw_total, bool) or not str(raw_total).isascii() or not str(raw_total).isdecimal():
                    raise ValueError("Invalid closed-position history total")
                page_total = int(raw_total)
                if total is not None and page_total != total:
                    break  # 翻页途中数据集变化，下一轮重新取得连续证据。
                total = page_total
            progress, reached_anchor = False, False
            for row in page:
                if not isinstance(row, dict):
                    raise ValueError("Malformed closed-position history record")
                identity = key(row)
                if identity is None:
                    return rows + page, False, total, anchor
                frozen = tuple(identity)
                if frozen in seen:
                    continue
                closed_at = identity[-1]
                if last_closed is not None and closed_at > last_closed:
                    return rows, False, total, anchor
                last_closed = closed_at
                newest = newest or identity
                seen.add(frozen)
                rows.append(row)
                progress = True
                reached_anchor |= identity == anchor
            if reached_anchor or (total is not None and len(seen) >= total):
                complete = True
                break
            if not progress or len(page) < 100:
                break
        return rows, complete, total, (newest if complete else anchor)

    async def fetch_performance(
        self, portfolio_id: str, time_range: str = "30D", proxy_url: Optional[str] = None
    ) -> Dict[str, Any]:
        return await self._http_json(
            f"{PUBLIC_COPY}/lead-portfolio/performance?portfolioId={quote(portfolio_id)}&timeRange={time_range}",
            portfolio_id=portfolio_id,
            proxy_url=proxy_url,
            time_range=time_range,
            retries=3,
        )

    async def fetch_holdings(self, portfolio_id: str, proxy_url: Optional[str] = None) -> List[Dict[str, Any]]:
        # The public holdings endpoint returns an empty list for some public
        # leaders (e.g. portfolio 5305355976370339073) while the friendly
        # endpoint — the one Binance's own web frontend uses — returns their
        # real positions. Prefer friendly; fall back to public only when the
        # friendly call fails or returns a malformed payload. An empty list
        # is a legitimate answer (flat leader) and does not trigger fallback.
        last_error: Optional[Exception] = None
        for base in (FRIENDLY_COPY, PUBLIC_COPY):
            try:
                data = await self._http_json(
                    f"{base}/lead-data/positions?portfolioId={quote(portfolio_id)}",
                    portfolio_id=portfolio_id,
                    proxy_url=proxy_url,
                    retries=3,
                    retry_backoff_cap=4.0,
                    retry_jitter=0.2,
                )
            except Exception as exc:
                last_error = exc
                continue
            if not isinstance(data, list):
                last_error = RuntimeError("Malformed direct holdings response")
                continue
            return data
        raise RuntimeError("Malformed direct holdings response") from last_error

    async def fetch_roi_chart(
        self, portfolio_id: str, time_range: str = "365D", proxy_url: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        try:
            data = await self._http_json(
                f"{PUBLIC_COPY}/lead-portfolio/chart-data?portfolioId={quote(portfolio_id)}&dataType=ROI&timeRange={time_range}",
                portfolio_id=portfolio_id,
                proxy_url=proxy_url,
                time_range=time_range,
                retries=3,
            )
            if isinstance(data, list):
                return [
                    {"t": x.get("dateTime"), "v": fnum(x.get("value")) or 0.0}
                    for x in data
                    if x.get("dateTime") is not None
                ]
            return []
        except Exception:
            return []

    async def fetch_allocation(
        self, portfolio_id: str, time_range: str = "90D", proxy_url: Optional[str] = None
    ) -> Dict[str, Any]:
        try:
            data = await self._http_json(
                f"{PUBLIC_COPY}/lead-portfolio/performance/coin?portfolioId={quote(portfolio_id)}&timeRange={time_range}",
                portfolio_id=portfolio_id,
                proxy_url=proxy_url,
                time_range=time_range,
                retries=3,
            )
            if isinstance(data, dict):
                return {
                    "timeRange": data.get("timeRange") or time_range,
                    "updateTime": data.get("updateTime"),
                    "items": data.get("data") or [],
                }
            return {"timeRange": time_range, "updateTime": None, "items": []}
        except Exception:
            return {"timeRange": time_range, "updateTime": None, "items": []}

    async def fetch_marks(
        self, symbols: Sequence[str], proxy_url: Optional[str] = None
    ) -> Dict[str, Dict[str, Any]]:
        want = {s for s in symbols if s}
        if not want:
            return {}
        pi_map: Dict[str, Any] = {}
        t_map: Dict[str, Tuple[float, Any]] = {}

        async def _get_pi():
            arr = await self._fapi_get("/fapi/v1/premiumIndex", proxy_url=proxy_url, timeout=12.0, retries=3)
            if isinstance(arr, list):
                for it in arr:
                    if it.get("symbol") in want:
                        pi_map[it["symbol"]] = it

        async def _get_tp():
            arr = await self._fapi_get("/fapi/v1/ticker/price", proxy_url=proxy_url, timeout=12.0, retries=3)
            if isinstance(arr, list):
                for it in arr:
                    s = it.get("symbol")
                    if s in want:
                        px = fnum(it.get("price"))
                        if px:
                            t_map[s] = (px, it.get("time"))

        await asyncio.gather(_get_pi(), _get_tp(), return_exceptions=True)

        now_ms = int(time.time() * 1000)
        out: Dict[str, Dict[str, Any]] = {}
        for s in want:
            it = pi_map.get(s) or {}
            tp = t_map.get(s)
            mp = fnum(it.get("markPrice"))
            lp = tp[0] if tp else None
            cur = lp or mp
            if cur:
                rec = {
                    "lastPrice": lp or mp,
                    "markPrice": mp or lp,
                    "currentPrice": cur,
                    "indexPrice": fnum(it.get("indexPrice")),
                    "fundingRate": fnum(it.get("lastFundingRate")),
                    "nextFundingTime": it.get("nextFundingTime"),
                    "priceTime": max(int(it.get("time") or 0), int((tp[1] if tp else 0) or 0)) or now_ms,
                    "source": "ticker+premiumIndex" if (lp and mp) else ("ticker" if lp else "premiumIndex"),
                }
                self._marks_cache[s] = rec
                out[s] = rec
            elif s in self._marks_cache:
                out[s] = dict(self._marks_cache[s], source="cache")
        return out

    async def fetch_brackets(self, proxy_url: Optional[str] = None) -> Dict[str, Any]:
        if self._brackets_map and (time.time() - self._brackets_ts < BRACKET_TTL_S):
            return self._brackets_map
        try:
            j = await self._http_json(
                f"{FRIENDLY_ROOT}/common/brackets",
                proxy_url=proxy_url,
                retries=3,
                timeout=25.0,
            )
            rows = (j or {}).get("brackets") or []
            m: Dict[str, Any] = {}
            for it in rows:
                sym = it.get("symbol")
                tiers = []
                for b in it.get("riskBrackets") or []:
                    tiers.append(
                        {
                            "floor": fnum(b.get("bracketNotionalFloor")) or 0.0,
                            "cap": fnum(b.get("bracketNotionalCap")),
                            "mmr": fnum(b.get("bracketMaintenanceMarginRate")) or 0.05,
                            "cum": fnum(b.get("cumFastMaintenanceAmount")) or 0.0,
                            "maxLev": b.get("maxOpenPosLeverage"),
                        }
                    )
                tiers.sort(key=lambda t: t["floor"])
                if sym and tiers:
                    m[sym] = {"tiers": tiers, "updateTime": it.get("updateTime")}
            if m:
                self._brackets_map = m
                self._brackets_ts = time.time()
        except Exception:
            pass
        return self._brackets_map

    @staticmethod
    def mmr_tier(brk_map: Dict[str, Any], symbol: str, notional: Optional[float]) -> Optional[Dict[str, Any]]:
        if notional is None or notional <= 0:
            return None
        info = (brk_map or {}).get(symbol)
        if info:
            for idx, t in enumerate(info["tiers"]):
                cap = t["cap"]
                if notional <= (cap if cap is not None else float("inf")):
                    mm = notional * t["mmr"] - t["cum"]
                    return {
                        "tier": idx + 1,
                        "mmrPct": round(t["mmr"] * 100, 4),
                        "maintMargin": round(max(mm, 0.0), 2),
                        "maxLev": t["maxLev"],
                        "exact": True,
                    }
        return {
            "tier": None,
            "mmrPct": 5.0,
            "maintMargin": round(notional * 0.05, 2),
            "maxLev": None,
            "exact": False,
        }

    async def sync_orders_into_store(
        self,
        portfolio_id: str,
        store: Dict[str, Dict[str, Any]],
        full_sweep: bool = False,
        proxy_url: Optional[str] = None,
        history_max_pages: int = 25,
        require_complete: bool = False,
    ) -> int:
        # Work on a copy: a page gap must not partially advance the durable source cursor.
        pending = {}
        for old in store.values():
            expanded = expand_slim_order(old)
            rec = slim_order(expanded)
            rec["legacyKeys"] = [key for key in dict.fromkeys([
                *(rec.get("legacyKeys") or []), old.get("h"),
            ]) if key and key != rec["h"]][:8]
            previous = pending.get(rec["h"])
            if previous is None or exact(rec["q"]) >= exact(previous["q"]):
                pending[rec["h"]] = rec
        existing_keys = set(pending)
        new_count = 0
        dup_streak = 0
        max_pages = history_max_pages if full_sweep else 3
        complete = False
        for pn in range(1, max_pages + 1):
            lst = await self.fetch_order_history(
                portfolio_id, page_number=pn, page_size=100, proxy_url=proxy_url
            )
            added = 0
            for raw in lst:
                rec = slim_order(raw)
                previous = pending.get(rec["h"])
                # Older stores discarded orderId. Adopt its newly available identity
                # without retaining a second copy of the same cumulative execution.
                legacy_key = None
                if previous is None and rec.get("oid") not in (None, ""):
                    candidates = [key for key, old in pending.items() if old.get("oid") in (None, "")
                                  and all(old.get(k) == rec.get(k) for k in ("ot", "sym", "side", "ps", "ty"))]
                    if len(candidates) > 1:
                        raise RuntimeError("Ambiguous legacy cumulative lead identity")
                    legacy_key = candidates[0] if candidates else None
                if legacy_key is not None:
                    previous = pending.pop(legacy_key)
                    rec["legacySourceKeys"] = list(dict.fromkeys([*(previous.get("legacySourceKeys") or []), legacy_key]))
                    if legacy_key in existing_keys:
                        existing_keys.add(rec["h"])
                if previous is not None:
                    rec["legacyKeys"] = list(dict.fromkeys([
                        *(previous.get("legacyKeys") or []), *(rec.get("legacyKeys") or []),
                    ]))[:8]
                    rec["legacySourceKeys"] = rec["legacySourceKeys"] or previous.get("legacySourceKeys") or []
                if previous is not None:
                    check_source_fill_update(previous, rec)
                if previous is None:
                    added += 1
                pending[rec["h"]] = rec
                if rec["h"] in existing_keys and not require_complete:
                    complete = True
            new_count += added
            if len(lst) < 100:
                complete = True
                break
            if not lst or added == 0:
                dup_streak += 1
            else:
                dup_streak = 0
            if dup_streak >= 2 and not require_complete:
                break
            await asyncio.sleep(0.15)

        if not complete:
            raise RuntimeError("Lead order history is truncated without continuity")
        if require_complete and len(pending) > STORE_MAX:
            raise RuntimeError("Complete lead history exceeds store capacity")
        trimmed = sorted(pending.values(), key=lambda x: (x.get("ot") or x.get("t") or 0, x["h"]))[-STORE_MAX:]
        store.clear()
        for item in trimmed:
            store[item["h"]] = item
        return new_count

    async def sync_snapshot(
        self,
        portfolio_id: str,
        orders_store: Dict[str, Dict[str, Any]],
        closed_cache: List[Dict[str, Any]],
        proxy_url: Optional[str] = None,
        full_sweep: bool = False,
        with_analysis: bool = True,
        closed_history: Optional[List[Dict[str, Any]]] = None,
        previous_snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        t0 = time.perf_counter()
        detail_ok = True
        detail_error = None
        previous_profile = dict((previous_snapshot or {}).get("profile") or {})
        try:
            detail = await self.fetch_detail(portfolio_id, proxy_url=proxy_url)
        except Exception as exc:
            detail_ok = False
            detail_error = describe_exception(exc)
            detail = dict(previous_profile)
            if detail.get("positionShow") is None:
                detail["positionShow"] = False
            now = time.monotonic()
            last_warn = self._detail_degraded_log_at.get(portfolio_id)
            if last_warn is None or now - last_warn >= DETAIL_DEGRADED_WARN_INTERVAL:
                self._detail_degraded_log_at[portfolio_id] = now
                log = logger.warning
            else:
                log = logger.debug
            log("Lead portfolio %s detail degraded: %s", portfolio_id, detail_error)
        added = await self.sync_orders_into_store(
            portfolio_id, orders_store, full_sweep=full_sweep, proxy_url=proxy_url,
            history_max_pages=STORE_MAX // 100 + 1 if closed_history is not None else 25,
            require_complete=closed_history is not None,
        )

        # History failures must reach the orchestrator's failure/alert handling.
        closed_rows = list(closed_cache)
        closed_total = None
        if closed_history is not None:
            lst, closed_total, closed_history_complete, closed_cursor = closed_history, len(closed_history), True, None
        else:
            lst, closed_history_complete, closed_total, closed_cursor = await self.collect_closed_history(
                portfolio_id, closed_cache, previous_snapshot, proxy_url)
        if lst:
            # 保留已知闭仓周期和刚补齐的增量，不再只保留首屏八十条。
            seen_c = {(str(p.get("id")), p.get("opened")): p for p in closed_rows}
            for p in lst:
                seen_c[(str(p.get("positionId")), p.get("opened"))] = slim_closed_position(p)
            closed_rows = list(seen_c.values())
            closed_rows.sort(key=lambda x: x.get("closed") or 0, reverse=True)
            if closed_history is None:
                closed_rows = closed_rows[:2500]

        # Failed or unexplained empty holdings are UNKNOWN. A previous position
        # remains useful for display for at most 60 seconds, never for execution.
        now_s = time.time()
        holdings: List[Dict[str, Any]] = []
        holdings_observed_ms = None
        holdings_error = None
        authoritative = False
        holdings_source = "derived-from-order-history"
        empty_verified = False
        previous_meta = (previous_snapshot or {}).get("openPositionsMeta") or {}
        previous_positions = (previous_snapshot or {}).get("openPositions") or previous_meta.get("lastNonEmptyPositions") or []
        previous_observed_ms = snapshot_positions_observed_ms(previous_snapshot or {})
        close_evidence = False
        open_positions = reconstruct_open_positions(orders_store, closed_rows=closed_rows)
        if detail.get("positionShow") is True:
            try:
                holdings = await self.fetch_holdings(portfolio_id, proxy_url=proxy_url)
                candidate_positions = normalize_holdings(holdings)
                if not candidate_positions and previous_positions:
                    close_evidence = previous_observed_ms is not None and positions_have_close_evidence(
                        previous_positions, list(orders_store.values()), closed_rows, previous_observed_ms,
                    )
                    if not close_evidence:
                        await asyncio.sleep(1.0)
                        holdings = await self.fetch_holdings(portfolio_id, proxy_url=proxy_url)
                        candidate_positions = normalize_holdings(holdings)
                        if not candidate_positions:
                            holdings_error = (
                                "Anomalous empty holdings response without close evidence; fallback to previous snapshot"
                            )
                if holdings_error is None:
                    open_positions = candidate_positions
                    holdings_observed_ms = int(time.time() * 1000)
                    holdings_source = "api"
                    authoritative = detail_ok
                    empty_verified = not candidate_positions
            except Exception as exc:
                holdings_error = describe_exception(exc)[:240]
                holdings = []
            if holdings_error:
                logger.warning("Lead portfolio %s holdings degraded: %s", portfolio_id, holdings_error)
                if (previous_positions and previous_observed_ms is not None
                        and 0 <= time.time() * 1000 - previous_observed_ms <= MAX_STALE_SNAPSHOT_SEC * 1000):
                    open_positions = copy.deepcopy(previous_positions)
                    holdings_source = "stale-fallback"
                    holdings_observed_ms = previous_observed_ms

        # Seed durable analysis data with an expired timestamp so this round
        # still attempts a refresh while retaining a fallback after a restart.
        cached_perf = self._perf_cache.get(portfolio_id)
        if (not cached_perf or not cached_perf[1]) and (previous_snapshot or {}).get("performance"):
            cached_perf = (0.0, dict(previous_snapshot["performance"]))
            self._perf_cache[portfolio_id] = cached_perf
        perf = dict(cached_perf[1]) if cached_perf else {}
        cached_chart = self._chart_cache.get(portfolio_id)
        if (not cached_chart or not cached_chart[1]) and (previous_snapshot or {}).get("roiChart365D"):
            cached_chart = (0.0, list(previous_snapshot["roiChart365D"]))
            self._chart_cache[portfolio_id] = cached_chart
        roi_chart = list(cached_chart[1]) if cached_chart else []
        cached_alloc = self._alloc_cache.get(portfolio_id)
        if (not cached_alloc or not cached_alloc[1].get("items")) and (previous_snapshot or {}).get("allocation"):
            cached_alloc = (0.0, dict(previous_snapshot["allocation"]))
            self._alloc_cache[portfolio_id] = cached_alloc
        allocation = dict(cached_alloc[1]) if cached_alloc else {}
        if with_analysis:
            if not cached_perf or not cached_perf[1] or now_s - cached_perf[0] >= 180.0:
                async def _get_perf(tr: str):
                    try:
                        return tr, await self.fetch_performance(portfolio_id, time_range=tr, proxy_url=proxy_url)
                    except Exception:
                        return tr, None
                perf_items = await asyncio.gather(*(_get_perf(tr) for tr in ("7D", "30D", "90D", "180D", "365D")))
                new_perf = {tr: val for tr, val in perf_items if val}
                if new_perf:
                    perf.update(new_perf)
                    self._perf_cache[portfolio_id] = (now_s, dict(perf))
            if not cached_chart or not cached_chart[1] or now_s - cached_chart[0] >= 300.0:
                try:
                    new_chart = await self.fetch_roi_chart(portfolio_id, time_range="365D", proxy_url=proxy_url)
                except Exception:
                    new_chart = None
                if new_chart:
                    roi_chart = list(new_chart)
                    self._chart_cache[portfolio_id] = (now_s, list(roi_chart))
            if not cached_alloc or not cached_alloc[1].get("items") or now_s - cached_alloc[0] >= 300.0:
                try:
                    new_alloc = await self.fetch_allocation(portfolio_id, time_range="90D", proxy_url=proxy_url)
                except Exception:
                    new_alloc = None
                if new_alloc and new_alloc.get("items"):
                    allocation = dict(new_alloc)
                    self._alloc_cache[portfolio_id] = (now_s, dict(allocation))

        margin_balance = fnum(detail.get("marginBalance"))
        if with_analysis:
            marks = {symbol: dict(quote, source="cache") for symbol, quote in self._marks_cache.items()}
            try:
                marks.update(await self.fetch_marks([p["symbol"] for p in open_positions], proxy_url=proxy_url) or {})
            except Exception:
                pass
            try:
                brackets = await self.fetch_brackets(proxy_url=proxy_url) or self._brackets_map
            except Exception:
                brackets = self._brackets_map
        else:
            marks = {
                row["symbol"]: {
                    "markPrice": fnum(row.get("markPrice")),
                    "currentPrice": fnum(row.get("markPrice")),
                    "priceTime": holdings_observed_ms,
                    "source": "direct-holdings",
                }
                for row in holdings if row.get("symbol")
            }
            brackets = self._brackets_map

        now_ms = int(time.time() * 1000)
        if detail.get("positionShow") is False and holdings_error is None and holdings_observed_ms is None:
            holdings_observed_ms = now_ms
        previous_prices = {p["symbol"]: p for p in (previous_snapshot or {}).get("openPositions") or [] if p.get("symbol")}

        def valid_price(*values: Any) -> Optional[float]:
            for value in values:
                price = fnum(value)
                if price is not None and price > 0:
                    return price
            return None

        for p in open_positions:
            m = marks.get(p["symbol"]) or {}
            previous_price = previous_prices.get(p["symbol"]) or {}
            mark = valid_price(m.get("markPrice"))
            last_px = valid_price(m.get("lastPrice"))
            cur_px = valid_price(m.get("currentPrice"), last_px, mark)
            inherited_price = False
            if mark is None:
                mark = valid_price(previous_price.get("markPrice"), previous_price.get("lastPrice"), previous_price.get("currentPrice"))
                inherited_price = mark is not None
                mark = mark or valid_price(last_px, cur_px)
            if cur_px is None:
                cur_px = valid_price(previous_price.get("currentPrice"), previous_price.get("lastPrice"), previous_price.get("markPrice"))
                inherited_price = inherited_price or cur_px is not None
            last_px = last_px or valid_price(previous_price.get("lastPrice"), cur_px, mark)
            p["lastPrice"] = last_px
            p["markPrice"] = mark
            p["currentPrice"] = cur_px
            p["indexPrice"] = m.get("indexPrice")
            p["fundingRate"] = m.get("fundingRate")
            p["nextFundingTime"] = m.get("nextFundingTime")
            p["priceTime"] = previous_price.get("priceTime") if inherited_price else m.get("priceTime")
            p["priceSource"] = "previous-snapshot" if inherited_price else m.get("source")

            qty = p.get("qty") or 0.0
            entry = p.get("avgEntry")
            if cur_px is not None and entry:
                diff_pct = (cur_px - entry) / entry * 100.0
                edge_pct = -diff_pct if p["positionSide"] == "SHORT" else diff_pct
                p["priceDiffPct"] = round(diff_pct, 4)
                p["priceEdgePct"] = round(edge_pct, 4)
            else:
                p["priceDiffPct"] = p["priceEdgePct"] = None

            p["entryValue"] = round(qty * entry, 2) if (entry and qty) else None
            p["markValue"] = round(qty * mark, 2) if (mark and qty) else None
            p["notionalLeverage"] = (
                round(p["markValue"] / margin_balance, 4)
                if (p.get("markValue") and margin_balance)
                else None
            )
            entry_val = p.get("entryValue")
            p["entryLeverage"] = (
                round(entry_val / margin_balance, 4)
                if (entry_val is not None and margin_balance)
                else None
            )
            if mark is not None and entry:
                upnl = (entry - mark) * qty if p["positionSide"] == "SHORT" else (mark - entry) * qty
                p["uPnL"] = round(upnl, 2)
                p["uPnLPct"] = round(upnl / p["entryValue"] * 100, 4) if p.get("entryValue") else None
                p["uPnLRoePct"] = round(upnl / margin_balance * 100, 4) if margin_balance else None
            else:
                p["uPnL"] = p["uPnLPct"] = p["uPnLRoePct"] = None

            t = self.mmr_tier(brackets, p["symbol"], p.get("markValue"))
            p["mmr"] = t
            maint_m = (t or {}).get("maintMargin")
            p["maintMargin"] = maint_m
            p["maintMarginPct"] = (t or {}).get("mmrPct")
            p["maintMarginRoePct"] = (
                round(maint_m / margin_balance * 100, 4)
                if (maint_m is not None and margin_balance)
                else None
            )
            p["heldMs"] = (now_ms - p["openTime"]) if p.get("openTime") else None
            entry_orders = p.get("entryOrders") or []
            p["entryOrdersCount"] = len(entry_orders)
            last_add = max((e.get("t") or 0) for e in entry_orders) if entry_orders else p.get("openTime")
            p["lastAddTime"] = last_add
            p["sinceLastAddMs"] = (now_ms - last_add) if last_add else None

        tot_mark = sum(p["markValue"] for p in open_positions if p.get("markValue"))
        tot_entry = sum(p["entryValue"] for p in open_positions if p.get("entryValue"))
        tot_upnl = sum(p["uPnL"] for p in open_positions if p.get("uPnL") is not None)
        tot_mm = sum((p.get("mmr") or {}).get("maintMargin") or 0 for p in open_positions)
        tot_lev = round(tot_mark / margin_balance, 4) if (margin_balance and tot_mark) else 0.0
        entry_lev = round(tot_entry / margin_balance, 4) if (margin_balance and tot_entry) else 0.0
        short_cnt = sum(1 for p in open_positions if p.get("positionSide") == "SHORT")
        long_cnt = sum(1 for p in open_positions if p.get("positionSide") == "LONG")
        prices_complete = all(p.get("markValue") is not None for p in open_positions)
        pnl_complete = all(p.get("uPnL") is not None for p in open_positions)
        exact_mmr = all(((p.get("mmr") or {}).get("exact", False)) for p in open_positions) if open_positions else True
        max_pos_lev = max((p.get("notionalLeverage") or 0.0 for p in open_positions), default=0.0)
        for p in open_positions:
            p["sharePct"] = round((p["markValue"] / tot_mark * 100.0), 2) if (prices_complete and tot_mark and p.get("markValue")) else None

        price_as_of = max((p.get("priceTime") or 0 for p in open_positions), default=now_ms) or None

        if with_analysis and self.pa_analyzer is not None and open_positions:
            analyzer = self.pa_analyzer

            async def _analyze(pos):
                try:
                    return await analyzer.analyze_position(
                        pos["symbol"],
                        pos["positionSide"],
                        pos.get("avgEntry"),
                        current_price=pos.get("currentPrice") or pos.get("markPrice"),
                        entry_orders=pos.get("entryOrders"),
                        proxy_url=proxy_url,
                    )
                except Exception as exc:
                    return {"ok": False, "reason": redact_proxy_userinfo(str(exc))}

            analyses = await asyncio.gather(*(_analyze(p) for p in open_positions))
            for pos, res in zip(open_positions, analyses):
                pos["analysis"] = res

        recent_raw = sorted(orders_store.values(), key=lambda x: x.get("t") or 0, reverse=True)[:200]
        batches = group_orders_into_batches(recent_raw)[-60:]
        trade_cycles = reconstruct_trade_cycles(list(orders_store.values()))[:40]
        orders_view = [
            {
                "time": o.get("t"),
                "orderTime": o.get("ot"),
                "symbol": o.get("sym"),
                "side": o.get("side"),
                "positionSide": o.get("ps"),
                "type": o.get("ty"),
                "qty": fnum(o.get("q")),
                "avgPrice": fnum(o.get("p")),
                "pnl": o.get("pnl") if o.get("pnlKnown") is True else None,
                "pnlKnown": o.get("pnlKnown", False),  # 展示缓存不能把缺失收益变成已知零。
                "isClose": not is_open_leg(o.get("side"), o.get("ps")),
                "dir": direction_label(o.get("side"), o.get("ps"), not is_open_leg(o.get("side"), o.get("ps"))),
            }
            for o in recent_raw
        ]

        tags_raw = detail.get("tagItemVos") or []
        tags = [t.get("tagDesc") or t.get("tagName") for t in tags_raw if (t.get("tagDesc") or t.get("tagName"))]
        if not tags:
            tags = detail.get("tag") or []
        if detail.get("signTradFi") and "TradFi" not in tags:
            tags = ["TradFi", *tags]

        snapshot = {
            "meta": {
                "generatedAt": now_ms,
                "priceUpdatedAt": price_as_of,
                "fetchMs": int((time.perf_counter() - t0) * 1000),
                "portfolioId": portfolio_id,
                "nickname": detail.get("nickname") or portfolio_id,
                "orderStoreSize": len(orders_store),
                "ordersAddedThisRound": added,
                "fullSweep": full_sweep,
                "orderHistoryComplete": True,  # 连续性验证失败会在 sync_orders_into_store 抛错，不发布成功快照。
                "closedHistoryComplete": closed_history_complete,
                "closedHistoryCursor": closed_cursor,
                "detailOk": detail_ok,
                "detailError": detail_error,
            },
            "profile": {
                "nickname": detail.get("nickname"),
                "avatarUrl": detail.get("avatarUrl"),
                "badgeName": detail.get("badgeName"),
                "status": detail.get("status"),
                "isPaused": detail.get("isPaused"),
                "positionShow": detail.get("positionShow"),
                "description": detail.get("description"),
                "tags": tags,
                "signTradFi": detail.get("signTradFi"),
                "startTime": detail.get("startTime"),
                "lastTradeTime": detail.get("lastTradeTime"),
                "aumAmount": fnum(detail.get("aumAmount")),
                "copyAum": fnum(detail.get("copyAum")),
                "marginBalance": margin_balance,
                "copierPnl": fnum(detail.get("copierPnl")),
                "currentCopyCount": detail.get("currentCopyCount"),
                "maxCopyCount": detail.get("maxCopyCount"),
                "fixedAmountMinCopyUsd": fnum(detail.get("fixedAmountMinCopyUsd")),
                "totalCopyCount": detail.get("totalCopyCount"),
                "favoriteCount": detail.get("favoriteCount"),
                "mockCopyCount": detail.get("mockCopyCount"),
                "profitSharingRate": detail.get("profitSharingRate"),
                "sharpRatio": fnum(detail.get("sharpRatio")),
                "aiSummary": detail.get("aiSummary"),
            },
            "performance": perf,
            "roiChart365D": roi_chart,
            "allocation": allocation,
            "holdings": holdings,
            "openPositions": open_positions,
            "openPositionsMeta": {
                "source": holdings_source,
                "reconstructed": holdings_source == "derived-from-order-history",
                "derived": holdings_source == "derived-from-order-history",
                "positionShow": detail.get("positionShow"),
                "asOf": holdings_observed_ms,
                "complete": authoritative,
                "authoritative": authoritative,
                "partial": not detail_ok or holdings_error is not None,
                "error": holdings_error or detail_error,
                "emptyVerified": empty_verified if not open_positions else None,
                "closeEvidence": close_evidence,
                **({
                    # Preserve the suspicion after display fallback expires. An
                    # expired derived empty set must not verify the next empty poll.
                    "lastNonEmptyPositions": copy.deepcopy(previous_positions),
                    "lastNonEmptyAsOf": previous_observed_ms,
                } if holdings_error and previous_positions else {}),
            },
            "openPositionsSummary": {
                "positionCount": len(open_positions),
                "shortCount": short_cnt,
                "longCount": long_cnt,
                "symbols": [p["symbol"] for p in open_positions],
                "totalEntryNotional": round(tot_entry, 2),
                "totalMarkNotional": round(tot_mark, 2),
                "totalNotionalLeverage": tot_lev,
                "entryLeverage": entry_lev,
                "maxPositionLeverage": round(max_pos_lev, 4) if open_positions else 0.0,
                "marginCoveragePct": round(margin_balance / tot_mark * 100, 2) if (margin_balance and tot_mark) else (100.0 if margin_balance else None),
                "estMaintenanceMargin": round(tot_mm, 2),
                "estMaintenanceMarginRatioPct": round(tot_mm / margin_balance * 100, 2) if (margin_balance and open_positions) else 0.0,
                "avgMmrPct": round(tot_mm / tot_mark * 100, 2) if (tot_mark and open_positions) else 0.0,
                "mmrSource": ("binance-official-brackets" if exact_mmr else "estimated-fallback-5pct") if open_positions else None,
                "totalUnrealizedPnl": round(tot_upnl, 2),
                "totalUnrealizedPnlPctOfEntry": round(tot_upnl / tot_entry * 100, 2) if (tot_entry and open_positions) else 0.0,
                "totalMarginRoePct": (
                    round(tot_upnl / margin_balance * 100, 4) if (margin_balance and open_positions) else 0.0
                ),
                "adverseMoveBufferPct": round((margin_balance - tot_mm) / tot_mark * 100, 2) if (margin_balance and tot_mark) else (100.0 if margin_balance else None),
                "availableMargin": round(margin_balance - tot_mm, 2) if margin_balance is not None else None,
                "marginBalance": margin_balance,
                "priceAsOf": price_as_of,
            },
            "orderBatches": batches,
            "tradeCycles": trade_cycles,
            "orders": orders_view,
            "closed": closed_rows,
            "closedTotal": closed_total,
        }
        if not detail_ok:
            snapshot["profile"] = {**previous_profile, "positionShow": detail.get("positionShow")}
        if margin_balance is None:
            for field in ("totalNotionalLeverage", "entryLeverage", "maxPositionLeverage",
                          "estMaintenanceMarginRatioPct", "totalMarginRoePct"):
                snapshot["openPositionsSummary"][field] = None
        # A missing quote is unknown, never a zero-valued position or account.
        if not prices_complete:
            for field in ("totalMarkNotional", "totalNotionalLeverage", "maxPositionLeverage",
                          "marginCoveragePct", "estMaintenanceMargin", "estMaintenanceMarginRatioPct",
                          "avgMmrPct", "adverseMoveBufferPct", "availableMargin", "mmrSource"):
                snapshot["openPositionsSummary"][field] = None
        if not pnl_complete:
            for field in ("totalUnrealizedPnl", "totalUnrealizedPnlPctOfEntry", "totalMarginRoePct"):
                snapshot["openPositionsSummary"][field] = None
        return snapshot
