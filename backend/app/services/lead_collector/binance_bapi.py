from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

import httpx

from app.services.lead_collector.base import BaseLeadCollector
from app.services.proxy.manager import ProxyManager

FRIENDLY_ROOT = "https://www.binance.com/bapi/futures/v1/friendly/future"
FRIENDLY_COPY = f"{FRIENDLY_ROOT}/copy-trade"
PUBLIC_COPY = "https://www.binance.com/bapi/futures/v1/public/future/copy-trade"
FAPI_PRIMARY = "https://www.binance.com"
FAPI_FALLBACK = "https://fapi.binance.com"

BUSY_CODE = "11012005"
BATCH_GAP_MS = 30 * 60 * 1000
STORE_MAX = 6000
BRACKET_TTL_S = 6 * 3600


def fnum(val: Any) -> Optional[float]:
    try:
        if val is None or val == "":
            return None
        return float(val)
    except (TypeError, ValueError):
        return None


def order_dedup_key(o: Dict[str, Any]) -> str:
    """Stable SHA-1 dedup key excluding mutable orderUpdateTime."""
    raw = json.dumps(
        [
            o.get("orderTime"),
            o.get("symbol"),
            o.get("side"),
            o.get("positionSide"),
            o.get("type"),
            o.get("executedQty"),
            o.get("avgPrice"),
        ],
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:24]


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
    q = fnum(o.get("executedQty")) or 0.0
    p = fnum(o.get("avgPrice")) or 0.0
    return {
        "h": order_dedup_key(o),
        "t": t,
        "ot": ot,
        "sym": o.get("symbol"),
        "side": o.get("side"),
        "ps": o.get("positionSide"),
        "ty": o.get("type"),
        "q": q,
        "p": p,
        "pnl": fnum(o.get("totalPnl")) or 0.0,
    }


