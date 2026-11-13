from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from app.services.adapters.base import (
    BalanceInfo,
    BaseExchangeAdapter,
    ExchangeAPIError,
    ExchangePosition,
    InstrumentSpec,
    OrderBook,
    OrderResult,
)
from app.services.proxy.manager import ProxyManager


class OKXAdapter(BaseExchangeAdapter):
    """Async OKX V5 REST API adapter supporting Live and Demo (x-simulated-trading: 1)."""

    exchange = "okx"
    BASE_URL = "https://www.okx.com"

    def __init__(
        self,
        credentials: Dict[str, str],
        environment: str = "demo",
        proxy_url: Optional[str] = None,
        margin_mode: str = "cross",
        position_mode: str = "hedge",
        timeout: float = 12.0,
    ):
        super().__init__(credentials, environment, proxy_url, margin_mode, position_mode, timeout)
        self.api_key = credentials.get("api_key", "").strip()
        self.secret_key = credentials.get("secret_key", "").strip()
        self.passphrase = credentials.get("passphrase", "").strip()
        self.is_simulated = environment == "demo"
        self._instruments_cache: Dict[str, InstrumentSpec] = {}
        self._instruments_cache_ts: float = 0.0
        self._cache_ttl_sec: float = 600.0

    def _sign(self, timestamp: str, method: str, request_path: str, body_str: str = "") -> str:
        msg = f"{timestamp}{method.upper()}{request_path}{body_str}"
        mac = hmac.new(
            self.secret_key.encode("utf-8"),
            msg.encode("utf-8"),
            hashlib.sha256,
        )
        return base64.b64encode(mac.digest()).decode("utf-8")

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Any] = None,
        auth: bool = True,
        retries: int = 3,
    ) -> Any:
        self._check_rate_limit()
        method = method.upper()
        # A mutation may already have reached the exchange when its response is lost.
        attempts = max(1, retries) if method == "GET" else 1
        query_str = ""
        if params:
            parts = [f"{k}={v}" for k, v in sorted(params.items()) if v is not None]
            if parts:
                query_str = "?" + "&".join(parts)

        request_path = path + query_str
        url = self.BASE_URL + request_path
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""

        last_err: Optional[Exception] = None
        async with ProxyManager.build_client(
            proxy_url=self.proxy_url, timeout=self.timeout, exchange=self.exchange,
        ) as client:
            for attempt in range(attempts):
                headers: Dict[str, str] = {
                    "Content-Type": "application/json",
                    "User-Agent": "MirrorQuant/1.0",
                }
                if self.is_simulated:
                    headers["x-simulated-trading"] = "1"
                if auth:
                    now_ms = int(time.time() * 1000) + self._time_offset_ms
                    ts_dt = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc)
                    ts = ts_dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts_dt.microsecond // 1000:03d}Z"
                    headers["OK-ACCESS-KEY"] = self.api_key
                    headers["OK-ACCESS-SIGN"] = self._sign(ts, method, request_path, body_str)
                    headers["OK-ACCESS-TIMESTAMP"] = ts
                    headers["OK-ACCESS-PASSPHRASE"] = self.passphrase

                try:
                    if method == "GET":
                        resp = await client.get(url, headers=headers)
                    elif method == "POST":
                        resp = await client.post(url, headers=headers, content=body_str)
                    else:
                        raise ValueError(f"Unsupported HTTP method: {method}")

                    if resp.status_code == 429:
                        self._note_rate_limit(resp)
                    if resp.status_code != 200:
                        raise ExchangeAPIError("okx", str(resp.status_code), f"HTTP {resp.status_code}: {resp.text[:200]}")

                    payload = resp.json()
                    code = str(payload.get("code", ""))
                    msg = payload.get("msg", "")
                    if code != "0":
                        if code in ("50011", "50061"):
                            self._note_rate_limit(resp)
                        raise ExchangeAPIError("okx", code, msg, payload.get("data"))
                    return payload.get("data", [])
                except ExchangeAPIError:
                    raise
                except Exception as exc:
                    last_err = exc
                    if attempt < attempts - 1:
                        await asyncio.sleep(0.5 * (attempt + 1))
                    else:
                        raise
        raise RuntimeError(f"OKX request failed ({request_path}): {last_err}")

    def resolve_symbol(
        self,
        lead_symbol: str,
        instruments: Dict[str, InstrumentSpec],
        symbol_map: Optional[Dict[str, str]] = None,
    ) -> tuple[str, bool]:
        if symbol_map and lead_symbol in symbol_map:
            inst_id = symbol_map[lead_symbol]
        else:
            base = lead_symbol[:-4] if lead_symbol.endswith("USDT") else lead_symbol
            inst_id = f"{base}-USDT-SWAP"
        return inst_id, self._symbol_matches(lead_symbol, inst_id, instruments)

    async def test_connection(self) -> Dict[str, Any]:
        cfg_data = await self._request("GET", "/api/v5/account/config")
        cfg = cfg_data[0] if cfg_data else {}
        bal = await self.get_usdt_balance()
        positions = await self.get_positions()
        return {
            "ok": True,
            "exchange": "okx",
            "environment": self.environment,
            "posMode": cfg.get("posMode", "unknown"),
            "acctLv": cfg.get("acctLv", "unknown"),
            "usdtEquity": bal.eq,
            "availEquity": bal.avail_eq,
            "openPositions": len(positions),
        }

    async def get_usdt_balance(self) -> BalanceInfo:
        """Extract strictly USDT equity from details[] to avoid demo non-USDT spot inflation."""
        data = await self._request("GET", "/api/v5/account/balance", params={"ccy": "USDT"})
        if not data:
            return BalanceInfo(eq=0.0, avail_eq=0.0, cash_bal=0.0, upl=0.0)
        bal = data[0]
        for det in bal.get("details", []):
            if det.get("ccy") == "USDT":
                return BalanceInfo(
                    eq=float(det.get("eq") or 0.0),
                    avail_eq=float(det.get("availEq") or 0.0),
                    cash_bal=float(det.get("cashBal") or 0.0),
                    upl=float(det.get("upl") or 0.0),
                    raw=det,
                )
        # Other collateral does not become USDT capital when the USDT row is absent.
        return BalanceInfo(eq=0.0, avail_eq=0.0, cash_bal=0.0, upl=0.0, raw=bal)

    async def get_positions(self) -> List[ExchangePosition]:
        data = await self._request("GET", "/api/v5/account/positions", params={"instType": "SWAP"})
        positions: List[ExchangePosition] = []
        for p in data:
            pos_sz = float(p.get("pos") or 0.0)
            if abs(pos_sz) <= 0:
                continue
            inst_id = str(p.get("instId") or "")
            base_sym = inst_id.replace("-USDT-SWAP", "USDT") if inst_id.endswith("-USDT-SWAP") else inst_id
            raw_side = str(p.get("posSide") or "net").lower()
            logical_side = ("long" if pos_sz > 0 else "short") if raw_side == "net" else raw_side
            positions.append(
                ExchangePosition(
                    inst_id=inst_id,
                    symbol=base_sym,
                    pos_side=logical_side,
                    mgn_mode=str(p.get("mgnMode") or "cross").lower(),
                    pos=abs(pos_sz),
                    avail_pos=abs(float(p.get("availPos") or 0.0)),
                    avg_px=float(p.get("avgPx") or 0.0),
                    upl=float(p.get("upl") or 0.0),
                    upl_ratio=float(p.get("uplRatio") or 0.0),
                    notional_usd=abs(float(p.get("notionalUsd") or 0.0)),
                    lever=float(p.get("lever") or 1.0),
                    c_time=int(p.get("cTime") or 0),
                    u_time=int(p.get("uTime") or 0),
                )
            )
        return positions

    async def get_instruments(self, force_refresh: bool = False) -> Dict[str, InstrumentSpec]:
        now = time.time()
        if not force_refresh and self._instruments_cache and now - self._instruments_cache_ts < self._cache_ttl_sec:
            return self._instruments_cache
        data = await self._request("GET", "/api/v5/public/instruments", params={"instType": "SWAP"}, auth=False)
        result: Dict[str, InstrumentSpec] = {}
        for item in data:
            inst_id = str(item.get("instId") or "")
            if (item.get("state") != "live" or item.get("ctType") != "linear"
                    or item.get("settleCcy") != "USDT" or not inst_id.endswith("-USDT-SWAP")):
                continue
            base = inst_id.removesuffix("-USDT-SWAP")
            if item.get("ctValCcy") != base or item.get("ctMult") not in (None, "", "1", 1):
                continue
            try:
                result[inst_id] = InstrumentSpec(
                    inst_id=inst_id, ct_val=Decimal(str(item["ctVal"])),
                    min_sz=Decimal(str(item["minSz"])), lot_sz=Decimal(str(item["lotSz"])),
                    tick_sz=Decimal(str(item["tickSz"])), max_lvr=float(item["lever"]),
                    max_sz=Decimal(str(item["maxLmtSz"])), market_max_sz=Decimal(str(item["maxMktSz"])),
                    settle_ccy="USDT", base_ccy=base,
                )
            except (ValueError, KeyError, ArithmeticError, TypeError):
                # Incomplete metadata suspends this instrument; never invent its units.
                continue
        self._instruments_cache, self._instruments_cache_ts = result, now
        return result

    async def get_orderbook(self, inst_id: str, depth: int = 5) -> OrderBook:
        data = await self._request(
            "GET", "/api/v5/market/books", params={"instId": inst_id, "sz": depth}, auth=False
        )
        if not data:
            return OrderBook(bids=[], asks=[])
        book = data[0]
        return OrderBook(
            bids=[[float(x[0]), float(x[1])] for x in book.get("bids", [])],
            asks=[[float(x[0]), float(x[1])] for x in book.get("asks", [])],
        )

    async def ensure_leverage(self, inst_id: str, lever: int, pos_side: str = "long", mgn_mode: str = "cross") -> bool:
        self._assert_supported_entry_mode(mgn_mode)
        config = await self._request("GET", "/api/v5/account/config")
        if not config or config[0].get("posMode") != "long_short_mode" or str(config[0].get("acctLv")) != "2":
            raise ExchangeAPIError(self.exchange, "UNSUPPORTED_MODE", "OKX requires single-currency futures and hedge mode")
        body = {"instId": inst_id, "lever": str(lever), "mgnMode": mgn_mode}
        # Cross-margin SWAP leverage is instrument-wide; posSide is for isolated hedge mode.
        try:
            await self._request("POST", "/api/v5/account/set-leverage", body=body)
        except ExchangeAPIError:
            return False
        rows = await self._request("GET", "/api/v5/account/leverage-info", params={"instId": inst_id, "mgnMode": mgn_mode})
        return bool(rows) and all(float(row.get("lever") or 0) == lever for row in rows)

    async def get_max_leverage(self, inst_id: str, target_notional: Any) -> int:
        spec = (await self.get_instruments()).get(inst_id)
        if spec is None:
            raise ValueError("Unqualified instrument")
        rows = await self._request("GET", "/api/v5/public/position-tiers", params={
            "instType": "SWAP", "tdMode": "cross", "instFamily": inst_id.removesuffix("-SWAP")
        }, auth=False)
        book = await self.get_orderbook(inst_id, depth=1)
        if not rows or not book.bids or not book.asks:
            raise ValueError("Missing OKX tier or mark data")
        # Tier bounds are contracts. Use the lower quote conservatively.
        price = min(Decimal(str(book.bids[0][0])), Decimal(str(book.asks[0][0])))
        qty = Decimal(str(target_notional)) / (price * spec.ct_val)
        for tier in sorted(rows, key=lambda row: Decimal(str(row["maxSz"]))):
            if Decimal(str(tier["minSz"])) <= qty <= Decimal(str(tier["maxSz"])):
                return min(int(spec.max_lvr), int(float(tier["maxLever"])))
        raise ValueError("Target exposure exceeds qualified OKX tiers")

    async def place_order(
        self,
        inst_id: str,
        side: str,
        pos_side: str,
        ord_type: str,
        sz: str,
        px: Optional[str] = None,
        cl_ord_id: Optional[str] = None,
        reduce_only: bool = False,
        td_mode: str = "cross",
    ) -> OrderResult:
        await self.validate_order(inst_id, ord_type, sz, px, reduce_only)
        body: Dict[str, Any] = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side.lower(),
            "ordType": ord_type.lower(),
            "sz": str(sz),
        }
        if self.position_mode == "hedge":
            body["posSide"] = pos_side.lower()
        else:
            body["posSide"] = "net"
        if px is not None:
            body["px"] = str(px)
        if cl_ord_id:
            body["clOrdId"] = cl_ord_id[:32]
        if reduce_only:
            body["reduceOnly"] = True

        data = await self._request("POST", "/api/v5/trade/order", body=body)
        if not data:
            raise ExchangeAPIError("okx", "UNKNOWN", "Empty response for place_order")
        item = data[0]
        s_code = str(item.get("sCode", ""))
        if s_code != "0":
            raise ExchangeAPIError("okx", s_code, item.get("sMsg", ""), item)
        return OrderResult(
            ord_id=str(item.get("ordId") or ""),
            cl_ord_id=str(item.get("clOrdId") or cl_ord_id or ""),
            raw=item,
        )

    async def get_order(
        self,
        inst_id: str,
        ord_id: Optional[str] = None,
        cl_ord_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        params: Dict[str, Any] = {"instId": inst_id}
        if ord_id:
            params["ordId"] = ord_id
        if cl_ord_id:
            params["clOrdId"] = cl_ord_id
        try:
            data = await self._request("GET", "/api/v5/trade/order", params=params)
        except ExchangeAPIError as exc:
            if str(exc.code) in ("51603", "51001"):
                return None
            raise
        if not data:
            return None
        row = data[0]
        return {
            "ordId": row.get("ordId"),
            "clOrdId": row.get("clOrdId"),
            "accFillSz": float(row.get("accFillSz") or 0.0),
            "avgPx": float(row.get("avgPx") or row.get("fillPx") or 0.0),
            "state": row.get("state"),
            "raw": row,
        }

    async def get_open_orders(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        queries = [("/api/v5/trade/orders-pending", {}, "ordId")]
        queries.extend(("/api/v5/trade/orders-algo-pending", {"ordType": kind}, "algoId")
                       for kind in ("conditional", "oco", "trigger", "move_order", "iceberg", "twap"))
        for path, extra, key in queries:
            after, seen = None, set()
            while True:
                params = {"instType": "SWAP", "limit": 100, **extra}
                if after:
                    params["after"] = after
                page = await self._request("GET", path, params=params)
                if not isinstance(page, list):
                    raise ValueError("Incomplete OKX pending-order inventory")
                rows.extend(page)
                if len(page) < 100:
                    break
                after = page[-1].get(key)
                if not after or after in seen:
                    raise ValueError("Repeated OKX pending-order cursor")
                seen.add(after)
        return rows

    async def sync_time(self) -> int:
        sent_ms = time.time() * 1000
        data = await self._request("GET", "/api/v5/public/time", auth=False, retries=1)
        if not data or not data[0].get("ts"):
            raise RuntimeError("Missing OKX server time")
        return self._calibrate_time(int(data[0]["ts"]), sent_ms)

    async def place_tp_sl(
        self,
        inst_id: str,
        pos_side: str,
        sz: str,
        tp_px: Optional[str] = None,
        sl_px: Optional[str] = None,
        td_mode: str = "cross",
    ) -> Dict[str, Any]:
        if not tp_px and not sl_px:
            return {"ok": False, "details": "No TP/SL trigger price specified"}
        close_side = "sell" if pos_side.lower() == "long" else "buy"
        ord_type = "oco" if (tp_px and sl_px) else "conditional"
        body: Dict[str, Any] = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": close_side,
            "ordType": ord_type,
            "sz": str(sz),
            "reduceOnly": True,
        }
        body["posSide"] = pos_side.lower() if self.position_mode == "hedge" else "net"
        if tp_px:
            body["tpTriggerPx"] = str(tp_px)
            body["tpOrdPx"] = "-1"
            body["tpTriggerPxType"] = "mark"
        if sl_px:
            body["slTriggerPx"] = str(sl_px)
            body["slOrdPx"] = "-1"
            body["slTriggerPxType"] = "mark"

        data = await self._request("POST", "/api/v5/trade/order-algo", body=body)
        item = data[0] if data else {}
        s_code = str(item.get("sCode", ""))
        if s_code != "0":
            raise ExchangeAPIError("okx", s_code, item.get("sMsg", "TP/SL algo order rejected"), item)
        algo_id = str(item.get("algoId") or "")
        if not algo_id:
            raise ExchangeAPIError(self.exchange, "AMBIGUOUS", "Missing protective order identity")
        return {"ok": True, "algoId": algo_id, "tpOrderId": algo_id if tp_px else None, "slOrderId": algo_id if sl_px else None}

    async def cancel_tp_sl(self, inst_id: str, pos_side: Optional[str] = None) -> bool:
        cancelled = False
        for ot in ("conditional", "oco"):
            try:
                pending = await self._request(
                    "GET",
                    "/api/v5/trade/orders-algo-pending",
                    params={"instId": inst_id, "ordType": ot},
                )
                req_list = [
                    {"algoId": p["algoId"], "instId": inst_id}
                    for p in (pending or [])
                    if p.get("algoId") and (not pos_side or str(p.get("posSide") or "").lower() == pos_side.lower())
                ]
                if req_list:
                    await self._request("POST", "/api/v5/trade/cancel-algos", body=req_list[:10])
                    cancelled = True
            except Exception:
                continue
        return cancelled
