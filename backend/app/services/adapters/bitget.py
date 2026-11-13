from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
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


class BitgetAdapter(BaseExchangeAdapter):
    """Async Bitget V2 USDT-M Futures REST API adapter (Live & Demo)."""

    exchange = "bitget"
    BASE_URL = "https://api.bitget.com"

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
        self.is_demo = environment == "demo"
        self.product_type = "SUSDT-FUTURES" if self.is_demo else "USDT-FUTURES"
        self._instruments_cache: Dict[str, InstrumentSpec] = {}
        self._instruments_cache_ts: float = 0.0
        self._cache_ttl_sec: float = 600.0

    def _sign(self, timestamp_ms: str, method: str, request_path: str, body_str: str = "") -> str:
        msg = f"{timestamp_ms}{method.upper()}{request_path}{body_str}"
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
        self._check_rate_limit()
        method = method.upper()
        # A mutation may already have reached the exchange when its response is lost.
        attempts = max(1, retries) if method == "GET" else 1
        clean_params = [(k, v) for k, v in sorted((params or {}).items()) if v is not None]
        query_str = urlencode(clean_params)
        request_path = path + (f"?{query_str}" if query_str else "")
        url = f"{self.BASE_URL}{request_path}"
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""

        last_err: Optional[Exception] = None
        async with ProxyManager.build_client(
            proxy_url=self.proxy_url, timeout=self.timeout, exchange=self.exchange,
        ) as client:
            for attempt in range(attempts):
                headers: Dict[str, str] = {
                    "Content-Type": "application/json",
                    "locale": "en-US",
                    "User-Agent": "MirrorQuant/1.0",
                }
                if self.is_demo:
                    headers["paptrading"] = "1"
                if auth:
                    ts = str(int(time.time() * 1000) + self._time_offset_ms)
                    headers["ACCESS-KEY"] = self.api_key
                    headers["ACCESS-SIGN"] = self._sign(ts, method, request_path, body_str)
                    headers["ACCESS-TIMESTAMP"] = ts
                    headers["ACCESS-PASSPHRASE"] = self.passphrase

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
                        raise ExchangeAPIError("bitget", str(resp.status_code), f"HTTP {resp.status_code}: {resp.text[:200]}")

                    payload = resp.json()
                    code = str(payload.get("code", ""))
                    if code != "00000":
                        if code == "429":
                            self._note_rate_limit(resp)
                        raise ExchangeAPIError("bitget", code, payload.get("msg", ""), payload)
                    return payload.get("data")
                except ExchangeAPIError:
                    raise
                except Exception as exc:
                    last_err = exc
                    if attempt < attempts - 1:
                        await asyncio.sleep(0.5 * (attempt + 1))
                    else:
                        raise
        raise RuntimeError(f"Bitget request failed ({path}): {last_err}")

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
        data = await self._request("GET", "/api/v2/public/time", auth=False, retries=1)
        if not isinstance(data, dict) or not data.get("serverTime"):
            raise RuntimeError("Missing Bitget server time")
        return self._calibrate_time(int(data["serverTime"]), sent_ms)

    async def test_connection(self) -> Dict[str, Any]:
        bal = await self.get_usdt_balance()
        positions = await self.get_positions()
        return {
            "ok": True,
            "exchange": "bitget",
            "environment": self.environment,
            "productType": self.product_type,
            "posMode": self.position_mode,
            "usdtEquity": bal.eq,
            "availEquity": bal.avail_eq,
            "openPositions": len(positions),
        }

    async def get_usdt_balance(self) -> BalanceInfo:
        data = await self._request(
            "GET",
            "/api/v2/mix/account/accounts",
            params={"productType": self.product_type},
        )
        for acct in data or []:
            if str(acct.get("marginCoin") or "").upper() in ("USDT", "SUSDT"):
                raw_eq = acct.get("accountEquity")
                eq = float(raw_eq if raw_eq not in (None, "") else acct.get("usdtEquity") or 0)
                # An explicit zero or unknown availability is never replaced by equity.
                raw_avail = acct.get("crossedMaxAvailable") if self.margin_mode == "cross" else acct.get("available")
                avail = float(raw_avail) if raw_avail not in (None, "") else 0.0
                upl = float(acct.get("unrealizedPL") or 0.0)
                cash = eq - upl
                return BalanceInfo(eq=eq, avail_eq=avail, cash_bal=cash, upl=upl, raw=acct)
        return BalanceInfo(eq=0.0, avail_eq=0.0, cash_bal=0.0, upl=0.0)

    async def get_positions(self) -> List[ExchangePosition]:
        margin_coin = "SUSDT" if self.is_demo else "USDT"
        data = await self._request(
            "GET",
            "/api/v2/mix/position/all-position",
            params={"productType": self.product_type, "marginCoin": margin_coin},
        )
        positions: List[ExchangePosition] = []
        for p in data or []:
            sz = float(p.get("total") or 0.0)
            if sz <= 0:
                continue
            sym = str(p.get("symbol") or "")
            hold_side = str(p.get("holdSide") or "").lower()
            hold_side = {"buy": "long", "sell": "short"}.get(hold_side, hold_side)
            if hold_side not in ("long", "short"):
                raise ValueError("Unknown Bitget position direction")
            avg_px = float(p.get("openPriceAvg") or 0.0)
            mark_px = float(p.get("markPrice") or avg_px)
            notional = abs(sz * mark_px)
            upl = float(p.get("unrealizedPL") or 0.0)
            positions.append(
                ExchangePosition(
                    inst_id=sym,
                    symbol=sym,
                    pos_side=hold_side,
                    mgn_mode="isolated" if str(p.get("marginMode") or "").lower() == "isolated" else "cross",
                    pos=abs(sz),
                    avail_pos=abs(float(p["available"])) if p.get("available") not in (None, "") else 0.0,
                    avg_px=avg_px,
                    upl=upl,
                    upl_ratio=(upl / notional) if notional > 0 else 0.0,
                    notional_usd=notional,
                    lever=float(p.get("leverage") or 1.0),
                    c_time=int(p.get("cTime") or 0),
                    u_time=int(p.get("uTime") or 0),
                )
            )
        return positions

    async def get_instruments(self, force_refresh: bool = False) -> Dict[str, InstrumentSpec]:
        now = time.time()
        if not force_refresh and self._instruments_cache and now - self._instruments_cache_ts < self._cache_ttl_sec:
            return self._instruments_cache
        data = await self._request("GET", "/api/v2/mix/market/contracts", params={"productType": self.product_type}, auth=False)
        result: Dict[str, InstrumentSpec] = {}
        for item in data or []:
            if item.get("symbolStatus") != "normal" or item.get("symbolType") != "perpetual":
                continue
            try:
                sym = item["symbol"]
                if item["quoteCoin"] not in ("USDT", "SUSDT"):
                    continue
                tick_sz = Decimal(str(item["priceEndStep"])) * Decimal("10") ** -int(item["pricePlace"])
                result[sym] = InstrumentSpec(
                    inst_id=sym, ct_val=Decimal("1"), min_sz=Decimal(str(item["minTradeNum"])),
                    lot_sz=Decimal(str(item["sizeMultiplier"])), tick_sz=tick_sz,
                    max_sz=Decimal(str(item["maxOrderQty"])), market_max_sz=Decimal(str(item["maxMarketOrderQty"])),
                    max_lvr=float(item["maxLever"]), min_notional=Decimal(str(item["minTradeUSDT"])),
                    settle_ccy="USDT", base_ccy=item["baseCoin"],
                )
            except (ValueError, KeyError, ArithmeticError, TypeError):
                continue
        self._instruments_cache, self._instruments_cache_ts = result, now
        return result

    async def get_orderbook(self, inst_id: str, depth: int = 5) -> OrderBook:
        if not 1 <= depth <= 50:
            raise ValueError("Invalid requested book depth")
        wire_depth = next(n for n in (1, 5, 15, 50) if n >= depth)
        data = await self._request("GET", "/api/v2/mix/market/merge-depth", params={
            "productType": self.product_type, "symbol": inst_id, "limit": str(wire_depth)
        }, auth=False)
        book = data if isinstance(data, dict) else {}
        return OrderBook(
            bids=[[float(x[0]), float(x[1])] for x in book.get("bids", [])[:depth]],
            asks=[[float(x[0]), float(x[1])] for x in book.get("asks", [])[:depth]],
        )

    async def ensure_leverage(self, inst_id: str, lever: int, pos_side: str = "long", mgn_mode: str = "cross") -> bool:
        self._assert_supported_entry_mode(mgn_mode)
        params = {"symbol": inst_id, "productType": self.product_type, "marginCoin": "SUSDT" if self.is_demo else "USDT"}
        account = await self._request("GET", "/api/v2/mix/account/account", params=params)
        if not account or account.get("posMode") != "hedge_mode" or account.get("marginMode") != "crossed":
            raise ExchangeAPIError(self.exchange, "UNSUPPORTED_MODE", "Bitget symbol must already use hedge and cross margin")
        body = dict(params, leverage=str(lever))
        try:
            await self._request("POST", "/api/v2/mix/account/set-leverage", body=body)
        except ExchangeAPIError:
            return False
        account = await self._request("GET", "/api/v2/mix/account/account", params=params)
        return bool(account) and float(account.get("crossedMarginLeverage") or 0) == lever

    async def get_max_leverage(self, inst_id: str, target_notional: Any) -> int:
        rows = await self._request("GET", "/api/v2/mix/market/query-position-lever", params={
            "productType": self.product_type, "symbol": inst_id
        }, auth=False)
        for tier in sorted(rows or [], key=lambda row: Decimal(str(row["endUnit"]))):
            if Decimal(str(tier["startUnit"])) <= Decimal(str(target_notional)) <= Decimal(str(tier["endUnit"])):
                return int(float(tier["leverage"]))
        raise ValueError("Target exposure exceeds qualified Bitget tiers")

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
        bg_margin_mode = "isolated" if td_mode == "isolated" else "crossed"
        body: Dict[str, Any] = {
            "symbol": inst_id,
            "productType": self.product_type,
            "marginMode": bg_margin_mode,
            "marginCoin": "SUSDT" if self.is_demo else "USDT",
            "size": str(sz),
            "orderType": "market" if ot == "market" else "limit",
        }
        if self.position_mode == "hedge":
            # Bitget V2 hedge mode: side = position direction (buy=long, sell=short), tradeSide = open/close
            body["side"] = "buy" if pos_side.lower() == "long" else "sell"
            body["tradeSide"] = "close" if reduce_only else "open"
        else:
            body["side"] = side.lower()
            if reduce_only:
                body["reduceOnly"] = "YES"

        if ot == "ioc":
            body["force"] = "ioc"
            if px is not None:
                body["price"] = str(px)
        elif ot == "limit":
            body["force"] = "gtc"
            if px is not None:
                body["price"] = str(px)

        if cl_ord_id:
            body["clientOid"] = cl_ord_id[:32]

        res = await self._request("POST", "/api/v2/mix/order/place-order", body=body)
        data = res if isinstance(res, dict) else {}
        return OrderResult(
            ord_id=str(data.get("orderId") or ""),
            cl_ord_id=str(data.get("clientOid") or cl_ord_id or ""),
            raw=data,
        )

    async def get_order(
        self,
        inst_id: str,
        ord_id: Optional[str] = None,
        cl_ord_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        params: Dict[str, Any] = {"productType": self.product_type, "symbol": inst_id}
        if ord_id:
            params["orderId"] = ord_id
        if cl_ord_id:
            params["clientOid"] = cl_ord_id
        try:
            row = await self._request("GET", "/api/v2/mix/order/detail", params=params)
        except ExchangeAPIError as exc:
            if str(exc.code) in ("40768", "40762"):
                return None
            raise
        if not row:
            return None
        if not isinstance(row, dict):
            raise ValueError("Malformed Bitget order lookup response")
        return {
            "ordId": row.get("orderId"),
            "clOrdId": row.get("clientOid"),
            "accFillSz": float(row.get("baseVolume") or 0.0),
            "avgPx": float(row.get("priceAvg") or 0.0),
            "state": row.get("state"),
            "raw": row,
        }

    async def get_open_orders(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        queries = [("/api/v2/mix/order/orders-pending", {})]
        queries.extend(("/api/v2/mix/order/orders-plan-pending", {"planType": kind})
                       for kind in ("normal_plan", "profit_loss", "track_plan"))
        for path, extra in queries:
            result = await self._request("GET", path, params={
                "productType": self.product_type, "limit": 100, **extra,
            })
            page = (result or {}).get("entrustedList")
            if not isinstance(page, list):
                raise ValueError("Incomplete Bitget pending-order inventory")
            # A full page cannot prove absence of additional orders. Fail closed
            # until this venue's page boundary is explicitly reconciled.
            if len(page) >= 100:
                raise ValueError("Bitget pending-order inventory exceeds one qualified page")
            rows.extend(page)
        return rows

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
        margin_coin = "SUSDT" if self.is_demo else "USDT"
        out: Dict[str, Any] = {"ok": True, "tpOrderId": None, "slOrderId": None}

        async def _submit_plan(plan_type: str, trig_px: str) -> str:
            body: Dict[str, Any] = {
                "marginCoin": margin_coin,
                "productType": self.product_type,
                "symbol": inst_id,
                "planType": plan_type,
                "triggerPrice": str(trig_px),
                "triggerType": "mark_price",
                "holdSide": pos_side.lower() if self.position_mode == "hedge" else ("buy" if pos_side.lower() == "long" else "sell"),
                "size": str(sz),
            }
            res = await self._request("POST", "/api/v2/mix/order/place-tpsl-order", body=body)
            order_id = str((res or {}).get("orderId") or "")
            if not order_id:
                raise ExchangeAPIError(self.exchange, "AMBIGUOUS", "Missing protective order identity")
            return order_id

        if tp_px:
            out["tpOrderId"] = await _submit_plan("pos_profit", tp_px)
        if sl_px:
            out["slOrderId"] = await _submit_plan("pos_loss", sl_px)
        return out

    async def cancel_tp_sl(self, inst_id: str, pos_side: Optional[str] = None) -> bool:
        try:
            pending = await self._request(
                "GET",
                "/api/v2/mix/order/orders-plan-pending",
                params={"productType": self.product_type, "symbol": inst_id, "planType": "profit_loss"},
            )
            items = (pending or {}).get("entrustedList") or []
            ids = [
                {"orderId": x["orderId"]}
                for x in items
                if x.get("orderId") and (not pos_side or str(x.get("posSide") or "").lower() == pos_side.lower())
            ]
            if ids:
                await self._request(
                    "POST",
                    "/api/v2/mix/order/cancel-plan-order",
                    body={
                        "productType": self.product_type,
                        "symbol": inst_id,
                        "marginCoin": "SUSDT" if self.is_demo else "USDT",
                        "orderIdList": ids[:20],
                    },
                )
                return True
            return False
        except Exception:
            return False