def expand_slim_order(o: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize slim stored orders back to Binance order-history schema."""
    if "symbol" in o and "executedQty" in o:
        return o
    return {
        "symbol": o.get("sym"),
        "side": o.get("side"),
        "positionSide": o.get("ps"),
        "type": o.get("ty"),
        "orderTime": o.get("ot") or o.get("t") or 0,
        "orderUpdateTime": o.get("t") or o.get("ot") or 0,
        "executedQty": o.get("q") or 0.0,
        "avgPrice": o.get("p") or 0.0,
        "totalPnl": o.get("pnl") or 0.0,
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
    orders = sorted(store.values(), key=lambda o: o.get("t") or 0)
    net: Dict[Tuple[str, str], float] = defaultdict(float)
    pv: Dict[Tuple[str, str], float] = defaultdict(float)
    legs: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    open_time: Dict[Tuple[str, str], int] = {}

    for o in orders:
        sym = o.get("sym")
        ps = o.get("ps")
        if not sym or not ps:
            continue
        k = (sym, ps)
        q = float(o.get("q") or 0.0)
        p = float(o.get("p") or 0.0)
        t = int(o.get("t") or 0)
        if q <= 0:
            continue
        if is_open_leg(o.get("side"), ps):
            if abs(net[k]) < 1e-9:
                open_time[k] = t
                legs[k] = []
            net[k] += q
            pv[k] += q * p
            legs[k].append({"t": t, "q": q, "p": p})
        else:
            net[k] -= q
            if net[k] <= 1e-9:
                net[k] = 0.0
                pv[k] = 0.0
                legs[k] = []
                open_time.pop(k, None)

    tol_ms = 120 * 1000
    out: List[Dict[str, Any]] = []
    for k, q in net.items():
        if q <= 1e-9:
            continue
        sym, ps = k
        ot = open_time.get(k)
        stale = False
        if ot:
            for c in closed_rows:
                if (
                    c.get("symbol") == sym
                    and str(c.get("side") or "").upper() == ps
                    and (c.get("opened") or 0) - tol_ms <= ot <= (c.get("closed") or 0) + tol_ms
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
                "qty": round(q, 6),
                "avgEntry": round(avg, 6) if avg else None,
                "notional": round(q * avg, 2) if avg else None,
                "openTime": ot,
                "entryOrders": sorted(legs[k], key=lambda x: x["t"]),
                "source": "derived",
            }
        )
    out.sort(key=lambda x: x.get("openTime") or 0)
    return out


def group_orders_into_batches(orders: Sequence[Dict[str, Any]], gap_ms: int = BATCH_GAP_MS) -> List[Dict[str, Any]]:
    """Group contiguous same-symbol/side/positionSide orders within gap_ms."""
    normalized = [expand_slim_order(o) for o in orders]
    asc = sorted(normalized, key=lambda o: o.get("orderTime") or 0)
    batches: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None

    for o in asc:
        sym = o.get("symbol") or ""
        side = o.get("side") or ""
        ps = o.get("positionSide") or ""
        ot = int(o.get("orderTime") or 0)
        ut = int(o.get("orderUpdateTime") or ot)
        q = float(o.get("executedQty") or 0.0)
        p = float(o.get("avgPrice") or 0.0)
        pnl = float(o.get("totalPnl") or 0.0)
        closing = not is_open_leg(side, ps)

        if (
            cur
            and cur["symbol"] == sym
            and cur["positionSide"] == ps
            and cur["side"] == side
            and ut - cur["endTime"] <= gap_ms
        ):
            cur["endTime"] = ut
            cur["orders"] += 1
            cur["qty"] += q
            cur["_pv"] += q * p
            cur["pnl"] += pnl
            cur["items"].append({"time": ut, "orderTime": ot, "qty": q, "price": p, "pnl": pnl})
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
                "items": [{"time": ut, "orderTime": ot, "qty": q, "price": p, "pnl": pnl}],
            }
    if cur:
        batches.append(cur)

    for b in batches:
        pv = b.pop("_pv")
        b["avgPrice"] = round(pv / b["qty"], 6) if b["qty"] else None
        b["notional"] = round(pv, 2) if b["qty"] else None
        b["dir"] = direction_label(b["side"], b["positionSide"], b["isClose"])
        b["key"] = hashlib.sha1(f"{b['key']}|{b['orders']}|{round(b['qty'], 6)}".encode()).hexdigest()[:20]
        b["orderCount"] = b["orders"]
        b["qty"] = round(b["qty"], 6)
        b["pnl"] = round(b["pnl"], 4)

    batches.sort(key=lambda x: x["time"], reverse=True)
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

    def __init__(self, pa_analyzer=None):
        self.pa_analyzer = pa_analyzer
        self._brackets_map: Dict[str, Any] = {}
        self._brackets_ts: float = 0.0
        self._marks_cache: Dict[str, Dict[str, Any]] = {}

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
        retries: int = 5,
        timeout: float = 20.0,
    ) -> Any:
        headers = self._headers(portfolio_id, time_range)
        last_err: Optional[Exception] = None
        async with ProxyManager.build_client(proxy_url=proxy_url, timeout=timeout, headers=headers) as client:
            for attempt in range(retries):
                try:
                    if body is not None:
                        resp = await client.post(url, json=body)
                    else:
                        resp = await client.get(url)
                    if resp.status_code != 200:
                        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:160]}")
                    payload = resp.json()
                    code = payload.get("code")
                    if code == BUSY_CODE:
                        last_err = RuntimeError(f"bapi busy {BUSY_CODE}")
                        await asyncio.sleep(min(0.8 * (attempt + 1), 6.0))
                        continue
                    if code not in ("000000", None) or payload.get("data") is None:
                        raise RuntimeError(f"bapi code={code} msg={payload.get('message')}")
                    return payload["data"]
                except Exception as exc:
                    last_err = exc
                    if attempt + 1 < retries:
                        await asyncio.sleep(min(0.8 * (attempt + 1), 5.0))
        raise RuntimeError(f"Binance bapi failed ({url}): {last_err}")

    async def _fapi_get(
        self,
        path_and_query: str,
        proxy_url: Optional[str] = None,
        timeout: float = 10.0,
        retries: int = 4,
    ) -> Any:
        urls = [f"{FAPI_PRIMARY}{path_and_query}", f"{FAPI_FALLBACK}{path_and_query}"]
        last_err: Optional[Exception] = None
        async with ProxyManager.build_client(proxy_url=proxy_url, timeout=timeout) as client:
            for attempt in range(retries):
                target = urls[attempt % len(urls)] if attempt >= 2 else urls[0]
                try:
                    resp = await client.get(target)
                    if resp.status_code != 200:
                        raise RuntimeError(f"HTTP {resp.status_code}")
                    return resp.json()
                except Exception as exc:
                    last_err = exc
                    if attempt + 1 < retries:
                        await asyncio.sleep(0.3 * (attempt + 1))
        raise RuntimeError(f"fapi request failed ({path_and_query}): {last_err}")

    async def fetch_detail(self, portfolio_id: str, proxy_url: Optional[str] = None) -> Dict[str, Any]:
        return await self._http_json(
            f"{FRIENDLY_COPY}/lead-portfolio/detail?portfolioId={quote(portfolio_id)}",
            portfolio_id=portfolio_id,
            proxy_url=proxy_url,
        )

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
        try:
            data = await self._http_json(
                f"{PUBLIC_COPY}/lead-data/positions?portfolioId={quote(portfolio_id)}",
                portfolio_id=portfolio_id,
                proxy_url=proxy_url,
                retries=2,
            )
            return data if isinstance(data, list) else []
        except Exception:
            return []

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
                retries=2,
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
    ) -> int:
        new_count = 0
        dup_streak = 0
        max_pages = 25 if full_sweep else 3
        for pn in range(1, max_pages + 1):
            try:
                lst = await self.fetch_order_history(
                    portfolio_id, page_number=pn, page_size=100, proxy_url=proxy_url
                )
            except Exception:
                dup_streak += 1
                if dup_streak >= 2:
                    break
                continue
            added = 0
            for raw in lst:
                rec = slim_order(raw)
                if rec["h"] not in store:
                    store[rec["h"]] = rec
                    added += 1
            new_count += added
            if not lst or added == 0:
                dup_streak += 1
            else:
                dup_streak = 0
            if dup_streak >= 2:
                break
            await asyncio.sleep(0.15)

        if len(store) > STORE_MAX:
            trimmed = sorted(store.values(), key=lambda x: x.get("t") or 0)[-STORE_MAX:]
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
    ) -> Dict[str, Any]:
        t0 = time.perf_counter()
        detail = await self.fetch_detail(portfolio_id, proxy_url=proxy_url)
        added = await self.sync_orders_into_store(
            portfolio_id, orders_store, full_sweep=full_sweep, proxy_url=proxy_url
        )

        # Fetch closed positions for reconciliation (fallback to closed_cache on failure)
        closed_rows = list(closed_cache)
        closed_total = None
        try:
            ph1 = await self.fetch_position_history(
                portfolio_id, page_number=1, page_size=100, proxy_url=proxy_url
            )
            lst = (ph1 or {}).get("list") or []
            closed_total = (ph1 or {}).get("total")
            if lst:
                seen_c: Dict[Tuple[str, Any], Dict[str, Any]] = {}
                for p in lst:
                    seen_c[(str(p.get("positionId")), p.get("opened"))] = p
                closed_rows = [slim_closed_position(p) for p in seen_c.values()]
                closed_rows.sort(key=lambda x: x.get("closed") or 0, reverse=True)
                closed_rows = closed_rows[:80]
        except Exception:
            pass

        # Performance & direct holdings
        perf: Dict[str, Any] = {}
        for tr in ("7D", "30D", "90D"):
            try:
                perf[tr] = await self.fetch_performance(portfolio_id, time_range=tr, proxy_url=proxy_url)
            except Exception:
                pass
        holdings = await self.fetch_holdings(portfolio_id, proxy_url=proxy_url)

        margin_balance = fnum(detail.get("marginBalance"))
        open_positions = reconstruct_open_positions(orders_store, closed_rows=closed_rows)
        marks = await self.fetch_marks([p["symbol"] for p in open_positions], proxy_url=proxy_url)
        brackets = await self.fetch_brackets(proxy_url=proxy_url)

        now_ms = int(time.time() * 1000)
        for p in open_positions:
            m = marks.get(p["symbol"]) or {}
            mark = m.get("markPrice")
            last_px = m.get("lastPrice") or mark
            cur_px = m.get("currentPrice") or last_px or mark
            p["lastPrice"] = last_px
            p["markPrice"] = mark
            p["currentPrice"] = cur_px
            p["fundingRate"] = m.get("fundingRate")
            p["priceTime"] = m.get("priceTime")

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
            if mark is not None and entry:
                upnl = (entry - mark) * qty if p["positionSide"] == "SHORT" else (mark - entry) * qty
                p["uPnL"] = round(upnl, 2)
                p["uPnLPct"] = round(upnl / p["entryValue"] * 100, 4) if p.get("entryValue") else None
                p["uPnLRoePct"] = round(upnl / margin_balance * 100, 4) if margin_balance else None
            else:
                p["uPnL"] = p["uPnLPct"] = p["uPnLRoePct"] = None

            t = self.mmr_tier(brackets, p["symbol"], p.get("markValue"))
            p["mmr"] = t
            p["maintMargin"] = (t or {}).get("maintMargin")
            p["heldMs"] = (now_ms - p["openTime"]) if p.get("openTime") else None

        tot_mark = sum(p["markValue"] for p in open_positions if p.get("markValue"))
        tot_entry = sum(p["entryValue"] for p in open_positions if p.get("entryValue"))
        tot_upnl = sum(p["uPnL"] for p in open_positions if p.get("uPnL") is not None)
        tot_mm = sum((p.get("mmr") or {}).get("maintMargin") or 0 for p in open_positions)
        tot_lev = round(tot_mark / margin_balance, 4) if (margin_balance and tot_mark) else 0.0

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
                    return {"ok": False, "reason": str(exc)}

            analyses = await asyncio.gather(*(_analyze(p) for p in open_positions))
            for pos, res in zip(open_positions, analyses):
                pos["analysis"] = res

        recent_raw = sorted(orders_store.values(), key=lambda x: x.get("t") or 0, reverse=True)[:200]
        batches = group_orders_into_batches(recent_raw)[:60]
        trade_cycles = reconstruct_trade_cycles(list(orders_store.values()))[:40]
        orders_view = [
            {
                "time": o.get("t"),
                "orderTime": o.get("ot"),
                "symbol": o.get("sym"),
                "side": o.get("side"),
                "positionSide": o.get("ps"),
                "type": o.get("ty"),
                "qty": o.get("q"),
                "avgPrice": o.get("p"),
                "pnl": o.get("pnl"),
                "isClose": not is_open_leg(o.get("side"), o.get("ps")),
                "dir": direction_label(o.get("side"), o.get("ps"), not is_open_leg(o.get("side"), o.get("ps"))),
            }
            for o in recent_raw
        ]

        return {
            "meta": {
                "generatedAt": now_ms,
                "fetchMs": int((time.perf_counter() - t0) * 1000),
                "portfolioId": portfolio_id,
                "nickname": detail.get("nickname") or portfolio_id,
                "orderStoreSize": len(orders_store),
                "ordersAddedThisRound": added,
                "fullSweep": full_sweep,
            },
            "profile": {
                "nickname": detail.get("nickname"),
                "avatarUrl": detail.get("avatarUrl"),
                "badgeName": detail.get("badgeName"),
                "status": detail.get("status"),
                "isPaused": detail.get("isPaused"),
                "positionShow": detail.get("positionShow"),
                "description": detail.get("description"),
                "lastTradeTime": detail.get("lastTradeTime"),
                "aumAmount": fnum(detail.get("aumAmount")),
                "copyAum": fnum(detail.get("copyAum")),
                "marginBalance": margin_balance,
                "copierPnl": fnum(detail.get("copierPnl")),
                "currentCopyCount": detail.get("currentCopyCount"),
                "maxCopyCount": detail.get("maxCopyCount"),
                "profitSharingRate": detail.get("profitSharingRate"),
            },
            "performance": perf,
            "holdings": holdings,
            "openPositions": open_positions,
            "openPositionsSummary": {
                "positionCount": len(open_positions),
                "totalEntryNotional": round(tot_entry, 2),
                "totalMarkNotional": round(tot_mark, 2),
                "totalNotionalLeverage": tot_lev,
                "estMaintenanceMargin": round(tot_mm, 2),
                "totalUnrealizedPnl": round(tot_upnl, 2),
                "totalMarginRoePct": (
                    round(tot_upnl / margin_balance * 100, 4) if (margin_balance and open_positions) else 0.0
                ),
                "marginBalance": margin_balance,
            },
            "orderBatches": batches,
            "tradeCycles": trade_cycles,
            "orders": orders_view,
            "closed": closed_rows,
            "closedTotal": closed_total,
        }
