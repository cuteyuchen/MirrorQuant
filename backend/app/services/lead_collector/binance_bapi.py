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


def slim_order(o: Dict[str, Any]) -> Dict[str, Any]:
    t = o.get("orderUpdateTime") or o.get("orderTime") or 0
    ot = o.get("orderTime") or t
    q, p = validate_source_fill(o.get("executedQty"), o.get("avgPrice"))
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
        "pnl": fnum(o.get("totalPnl")) or 0.0,
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
        return o
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
        "totalPnl": o.get("pnl") or 0.0,
        "_legacy_keys": o.get("legacyKeys") or [],
        "_legacy_source_keys": o.get("legacySourceKeys") or [],
    }


def slim_closed_position(p: Dict[str, Any]) -> Dict[str, Any]:
    opened = p.get("opened")
    closed = p.get("closed")
    return {
        "id": str(p.get("positionId")),
        "symbol": p.get("symbol"),
        "side": p.get("side"),
        "leverage": p.get("leverage"),
        "isolated": p.get("isolated"),
        "avgCost": fnum(p.get("avgCost")),
        "avgClosePrice": fnum(p.get("avgClosePrice")),
        "maxQty": fnum(p.get("maxOpenInterest")),
        "pnl": fnum(p.get("closingPnl")),
        "roi": fnum(p.get("roi")),
        "opened": opened,
        "closed": closed,
        "heldMs": (closed - opened) if (opened and closed) else None,
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
                if (
                    c.get("symbol") == sym
                    and str(c.get("side") or "").upper() == ps
                    and (c.get("opened") or 0) <= ot <= (c.get("closed") or 0)
                ):
                    stale = True
                    break
        if stale:
            continue
        avg = pv[k] / q if q else None
        out.append(
            {
                "symbol": sym,
                "baseAsset": sym.replace("USDT", ""),
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
            "baseAsset": symbol.removesuffix("USDT"),
            "positionSide": side,
            "qty": float(abs(qty)),
            "qtyExact": str(abs(qty)),
            "avgEntry": float(entry),
            "notional": float(abs(qty) * entry),
            "openTime": row.get("openTime") or row.get("opened"),
            "entryOrders": [],
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
    return fnum(observed)


def positions_have_close_evidence(
    positions: Sequence[Dict[str, Any]],
    orders: Sequence[Dict[str, Any]],
    closed_rows: Sequence[Dict[str, Any]],
    observed_ms: Optional[float] = None,
) -> bool:
    """Require a complete closure for every leg, after its last known exposure.

    Old cycles, partial reductions and another symbol's close cannot explain an
    empty account. Use order creation time so a cumulative partial close updated
    after the snapshot cannot count its previously executed quantity again.
    """
    if not positions:
        return False
    for position in positions:
        symbol = position.get("symbol")
        side = str(position.get("positionSide") or "").upper()
        opened = fnum(position.get("openTime"))
        cutoff = observed_ms if observed_ms is not None else opened
        qty = exact(position.get("qtyExact", position.get("qty")) or 0)
        if not symbol or side not in ("LONG", "SHORT") or qty <= 0 or cutoff is None:
            return False
        if any(
            row.get("symbol") == symbol
            and str(row.get("side") or row.get("positionSide") or "").upper() == side
            and (fnum(row.get("closed")) or 0) > cutoff
            and (opened is None or (fnum(row.get("opened")) or 0) <= opened)
            for row in closed_rows
        ):
            continue
        closed_qty = Decimal("0")
        added_qty = Decimal("0")
        for order in orders:
            if (order.get("symbol", order.get("sym")) != symbol
                    or str(order.get("positionSide", order.get("ps")) or "").upper() != side):
                continue
            timestamp = fnum(order.get("orderTime") or order.get("ot") or order.get("time") or order.get("t"))
            if timestamp is None or timestamp <= cutoff:
                continue
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
    """UNKNOWN, hidden, derived and stale holdings can never authorize financial actions."""
    meta = snapshot.get("openPositionsMeta") or {}
    if meta.get("partial") is True or (snapshot.get("meta") or {}).get("detailOk") is False:
        return False
    if meta.get("error") or meta.get("emptyVerified") is False:
        return False
    if meta.get("source") != "api" or meta.get("complete") is not True or meta.get("authoritative") is not True or meta.get("positionShow") is not True:
        return False
    observed = meta.get("asOf")
    if not isinstance(observed, (int, float)) or not math.isfinite(observed):
        return False
    age = (int(time.time() * 1000) if now_ms is None else now_ms) - observed
    return 0 <= age <= max_age_ms and isinstance(snapshot.get("openPositions"), list)


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
        pnl = exact(o.get("totalPnl") or 0)
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
            cur["pnl"] += pnl
            cur["items"].append({"time": ut, "orderTime": ot, "qty": float(q), "price": float(p), "pnl": float(pnl), "sourceKey": order_dedup_key(o)})
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
                "pnl": pnl,
                "isClose": closing,
                "items": [{"time": ut, "orderTime": ot, "qty": float(q), "price": float(p), "pnl": float(pnl), "sourceKey": order_dedup_key(o)}],
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
        b["pnl"] = float(b["pnl"])

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
            async with asyncio.timeout(retry_budget), ProxyManager.build_client(
                proxy_url=proxy_url, timeout=timeout, headers=headers, exchange=self.exchange,
            ) as client:
                for attempt in range(retries):
                    last_response = None
                    try:
                        if body is not None:
                            resp = await client.post(url, json=body)
                        else:
                            resp = await client.get(url)
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
        async with ProxyManager.build_client(
            proxy_url=proxy_url, timeout=timeout, exchange=self.exchange,
        ) as client:
            for attempt in range(retries):
                last_response = None
                target = urls[attempt % len(urls)] if attempt >= 2 else urls[0]
                try:
                    resp = await client.get(target)
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
        data = await self._http_json(
            f"{PUBLIC_COPY}/lead-data/positions?portfolioId={quote(portfolio_id)}",
            portfolio_id=portfolio_id,
            proxy_url=proxy_url,
            retries=3,
            retry_backoff_cap=4.0,
            retry_jitter=0.2,
        )
        if not isinstance(data, list):
            raise RuntimeError("Malformed direct holdings response")
        return data

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
        closed_history_complete = False
        ph1 = {"list": closed_history, "total": len(closed_history)} if closed_history is not None else await self.fetch_position_history(
            portfolio_id, page_number=1, page_size=100, proxy_url=proxy_url
        )
        lst = (ph1 or {}).get("list") or []
        closed_total = (ph1 or {}).get("total")
        closed_history_complete = closed_total is not None and int(closed_total) <= len(lst)
        if lst:
            seen_c: Dict[Tuple[str, Any], Dict[str, Any]] = {}
            for p in lst:
                seen_c[(str(p.get("positionId")), p.get("opened"))] = p
            closed_rows = [slim_closed_position(p) for p in seen_c.values()]
            closed_rows.sort(key=lambda x: x.get("closed") or 0, reverse=True)
            if closed_history is None:
                closed_rows = closed_rows[:80]

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
                    close_evidence = positions_have_close_evidence(
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

        # Optional dashboard requests cannot delay the execution collector.
        cached_perf = self._perf_cache.get(portfolio_id)
        perf = dict(cached_perf[1]) if cached_perf else {}
        cached_chart = self._chart_cache.get(portfolio_id)
        roi_chart = list(cached_chart[1]) if cached_chart else []
        cached_alloc = self._alloc_cache.get(portfolio_id)
        allocation = dict(cached_alloc[1]) if cached_alloc else {}
        if with_analysis:
            if not cached_perf or now_s - cached_perf[0] >= 180.0:
                async def _get_perf(tr: str):
                    try:
                        return tr, await self.fetch_performance(portfolio_id, time_range=tr, proxy_url=proxy_url)
                    except Exception:
                        return tr, None
                perf_items = await asyncio.gather(*(_get_perf(tr) for tr in ("7D", "30D", "90D", "180D", "365D")))
                perf = {tr: val for tr, val in perf_items if val is not None}
                if perf:
                    self._perf_cache[portfolio_id] = (now_s, perf)
            if not cached_chart or now_s - cached_chart[0] >= 300.0:
                roi_chart = await self.fetch_roi_chart(portfolio_id, time_range="365D", proxy_url=proxy_url)
                if roi_chart:
                    self._chart_cache[portfolio_id] = (now_s, roi_chart)
            if not cached_alloc or now_s - cached_alloc[0] >= 300.0:
                allocation = await self.fetch_allocation(portfolio_id, time_range="90D", proxy_url=proxy_url)
                if allocation.get("items"):
                    self._alloc_cache[portfolio_id] = (now_s, allocation)

        margin_balance = fnum(detail.get("marginBalance"))
        if with_analysis:
            marks = await self.fetch_marks([p["symbol"] for p in open_positions], proxy_url=proxy_url)
            brackets = await self.fetch_brackets(proxy_url=proxy_url)
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
        for p in open_positions:
            m = marks.get(p["symbol"]) or {}
            mark = m.get("markPrice")
            last_px = m.get("lastPrice") or mark
            cur_px = m.get("currentPrice") or last_px or mark
            p["lastPrice"] = last_px
            p["markPrice"] = mark
            p["currentPrice"] = cur_px
            p["indexPrice"] = m.get("indexPrice")
            p["fundingRate"] = m.get("fundingRate")
            p["nextFundingTime"] = m.get("nextFundingTime")
            p["priceTime"] = m.get("priceTime")
            p["priceSource"] = m.get("source")

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
        exact_mmr = all(((p.get("mmr") or {}).get("exact", True)) for p in open_positions) if open_positions else True
        max_pos_lev = max((p.get("notionalLeverage") or 0.0 for p in open_positions), default=0.0)
        for p in open_positions:
            p["sharePct"] = round((p["markValue"] / tot_mark * 100.0), 2) if (tot_mark and p.get("markValue")) else None

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
                "pnl": o.get("pnl"),
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
                "priceUpdatedAt": max((p.get("priceTime") or 0 for p in open_positions), default=now_ms) or now_ms,
                "fetchMs": int((time.perf_counter() - t0) * 1000),
                "portfolioId": portfolio_id,
                "nickname": detail.get("nickname") or portfolio_id,
                "orderStoreSize": len(orders_store),
                "ordersAddedThisRound": added,
                "fullSweep": full_sweep,
                "closedHistoryComplete": closed_history_complete,
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
                "priceAsOf": now_ms,
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
        return snapshot
