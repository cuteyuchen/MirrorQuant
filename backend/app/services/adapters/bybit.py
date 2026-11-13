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
        self._check_rate_limit()
        method = method.upper()
        # A mutation may already have reached the exchange when its response is lost.
        attempts = max(1, retries) if method == "GET" else 1
        clean_params = [(k, v) for k, v in sorted((params or {}).items()) if v is not None]
        query_str = urlencode(clean_params)
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
        url = f"{self.base_url}{path}" + (f"?{query_str}" if query_str else "")
        recv_window = "5000"

        last_err: Optional[Exception] = None
        async with ProxyManager.build_client(proxy_url=self.proxy_url, timeout=self.timeout) as client:
            for attempt in range(attempts):
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

                    if resp.status_code == 429:
                        self._note_rate_limit(resp)
                    if resp.status_code >= 400:
                        raise ExchangeAPIError("bybit", str(resp.status_code), f"HTTP {resp.status_code}: {resp.text[:200]}")

                    payload = resp.json()
                    ret_code = int(payload.get("retCode", -1))
                    if ret_code != 0:
                        if ret_code in (10006, 10018):
                            self._note_rate_limit(resp)
                        raise ExchangeAPIError("bybit", str(ret_code), payload.get("retMsg", ""), payload)
                    return payload.get("result") or {}
                except ExchangeAPIError:
                    raise
                except Exception as exc:
                    last_err = exc
                    if attempt < attempts - 1:
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
        return inst_id, self._symbol_matches(lead_symbol, inst_id, instruments)

    async def sync_time(self) -> int:
        sent_ms = time.time() * 1000
        res = await self._request("GET", "/v5/market/time", auth=False, retries=1)
        if res.get("timeNano"):
            server_ms = int(res["timeNano"]) // 1_000_000
        elif res.get("timeSecond"):
            server_ms = int(res["timeSecond"]) * 1000
        else:
            raise RuntimeError("Missing Bybit server time")
        return self._calibrate_time(server_ms, sent_ms)

    async def _all_pages(self, path: str, params: Dict[str, Any], auth: bool = True) -> List[Dict[str, Any]]:
        rows, seen = [], set()
        cursor = None
        while True:
            request = dict(params)
            if cursor:
                request["cursor"] = cursor
            result = await self._request("GET", path, params=request, auth=auth)
            page = result.get("list")
            if not isinstance(page, list):
                raise ValueError("Incomplete Bybit paginated response")
            rows.extend(page)
            cursor = result.get("nextPageCursor") or None
            if cursor is None:
                return rows
            if cursor in seen:
                raise RuntimeError("Repeated exchange pagination cursor; snapshot incomplete")
            seen.add(cursor)

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
        res = await self._request("GET", "/v5/account/wallet-balance", params={"accountType": "UNIFIED"})
        accounts = res.get("list") or []
        if not accounts:
            return BalanceInfo(eq=0, avail_eq=0, cash_bal=0, upl=0)
        acct = accounts[0]
        coins = acct.get("coin") or []
        single_collateral = all(c.get("coin") == "USDT" or Decimal(str(c.get("equity") or "0")) == 0 for c in coins)
        for coin in coins:
            if coin.get("coin") == "USDT":
                equity = float(coin.get("equity") or 0)
                # totalAvailableBalance is qualified only for a USDT-only UTA.
                available = acct.get("totalAvailableBalance")
                avail = min(max(0.0, equity), max(0.0, float(available))) if single_collateral and available not in (None, "") else 0.0
                return BalanceInfo(eq=equity, avail_eq=avail,
                    cash_bal=float(coin.get("walletBalance") or 0), upl=float(coin.get("unrealisedPnl") or 0), raw=acct)
        return BalanceInfo(eq=0, avail_eq=0, cash_bal=0, upl=0, raw=acct)

    async def get_positions(self) -> List[ExchangePosition]:
        rows = await self._all_pages("/v5/position/list", {"category": "linear", "settleCoin": "USDT", "limit": 200})
        positions: List[ExchangePosition] = []
        for p in rows:
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
        if not force_refresh and self._instruments_cache and now - self._instruments_cache_ts < self._cache_ttl_sec:
            return self._instruments_cache
        rows = await self._all_pages("/v5/market/instruments-info", {"category": "linear", "limit": 1000}, auth=False)
        result: Dict[str, InstrumentSpec] = {}
        for item in rows:
            if (item.get("status") != "Trading" or item.get("quoteCoin") != "USDT"
                    or item.get("settleCoin") != "USDT" or item.get("contractType") != "LinearPerpetual"):
                continue
            try:
                sym = item["symbol"]
                lot, price, leverage = item["lotSizeFilter"], item["priceFilter"], item["leverageFilter"]
                result[sym] = InstrumentSpec(
                    inst_id=sym, ct_val=Decimal("1"), min_sz=Decimal(str(lot["minOrderQty"])),
                    lot_sz=Decimal(str(lot["qtyStep"])), max_sz=Decimal(str(lot["maxOrderQty"])),
                    market_max_sz=Decimal(str(lot["maxMktOrderQty"])), tick_sz=Decimal(str(price["tickSize"])),
                    max_lvr=float(leverage["maxLeverage"]), min_notional=Decimal(str(lot["minNotionalValue"])),
                    settle_ccy="USDT", base_ccy=item["baseCoin"],
                )
            except (ValueError, KeyError, ArithmeticError, TypeError):
                continue
        self._instruments_cache, self._instruments_cache_ts = result, now
        return result

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

    async def ensure_leverage(self, inst_id: str, lever: int, pos_side: str = "long", mgn_mode: str = "cross") -> bool:
        self._assert_supported_entry_mode(mgn_mode)
        account = await self._request("GET", "/v5/account/info")
        if account.get("marginMode") != "REGULAR_MARGIN":
            raise ExchangeAPIError(self.exchange, "UNSUPPORTED_MODE", "Bybit requires regular cross-margin UTA")
        params = {"category": "linear", "symbol": inst_id}
        rows = await self._all_pages("/v5/position/list", params)
        if not rows or any(int(row.get("positionIdx") or 0) not in (1, 2) for row in rows):
            raise ExchangeAPIError(self.exchange, "UNSUPPORTED_MODE", "Bybit symbol must already use hedge mode")
        try:
            await self._request("POST", "/v5/position/set-leverage", body={
                "category": "linear", "symbol": inst_id, "buyLeverage": str(lever), "sellLeverage": str(lever)
            })
        except ExchangeAPIError as exc:
            if str(exc.code) != "110043":
                return False
        rows = await self._all_pages("/v5/position/list", params)
        return bool(rows) and all(float(row.get("leverage") or 0) == lever for row in rows)

    async def get_max_leverage(self, inst_id: str, target_notional: Any) -> int:
        rows = await self._all_pages("/v5/market/risk-limit", {"category": "linear", "symbol": inst_id}, auth=False)
        for tier in sorted(rows, key=lambda row: Decimal(str(row["riskLimitValue"]))):
            if Decimal(str(target_notional)) <= Decimal(str(tier["riskLimitValue"])):
                return int(float(tier["maxLeverage"]))
        raise ValueError("Target exposure exceeds qualified Bybit tiers")

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
        except ExchangeAPIError as exc:
            if str(exc.code) in ("110001", "170213"):
                return None
            raise
        rows = res.get("list") or []
        if not rows:
            try:
                history = await self._request("GET", "/v5/order/history", params=params)
            except ExchangeAPIError as exc:
                if str(exc.code) in ("110001", "170213"):
                    return None
                raise
            rows = history.get("list") or []
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

    async def get_open_orders(self) -> List[Dict[str, Any]]:
        # Without orderFilter, unified linear returns active ordinary and conditional orders.
        return await self._all_pages("/v5/order/realtime", {
            "category": "linear", "settleCoin": "USDT", "openOnly": 0, "limit": 50,
        })

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
