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
        body: Optional[Dict[str, Any]] = None,
        auth: bool = True,
        retries: int = 3,
    ) -> Any:
        method = method.upper()
        query_str = ""
        if params:
            parts = [f"{k}={v}" for k, v in sorted(params.items()) if v is not None]
            if parts:
                query_str = "?" + "&".join(parts)

        request_path = path + query_str
        url = self.BASE_URL + request_path
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""

        last_err: Optional[Exception] = None
        async with ProxyManager.build_client(proxy_url=self.proxy_url, timeout=self.timeout) as client:
            for attempt in range(retries):
                headers: Dict[str, str] = {
                    "Content-Type": "application/json",
                    "User-Agent": "MirrorQuant/1.0",
                }
                if self.is_simulated:
                    headers["x-simulated-trading"] = "1"
                if auth:
                    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
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

                    if resp.status_code != 200:
                        raise ExchangeAPIError("okx", str(resp.status_code), f"HTTP {resp.status_code}: {resp.text[:200]}")

                    payload = resp.json()
                    code = str(payload.get("code", ""))
                    msg = payload.get("msg", "")
                    if code != "0":
                        if code == "50011" and attempt < retries - 1:
                            await asyncio.sleep(0.8 * (attempt + 1))
                            continue
                        raise ExchangeAPIError("okx", code, msg, payload.get("data"))
                    return payload.get("data", [])
                except ExchangeAPIError as exc:
                    last_err = exc
                    if exc.code != "50011":
                        raise
                except Exception as exc:
                    last_err = exc
                    if attempt < retries - 1:
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
        return inst_id, inst_id in instruments

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
        return BalanceInfo(
            eq=float(bal.get("totalEq") or 0.0),
            avail_eq=float(bal.get("availEq") or 0.0),
            cash_bal=float(bal.get("cashBal") or 0.0),
            upl=float(bal.get("upl") or 0.0),
            raw=bal,
        )

    async def get_positions(self) -> List[ExchangePosition]:
        data = await self._request("GET", "/api/v5/account/positions", params={"instType": "SWAP"})
        positions: List[ExchangePosition] = []
        for p in data:
            pos_sz = float(p.get("pos") or 0.0)
            if abs(pos_sz) <= 0:
                continue
            inst_id = str(p.get("instId") or "")
            base_sym = inst_id.replace("-USDT-SWAP", "USDT") if inst_id.endswith("-USDT-SWAP") else inst_id
            positions.append(
                ExchangePosition(
                    inst_id=inst_id,
                    symbol=base_sym,
                    pos_side=str(p.get("posSide") or "net").lower(),
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
        if not force_refresh and self._instruments_cache and (now - self._instruments_cache_ts < self._cache_ttl_sec):
            return self._instruments_cache

        data = await self._request("GET", "/api/v5/public/instruments", params={"instType": "SWAP"}, auth=False)
        result: Dict[str, InstrumentSpec] = {}
        for item in data:
            if item.get("state") != "live":
                continue
            inst_id = item.get("instId")
            if not inst_id:
                continue
            try:
                result[inst_id] = InstrumentSpec(
                    inst_id=inst_id,
                    ct_val=Decimal(str(item.get("ctVal") or "1")),
                    min_sz=Decimal(str(item.get("minSz") or "1")),
                    lot_sz=Decimal(str(item.get("lotSz") or "1")),
                    tick_sz=Decimal(str(item.get("tickSz") or "0.01")),
                    max_lvr=float(item.get("lever") or 20.0),
                    settle_ccy=str(item.get("settleCcy") or "USDT"),
                )
            except Exception:
                continue
        if result:
            self._instruments_cache = result
            self._instruments_cache_ts = now
        return self._instruments_cache

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

    async def ensure_leverage(
        self,
        inst_id: str,
        lever: int,
        pos_side: str = "long",
        mgn_mode: str = "cross",
    ) -> bool:
        body: Dict[str, Any] = {
            "instId": inst_id,
            "lever": str(lever),
            "mgnMode": mgn_mode,
        }
        if pos_side in ("long", "short") and self.position_mode == "hedge":
            body["posSide"] = pos_side
        try:
            await self._request("POST", "/api/v5/account/set-leverage", body=body)
            return True
        except ExchangeAPIError as exc:
            if "not changed" in exc.msg.lower() or exc.code == "59000":
                return True
            return False

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
        data = await self._request("GET", "/api/v5/trade/order", params=params)
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
