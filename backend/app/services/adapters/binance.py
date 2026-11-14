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
        self._check_rate_limit()
        method = method.upper()
        # A mutation may already have reached the exchange when its response is lost.
        attempts = max(1, retries) if method == "GET" else 1
        base_params = dict(params or {})

        async with ProxyManager.build_client(
            proxy_url=self.proxy_url, timeout=self.timeout, exchange=self.exchange,
        ) as client:
            last_err: Optional[Exception] = None
            for attempt in range(attempts):
                req_params = dict(base_params)
                headers: Dict[str, str] = {"User-Agent": "MirrorQuant/1.0"}
                if auth:
                    req_params["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
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

                    if resp.status_code in (418, 429):
                        self._note_rate_limit(resp)

                    data = resp.json()
                    if resp.status_code >= 400 or (isinstance(data, dict) and "code" in data and data["code"] < 0):
                        code = str(data.get("code", resp.status_code)) if isinstance(data, dict) else str(resp.status_code)
                        msg = data.get("msg", resp.text[:200]) if isinstance(data, dict) else resp.text[:200]
                        if code in ("-1003", "-1015"):
                            self._note_rate_limit(resp)
                        raise ExchangeAPIError("binance", code, msg, data)
                    return data
                except ExchangeAPIError:
                    raise
                except Exception as exc:
                    last_err = exc
                    if attempt < attempts - 1:
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
        return inst_id, self._symbol_matches(lead_symbol, inst_id, instruments)

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
            "totalEquity": bal.eq,
            "usdtEquity": bal.usdt_eq if hasattr(bal, "usdt_eq") and bal.usdt_eq > 0 else bal.eq,
            "usdcEquity": getattr(bal, "usdc_eq", 0.0),
            "availEquity": bal.avail_eq,
            "openPositions": len(positions),
            "assetBalances": getattr(bal, "asset_balances", {}),
        }

    async def get_usdt_balance(self) -> BalanceInfo:
        data = await self._request("GET", "/fapi/v2/account")
        assets = {str(item.get("asset") or "").upper(): item for item in data.get("assets") or []}
        usdt = assets.get("USDT", {})
        usdc = assets.get("USDC", {})
        # Per-asset marginBalance includes cross and isolated unrealized PnL.
        usdt_mb = float(usdt.get("marginBalance") or 0.0)
        usdt_avail = float(usdt.get("availableBalance") or 0.0)
        usdt_wallet = float(usdt.get("walletBalance") or 0.0)
        usdt_upl = float(usdt.get("unrealizedProfit") or 0.0)
        usdc_mb = float(usdc.get("marginBalance") or 0.0)
        usdc_avail = float(usdc.get("availableBalance") or 0.0)
        usdc_wallet = float(usdc.get("walletBalance") or 0.0)
        usdc_upl = float(usdc.get("unrealizedProfit") or 0.0)

        asset_balances = {}
        for asset_name, item in assets.items():
            mb = float(item.get("marginBalance") or 0.0)
            avail = float(item.get("availableBalance") or 0.0)
            wb = float(item.get("walletBalance") or 0.0)
            asset_upl = float(item.get("unrealizedProfit") or 0.0)
            if mb > 0 or wb > 0:
                asset_balances[asset_name] = {"equity": mb, "available": avail, "wallet": wb, "upl": asset_upl}

        is_multi_assets = bool(data.get("multiAssetsMargin", False))
        if is_multi_assets and float(data.get("totalMarginBalance") or 0.0) > 0:
            eq = float(data.get("totalMarginBalance"))
            avail_eq = float(data.get("availableBalance") or 0.0)
            cash_bal = float(data.get("totalWalletBalance") or 0.0)
            upl = float(data.get("totalUnrealizedProfit") or 0.0)
        else:
            # Single-asset account totals omit USDC; combine stablecoin balances 1:1.
            eq = usdt_mb + usdc_mb
            avail_eq = usdt_avail + usdc_avail
            cash_bal = usdt_wallet + usdc_wallet
            upl = usdt_upl + usdc_upl

        if eq == 0.0 and avail_eq == 0.0 and cash_bal == 0.0 and not asset_balances:
            return BalanceInfo(eq=0.0, avail_eq=0.0, cash_bal=0.0, upl=0.0, raw=data)
        return BalanceInfo(
            eq=eq, avail_eq=avail_eq, cash_bal=cash_bal, upl=upl, currency="USDT", raw=data,
            usdt_eq=usdt_mb, usdc_eq=usdc_mb, asset_balances=asset_balances,
        )

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
                    cur_px=mark,
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
        if not force_refresh and self._instruments_cache and now - self._instruments_cache_ts < self._cache_ttl_sec:
            return self._instruments_cache
        info = await self._request("GET", "/fapi/v1/exchangeInfo", auth=False)
        result: Dict[str, InstrumentSpec] = {}
        for item in info.get("symbols", []):
            if (item.get("status") != "TRADING" or item.get("quoteAsset") != "USDT"
                    or item.get("marginAsset") != "USDT" or item.get("contractType") != "PERPETUAL"):
                continue
            try:
                sym = item["symbol"]
                filters = {row["filterType"]: row for row in item["filters"]}
                lot, market, price = filters["LOT_SIZE"], filters["MARKET_LOT_SIZE"], filters["PRICE_FILTER"]
                result[sym] = InstrumentSpec(
                    inst_id=sym, ct_val=Decimal("1"), min_sz=Decimal(str(lot["minQty"])),
                    lot_sz=Decimal(str(lot["stepSize"])), max_sz=Decimal(str(lot["maxQty"])),
                    market_min_sz=Decimal(str(market["minQty"])), market_lot_sz=Decimal(str(market["stepSize"])),
                    market_max_sz=Decimal(str(market["maxQty"])), tick_sz=Decimal(str(price["tickSize"])),
                    # Binance publishes actual caps in signed leverageBracket, consulted before entry.
                    max_lvr=125, min_notional=Decimal(str(filters["MIN_NOTIONAL"]["notional"])),
                    settle_ccy="USDT", base_ccy=item["baseAsset"],
                )
            except (ValueError, KeyError, ArithmeticError, TypeError):
                continue
        self._instruments_cache, self._instruments_cache_ts = result, now
        return result

    async def get_orderbook(self, inst_id: str, depth: int = 5) -> OrderBook:
        if not 1 <= depth <= 1000:
            raise ValueError("Invalid requested book depth")
        wire_depth = next(n for n in (5, 10, 20, 50, 100, 500, 1000) if n >= depth)
        data = await self._request("GET", "/fapi/v1/depth", params={"symbol": inst_id, "limit": wire_depth}, auth=False)
        return OrderBook(
            bids=[[float(x[0]), float(x[1])] for x in data.get("bids", [])[:depth]],
            asks=[[float(x[0]), float(x[1])] for x in data.get("asks", [])[:depth]],
        )

    async def ensure_leverage(self, inst_id: str, lever: int, pos_side: str = "long", mgn_mode: str = "cross") -> bool:
        self._assert_supported_entry_mode(mgn_mode)
        dual = await self._request("GET", "/fapi/v1/positionSide/dual")
        assets = await self._request("GET", "/fapi/v1/multiAssetsMargin")
        if dual.get("dualSidePosition") is not True or assets.get("multiAssetsMargin") is not False:
            raise ExchangeAPIError(self.exchange, "UNSUPPORTED_MODE", "Binance requires hedge mode and single-asset margin")
        rows = await self._request("GET", "/fapi/v2/positionRisk", params={"symbol": inst_id})
        if not rows or any(row.get("marginType") not in ("cross", "crossed") for row in rows):
            raise ExchangeAPIError(self.exchange, "UNSUPPORTED_MODE", "Binance symbol must already use cross margin")
        try:
            result = await self._request("POST", "/fapi/v1/leverage", params={"symbol": inst_id, "leverage": lever})
        except ExchangeAPIError:
            return False
        if int(result.get("leverage") or 0) != lever:
            return False
        rows = await self._request("GET", "/fapi/v2/positionRisk", params={"symbol": inst_id})
        return bool(rows) and all(float(row.get("leverage") or 0) == lever for row in rows)

    async def get_max_leverage(self, inst_id: str, target_notional: Any) -> int:
        data = await self._request("GET", "/fapi/v1/leverageBracket", params={"symbol": inst_id})
        records = data if isinstance(data, list) else [data]
        for record in records:
            if record.get("symbol") != inst_id:
                continue
            coefficient = Decimal(str(record.get("notionalCoef") or "1"))
            for bracket in record.get("brackets") or []:
                if Decimal(str(bracket["notionalFloor"])) * coefficient <= Decimal(str(target_notional)) < Decimal(str(bracket["notionalCap"])) * coefficient:
                    return int(bracket["initialLeverage"])
        raise ValueError("Target exposure exceeds qualified Binance tiers")

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
        except ExchangeAPIError as exc:
            if str(exc.code) in ("-2013", "-2011"):
                return None
            raise
        return {
            "ordId": str(row.get("orderId") or ""),
            "clOrdId": row.get("clientOrderId"),
            "accFillSz": float(row.get("executedQty") or 0.0),
            "avgPx": float(row.get("avgPrice") or 0.0),
            "state": row.get("status"),
            "raw": row,
        }

    async def get_open_orders(self) -> List[Dict[str, Any]]:
        ordinary = await self._request("GET", "/fapi/v1/openOrders")
        conditional = await self._request("GET", "/fapi/v1/openAlgoOrders")
        if isinstance(conditional, dict):
            conditional = conditional.get("orders")
        if not isinstance(ordinary, list) or not isinstance(conditional, list):
            raise ValueError("Incomplete Binance pending-order inventory")
        return ordinary + conditional

    async def sync_time(self) -> int:
        sent_ms = time.time() * 1000
        data = await self._request("GET", "/fapi/v1/time", auth=False, retries=1)
        if not data.get("serverTime"):
            raise RuntimeError("Missing Binance server time")
        return self._calibrate_time(int(data["serverTime"]), sent_ms)

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
        close_side = "SELL" if pos_side.lower() == "long" else "BUY"
        out: Dict[str, Any] = {"ok": True, "tpOrderId": None, "slOrderId": None}

        async def _submit_cond(ord_type: str, stop_px: str) -> str:
            params: Dict[str, Any] = {
                "symbol": inst_id,
                "side": close_side,
                "type": ord_type,
                "triggerPrice": str(stop_px),
                "algoType": "CONDITIONAL",
                "quantity": str(sz),
                "workingType": "MARK_PRICE",
                "priceProtect": "TRUE",
            }
            if self.position_mode == "hedge":
                params["positionSide"] = pos_side.upper()
            else:
                params["positionSide"] = "BOTH"
                params["reduceOnly"] = "true"
            res = await self._request("POST", "/fapi/v1/algoOrder", params=params)
            order_id = str(res.get("algoId") or "")
            if not order_id:
                raise ExchangeAPIError(self.exchange, "AMBIGUOUS", "Missing protective order identity")
            return order_id

        if tp_px:
            out["tpOrderId"] = await _submit_cond("TAKE_PROFIT_MARKET", tp_px)
        if sl_px:
            out["slOrderId"] = await _submit_cond("STOP_MARKET", sl_px)
        return out

    async def cancel_tp_sl(self, inst_id: str, pos_side: Optional[str] = None) -> bool:
        try:
            open_orders = await self._request("GET", "/fapi/v1/openAlgoOrders", params={"symbol": inst_id})
            cancelled = False
            for o in (open_orders.get("orders", []) if isinstance(open_orders, dict) else open_orders or []):
                if (o.get("orderType") or o.get("type")) in ("STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT"):
                    if pos_side and str(o.get("positionSide") or "").lower() not in (pos_side.lower(), "both"):
                        continue
                    await self._request(
                        "DELETE",
                        "/fapi/v1/algoOrder",
                        params={"symbol": inst_id, "algoId": o["algoId"]},
                    )
                    cancelled = True
            return cancelled
        except Exception:
            return False
