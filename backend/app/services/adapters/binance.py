from __future__ import annotations

import asyncio
import hashlib
import hmac
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


class BinanceAdapter(BaseExchangeAdapter):
    """Async Binance USDT-M Futures REST API adapter (Live & Testnet)."""

    exchange = "binance"
    LIVE_URL = "https://fapi.binance.com"
    TESTNET_URL = "https://testnet.binancefuture.com"

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
        self.base_url = self.TESTNET_URL if environment == "demo" else self.LIVE_URL
        self._instruments_cache: Dict[str, InstrumentSpec] = {}
        self._instruments_cache_ts: float = 0.0
        self._cache_ttl_sec: float = 600.0

    def _sign_query(self, params: Dict[str, Any]) -> str:
        clean = [(k, v) for k, v in params.items() if v is not None]
        query = urlencode(clean)
        sig = hmac.new(
            self.secret_key.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{query}&signature={sig}" if query else f"signature={sig}"

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        auth: bool = True,
        retries: int = 3,
    ) -> Any:
        method = method.upper()
        base_params = dict(params or {})

        async with ProxyManager.build_client(proxy_url=self.proxy_url, timeout=self.timeout) as client:
            last_err: Optional[Exception] = None
            for attempt in range(retries):
                req_params = dict(base_params)
                headers: Dict[str, str] = {"User-Agent": "MirrorQuant/1.0"}
                if auth:
                    req_params["timestamp"] = int(time.time() * 1000)
                    req_params["recvWindow"] = 5000
                    qs = self._sign_query(req_params)
                    headers["X-MBX-APIKEY"] = self.api_key
                else:
                    qs = urlencode([(k, v) for k, v in req_params.items() if v is not None])

                url = f"{self.base_url}{path}" + (f"?{qs}" if qs else "")
                try:
                    if method == "GET":
                        resp = await client.get(url, headers=headers)
                    elif method == "POST":
                        resp = await client.post(url, headers=headers)
                    elif method == "DELETE":
                        resp = await client.delete(url, headers=headers)
                    else:
                        raise ValueError(f"Unsupported HTTP method: {method}")

                    if resp.status_code == 429 and attempt < retries - 1:
                        await asyncio.sleep(1.0 * (attempt + 1))
                        continue

                    data = resp.json()
                    if resp.status_code >= 400 or (isinstance(data, dict) and "code" in data and data["code"] < 0):
                        code = str(data.get("code", resp.status_code)) if isinstance(data, dict) else str(resp.status_code)
                        msg = data.get("msg", resp.text[:200]) if isinstance(data, dict) else resp.text[:200]
                        raise ExchangeAPIError("binance", code, msg, data)
                    return data
                except ExchangeAPIError:
                    raise
                except Exception as exc:
                    last_err = exc
                    if attempt < retries - 1:
                        await asyncio.sleep(0.4 * (attempt + 1))
                    else:
                        raise
        raise RuntimeError(f"Binance Futures request failed ({path}): {last_err}")

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

    async def test_connection(self) -> Dict[str, Any]:
        acct = await self._request("GET", "/fapi/v2/account")
        dual = await self._request("GET", "/fapi/v1/positionSide/dual")
        bal = await self.get_usdt_balance()
        positions = await self.get_positions()
        return {
            "ok": True,
            "exchange": "binance",
            "environment": self.environment,
            "posMode": "hedge" if dual.get("dualSidePosition") else "one_way",
            "canTrade": acct.get("canTrade", True),
            "usdtEquity": bal.eq,
            "availEquity": bal.avail_eq,
            "openPositions": len(positions),
        }

    async def get_usdt_balance(self) -> BalanceInfo:
        data = await self._request("GET", "/fapi/v2/balance")
        for item in data or []:
            if item.get("asset") == "USDT":
                wallet = float(item.get("balance") or 0.0)
                upl = float(item.get("crossUnPnl") or 0.0)
                avail = float(item.get("availableBalance") or 0.0)
                return BalanceInfo(
                    eq=wallet + upl,
                    avail_eq=avail,
                    cash_bal=wallet,
                    upl=upl,
                    raw=item,
                )
        return BalanceInfo(eq=0.0, avail_eq=0.0, cash_bal=0.0, upl=0.0)

    async def get_positions(self) -> List[ExchangePosition]:
        data = await self._request("GET", "/fapi/v2/positionRisk")
        positions: List[ExchangePosition] = []
        for p in data or []:
            amt = float(p.get("positionAmt") or 0.0)
            if abs(amt) <= 0:
                continue
            sym = str(p.get("symbol") or "")
            raw_ps = str(p.get("positionSide") or "BOTH").upper()
            if raw_ps == "BOTH":
                pos_side = "long" if amt > 0 else "short"
            else:
                pos_side = raw_ps.lower()
            entry = float(p.get("entryPrice") or 0.0)
            mark = float(p.get("markPrice") or entry)
            notional = abs(float(p.get("notional") or (amt * mark)))
            upl = float(p.get("unRealizedProfit") or 0.0)
            positions.append(
                ExchangePosition(
                    inst_id=sym,
                    symbol=sym,
                    pos_side=pos_side,
                    mgn_mode=str(p.get("marginType") or "cross").lower(),
                    pos=abs(amt),
                    avail_pos=abs(amt),
                    avg_px=entry,
                    upl=upl,
                    upl_ratio=(upl / notional) if notional > 0 else 0.0,
                    notional_usd=notional,
                    lever=float(p.get("leverage") or 1.0),
                    u_time=int(p.get("updateTime") or 0),
                )
            )
        return positions

    async def get_instruments(self, force_refresh: bool = False) -> Dict[str, InstrumentSpec]:
        now = time.time()
        if not force_refresh and self._instruments_cache and (now - self._instruments_cache_ts < self._cache_ttl_sec):
            return self._instruments_cache

        info = await self._request("GET", "/fapi/v1/exchangeInfo", auth=False)
        result: Dict[str, InstrumentSpec] = {}
        for item in info.get("symbols", []):
            if item.get("status") != "TRADING" or item.get("quoteAsset") != "USDT":
                continue
            sym = item.get("symbol")
            if not sym:
                continue
            tick_sz = Decimal("0.01")
            lot_sz = Decimal("0.001")
            min_sz = Decimal("0.001")
            min_notional = Decimal("5")
            for f in item.get("filters", []):
                ft = f.get("filterType")
                if ft == "PRICE_FILTER":
                    tick_sz = Decimal(str(f.get("tickSize") or "0.01")).normalize()
                elif ft == "LOT_SIZE":
                    lot_sz = Decimal(str(f.get("stepSize") or "0.001")).normalize()
                    min_sz = Decimal(str(f.get("minQty") or "0.001")).normalize()
                elif ft == "MIN_NOTIONAL":
                    min_notional = Decimal(str(f.get("notional") or "5"))
            result[sym] = InstrumentSpec(
                inst_id=sym,
                ct_val=Decimal("1"),
                min_sz=min_sz,
                lot_sz=lot_sz,
                tick_sz=tick_sz,
                max_lvr=20.0,
                min_notional=min_notional,
                settle_ccy="USDT",
            )
        if result:
            self._instruments_cache = result
            self._instruments_cache_ts = now
        return self._instruments_cache

    async def get_orderbook(self, inst_id: str, depth: int = 5) -> OrderBook:
        data = await self._request(
            "GET", "/fapi/v1/depth", params={"symbol": inst_id, "limit": depth}, auth=False
        )
        return OrderBook(
            bids=[[float(x[0]), float(x[1])] for x in data.get("bids", [])],
            asks=[[float(x[0]), float(x[1])] for x in data.get("asks", [])],
        )

    async def ensure_leverage(
        self,
        inst_id: str,
        lever: int,
        pos_side: str = "long",
        mgn_mode: str = "cross",
    ) -> bool:
        try:
            await self._request("POST", "/fapi/v1/leverage", params={"symbol": inst_id, "leverage": lever})
            return True
        except ExchangeAPIError:
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
        params: Dict[str, Any] = {
            "symbol": inst_id,
            "side": side.upper(),
            "quantity": str(sz),
        }
        ot = ord_type.lower()
        if ot == "ioc":
            params["type"] = "LIMIT"
            params["timeInForce"] = "IOC"
            if px is not None:
                params["price"] = str(px)
        elif ot == "limit":
            params["type"] = "LIMIT"
            params["timeInForce"] = "GTC"
            if px is not None:
                params["price"] = str(px)
        else:
            params["type"] = "MARKET"

        if self.position_mode == "hedge":
            params["positionSide"] = pos_side.upper()
        else:
            params["positionSide"] = "BOTH"
            if reduce_only:
                params["reduceOnly"] = "true"

        if cl_ord_id:
            params["newClientOrderId"] = cl_ord_id[:32]

        data = await self._request("POST", "/fapi/v1/order", params=params)
        return OrderResult(
            ord_id=str(data.get("orderId") or ""),
            cl_ord_id=str(data.get("clientOrderId") or cl_ord_id or ""),
            fill_sz=float(data.get("executedQty") or 0.0),
            avg_px=float(data.get("avgPrice") or 0.0),
            state=str(data.get("status") or "NEW"),
            raw=data,
        )

    async def get_order(
        self,
        inst_id: str,
        ord_id: Optional[str] = None,
        cl_ord_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        params: Dict[str, Any] = {"symbol": inst_id}
        if ord_id:
            params["orderId"] = ord_id
        if cl_ord_id:
            params["origClientOrderId"] = cl_ord_id
        try:
            row = await self._request("GET", "/fapi/v1/order", params=params)
        except ExchangeAPIError:
            return None
        return {
            "ordId": str(row.get("orderId") or ""),
            "clOrdId": row.get("clientOrderId"),
            "accFillSz": float(row.get("executedQty") or 0.0),
            "avgPx": float(row.get("avgPrice") or 0.0),
            "state": row.get("status"),
            "raw": row,
        }
