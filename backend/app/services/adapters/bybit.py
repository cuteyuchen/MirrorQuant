from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

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


class BybitAdapter(BaseExchangeAdapter):
    """Async Bybit V5 Unified Trading REST API adapter (Live & Testnet/Demo)."""

    exchange = "bybit"
    LIVE_URL = "https://api.bybit.com"
    TESTNET_URL = "https://api-testnet.bybit.com"

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
        demo_url = (
            credentials.get("demo_url", "").strip()
            or os.environ.get("MQ_BYBIT_DEMO_URL", "").strip()
            or self.TESTNET_URL
        )
        self.base_url = demo_url.rstrip("/") if environment == "demo" else self.LIVE_URL
        self._instruments_cache: Dict[str, InstrumentSpec] = {}
        self._instruments_cache_ts: float = 0.0
        self._cache_ttl_sec: float = 600.0

    def _sign(self, timestamp_ms: str, recv_window: str, payload_str: str) -> str:
        msg = f"{timestamp_ms}{self.api_key}{recv_window}{payload_str}"
        return hmac.new(
            self.secret_key.encode("utf-8"),
            msg.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _position_idx(self, pos_side: str) -> int:
        if self.position_mode != "hedge":
            return 0
        return 1 if pos_side.lower() == "long" else 2

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        auth: bool = True,
        retries: int = 3,
    ) -> Dict[str, Any]:
        method = method.upper()
        clean_params = [(k, v) for k, v in sorted((params or {}).items()) if v is not None]
        query_str = urlencode(clean_params)
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
        url = f"{self.base_url}{path}" + (f"?{query_str}" if query_str else "")
        recv_window = "5000"

        last_err: Optional[Exception] = None
        async with ProxyManager.build_client(proxy_url=self.proxy_url, timeout=self.timeout) as client:
            for attempt in range(retries):
                headers: Dict[str, str] = {
                    "Content-Type": "application/json",
                    "User-Agent": "MirrorQuant/1.0",
                }
                if auth:
                    ts = str(int(time.time() * 1000) + self._time_offset_ms)
                    sign_payload = query_str if method == "GET" else body_str
                    headers["X-BAPI-API-KEY"] = self.api_key
                    headers["X-BAPI-TIMESTAMP"] = ts
                    headers["X-BAPI-RECV-WINDOW"] = recv_window
                    headers["X-BAPI-SIGN-TYPE"] = "2"
                    headers["X-BAPI-SIGN"] = self._sign(ts, recv_window, sign_payload)

                try:
                    if method == "GET":
                        resp = await client.get(url, headers=headers)
                    elif method == "POST":
                        resp = await client.post(url, headers=headers, content=body_str)
                    else:
                        raise ValueError(f"Unsupported HTTP method: {method}")

                    if resp.status_code == 429 and attempt < retries - 1:
                        await asyncio.sleep(0.8 * (attempt + 1))
                        continue
                    if resp.status_code >= 400:
                        raise ExchangeAPIError("bybit", str(resp.status_code), f"HTTP {resp.status_code}: {resp.text[:200]}")

                    payload = resp.json()
                    ret_code = int(payload.get("retCode", -1))
                    if ret_code != 0:
                        if ret_code in (10006, 10018) and attempt < retries - 1:
                            await asyncio.sleep(0.8 * (attempt + 1))
                            continue
                        raise ExchangeAPIError("bybit", str(ret_code), payload.get("retMsg", ""), payload)
                    return payload.get("result") or {}
                except ExchangeAPIError as exc:
                    last_err = exc
                    if exc.code not in ("10006", "10018"):
                        raise
                except Exception as exc:
                    last_err = exc
                    if attempt < retries - 1:
                        await asyncio.sleep(0.5 * (attempt + 1))
                    else:
                        raise
        raise RuntimeError(f"Bybit request failed ({path}): {last_err}")

    def resolve_symbol(
        self,
        lead_symbol: str,
        instruments: Dict[str, InstrumentSpec],
        symbol_map: Optional[Dict[str, str]] = None,
    ) -> tuple[str, bool]:
        if symbol_map and lead_symbol in symbol_map:
            inst_id = symbol_map[lead_symbol]
        else:
            inst_id = lead_symbol
        return inst_id, inst_id in instruments

    async def sync_time(self) -> int:
        res = await self._request("GET", "/v5/market/time", auth=False)
        if res.get("timeNano"):
            srv_ms = int(int(res["timeNano"]) // 1_000_000)
            self._time_offset_ms = srv_ms - int(time.time() * 1000)
        elif res.get("timeSecond"):
            srv_ms = int(res["timeSecond"]) * 1000
            self._time_offset_ms = srv_ms - int(time.time() * 1000)
        return self._time_offset_ms

    async def test_connection(self) -> Dict[str, Any]:
        bal = await self.get_usdt_balance()
        positions = await self.get_positions()
        return {
            "ok": True,
            "exchange": "bybit",
            "environment": self.environment,
            "posMode": self.position_mode,
            "usdtEquity": bal.eq,
            "availEquity": bal.avail_eq,
            "openPositions": len(positions),
        }

    async def get_usdt_balance(self) -> BalanceInfo:
        res = await self._request(
            "GET",
            "/v5/account/wallet-balance",
            params={"accountType": "UNIFIED", "coin": "USDT"},
        )
        acct_list = res.get("list") or []
        if not acct_list:
            return BalanceInfo(eq=0.0, avail_eq=0.0, cash_bal=0.0, upl=0.0)
        acct = acct_list[0]
        for c in acct.get("coin") or []:
            if c.get("coin") == "USDT":
                eq = float(c.get("equity") or 0.0)
                wallet = float(c.get("walletBalance") or 0.0)
                upl = float(c.get("unrealisedPnl") or 0.0)
                avail = float(c.get("availableToWithdraw") or acct.get("totalAvailableBalance") or eq)
                return BalanceInfo(
                    eq=eq,
                    avail_eq=avail,
                    cash_bal=wallet,
                    upl=upl,
                    raw=c,
                )
        return BalanceInfo(
            eq=float(acct.get("totalEquity") or 0.0),
            avail_eq=float(acct.get("totalAvailableBalance") or 0.0),
            cash_bal=float(acct.get("totalWalletBalance") or 0.0),
            upl=float(acct.get("totalPerpUPL") or 0.0),
            raw=acct,
        )

    async def get_positions(self) -> List[ExchangePosition]:
        res = await self._request(
            "GET",
            "/v5/position/list",
            params={"category": "linear", "settleCoin": "USDT"},
        )
        positions: List[ExchangePosition] = []
        for p in res.get("list") or []:
            sz = float(p.get("size") or 0.0)
            if sz <= 0:
                continue
            sym = str(p.get("symbol") or "")
            raw_side = str(p.get("side") or "").lower()
            pos_idx = int(p.get("positionIdx") or 0)
            if pos_idx == 1:
                pos_side = "long"
            elif pos_idx == 2:
                pos_side = "short"
            else:
                pos_side = "long" if raw_side == "buy" else "short"
            avg_px = float(p.get("avgPrice") or 0.0)
            notional = abs(float(p.get("positionValue") or (sz * avg_px)))
            upl = float(p.get("unrealisedPnl") or 0.0)
            positions.append(
                ExchangePosition(
                    inst_id=sym,
                    symbol=sym,
                    pos_side=pos_side,
                    mgn_mode="isolated" if int(p.get("tradeMode") or 0) == 1 else "cross",
                    pos=abs(sz),
                    avail_pos=abs(sz),
                    avg_px=avg_px,
                    upl=upl,
                    upl_ratio=(upl / notional) if notional > 0 else 0.0,
                    notional_usd=notional,
                    lever=float(p.get("leverage") or 1.0),
                    c_time=int(p.get("createdTime") or 0),
                    u_time=int(p.get("updatedTime") or 0),
                )
            )
        return positions

    async def get_instruments(self, force_refresh: bool = False) -> Dict[str, InstrumentSpec]:
        now = time.time()
        if not force_refresh and self._instruments_cache and (now - self._instruments_cache_ts < self._cache_ttl_sec):
            return self._instruments_cache

        res = await self._request(
            "GET",
            "/v5/market/instruments-info",
            params={"category": "linear", "limit": 1000},
            auth=False,
        )
        result: Dict[str, InstrumentSpec] = {}
        for item in res.get("list") or []:
            if item.get("status") != "Trading" or item.get("quoteCoin") != "USDT":
                continue
            sym = item.get("symbol")
            if not sym:
                continue
            lot = item.get("lotSizeFilter") or {}
            px = item.get("priceFilter") or {}
            lvr = item.get("leverageFilter") or {}
            try:
                result[sym] = InstrumentSpec(
                    inst_id=sym,
                    ct_val=Decimal("1"),
                    min_sz=Decimal(str(lot.get("minOrderQty") or "0.001")).normalize(),
                    lot_sz=Decimal(str(lot.get("qtyStep") or "0.001")).normalize(),
                    tick_sz=Decimal(str(px.get("tickSize") or "0.01")).normalize(),
                    max_lvr=float(lvr.get("maxLeverage") or 20.0),
                    min_notional=Decimal(str(lot.get("minNotionalValue") or "5")),
                    settle_ccy=str(item.get("settleCoin") or "USDT"),
                )
            except Exception:
                continue
        if result:
            self._instruments_cache = result
            self._instruments_cache_ts = now
        return self._instruments_cache

    async def get_orderbook(self, inst_id: str, depth: int = 5) -> OrderBook:
        res = await self._request(
            "GET",
            "/v5/market/orderbook",
            params={"category": "linear", "symbol": inst_id, "limit": depth},
            auth=False,
        )
        return OrderBook(
            bids=[[float(x[0]), float(x[1])] for x in res.get("b", [])],
            asks=[[float(x[0]), float(x[1])] for x in res.get("a", [])],
        )

    async def ensure_leverage(
        self,
        inst_id: str,
        lever: int,
        pos_side: str = "long",
        mgn_mode: str = "cross",
    ) -> bool:
        try:
            await self._request(
                "POST",
                "/v5/position/set-leverage",
                body={
                    "category": "linear",
                    "symbol": inst_id,
                    "buyLeverage": str(lever),
                    "sellLeverage": str(lever),
                },
            )
            return True
        except ExchangeAPIError as exc:
            if exc.code == "110043" or "not modified" in exc.msg.lower():
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
        ot = ord_type.lower()
        body: Dict[str, Any] = {
            "category": "linear",
            "symbol": inst_id,
            "side": "Buy" if side.lower() == "buy" else "Sell",
            "orderType": "Market" if ot == "market" else "Limit",
            "qty": str(sz),
            "positionIdx": self._position_idx(pos_side),
        }
        if ot == "ioc":
            body["timeInForce"] = "IOC"
            if px is not None:
                body["price"] = str(px)
        elif ot == "limit":
            body["timeInForce"] = "GTC"
            if px is not None:
                body["price"] = str(px)
        if reduce_only:
            body["reduceOnly"] = True
        if cl_ord_id:
            body["orderLinkId"] = cl_ord_id[:36]

        res = await self._request("POST", "/v5/order/create", body=body)
        return OrderResult(
            ord_id=str(res.get("orderId") or ""),
            cl_ord_id=str(res.get("orderLinkId") or cl_ord_id or ""),
            raw=res,
        )

    async def get_order(
        self,
        inst_id: str,
        ord_id: Optional[str] = None,
        cl_ord_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        params: Dict[str, Any] = {"category": "linear", "symbol": inst_id}
        if ord_id:
            params["orderId"] = ord_id
        if cl_ord_id:
            params["orderLinkId"] = cl_ord_id
        try:
            res = await self._request("GET", "/v5/order/realtime", params=params)
        except ExchangeAPIError:
            return None
        rows = res.get("list") or []
        if not rows:
            return None
        row = rows[0]
        return {
            "ordId": row.get("orderId"),
            "clOrdId": row.get("orderLinkId"),
            "accFillSz": float(row.get("cumExecQty") or 0.0),
            "avgPx": float(row.get("avgPrice") or 0.0),
            "state": row.get("orderStatus"),
            "raw": row,
        }

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
        body: Dict[str, Any] = {
            "category": "linear",
            "symbol": inst_id,
            "positionIdx": self._position_idx(pos_side),
            "tpslMode": "Full",
        }
        if tp_px:
            body["takeProfit"] = str(tp_px)
            body["tpTriggerBy"] = "MarkPrice"
        if sl_px:
            body["stopLoss"] = str(sl_px)
            body["slTriggerBy"] = "MarkPrice"
        res = await self._request("POST", "/v5/position/trading-stop", body=body)
        return {"ok": True, "raw": res, "tpOrderId": "trading-stop" if tp_px else None, "slOrderId": "trading-stop" if sl_px else None}

    async def cancel_tp_sl(self, inst_id: str, pos_side: Optional[str] = None) -> bool:
        try:
            await self._request(
                "POST",
                "/v5/position/trading-stop",
                body={
                    "category": "linear",
                    "symbol": inst_id,
                    "positionIdx": self._position_idx(pos_side or "long"),
                    "takeProfit": "0",
                    "stopLoss": "0",
                },
            )
            return True
        except Exception:
            return False
