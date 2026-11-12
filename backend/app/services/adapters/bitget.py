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
        method = method.upper()
        clean_params = [(k, v) for k, v in sorted((params or {}).items()) if v is not None]
        query_str = urlencode(clean_params)
        request_path = path + (f"?{query_str}" if query_str else "")
        url = f"{self.BASE_URL}{request_path}"
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""

        last_err: Optional[Exception] = None
        async with ProxyManager.build_client(proxy_url=self.proxy_url, timeout=self.timeout) as client:
            for attempt in range(retries):
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

                    if resp.status_code == 429 and attempt < retries - 1:
                        await asyncio.sleep(0.8 * (attempt + 1))
                        continue
                    if resp.status_code >= 400:
                        raise ExchangeAPIError("bitget", str(resp.status_code), f"HTTP {resp.status_code}: {resp.text[:200]}")

                    payload = resp.json()
                    code = str(payload.get("code", ""))
                    if code != "00000":
                        if code in ("40014", "40019") and attempt < retries - 1:
                            await asyncio.sleep(0.8 * (attempt + 1))
                            continue
                        raise ExchangeAPIError("bitget", code, payload.get("msg", ""), payload)
                    return payload.get("data")
                except ExchangeAPIError as exc:
                    last_err = exc
                    if exc.code not in ("40014", "40019"):
                        raise
                except Exception as exc:
                    last_err = exc
                    if attempt < retries - 1:
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
        return inst_id, inst_id in instruments

    async def sync_time(self) -> int:
        data = await self._request("GET", "/api/v2/public/time", auth=False)
        if isinstance(data, dict) and data.get("serverTime"):
            self._time_offset_ms = int(data["serverTime"]) - int(time.time() * 1000)
        return self._time_offset_ms

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
                eq = float(acct.get("accountEquity") or acct.get("usdtEquity") or 0.0)
                avail = float(acct.get("available") or acct.get("crossedMaxAvailable") or eq)
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
            hold_side = str(p.get("holdSide") or "long").lower()
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
                    avail_pos=abs(float(p.get("available") or sz)),
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
        if not force_refresh and self._instruments_cache and (now - self._instruments_cache_ts < self._cache_ttl_sec):
            return self._instruments_cache

        data = await self._request(
            "GET",
            "/api/v2/mix/market/contracts",
            params={"productType": self.product_type},
            auth=False,
        )
        result: Dict[str, InstrumentSpec] = {}
        for item in data or []:
            if str(item.get("symbolStatus") or "normal").lower() != "normal":
                continue
            sym = item.get("symbol")
            if not sym:
                continue
            try:
                price_place = int(item.get("pricePlace") or 2)
                vol_place = int(item.get("volumePlace") or 3)
                tick_sz = Decimal("10") ** (-price_place)
                lot_sz = Decimal(str(item.get("sizeMultiplier") or (Decimal("10") ** (-vol_place))))
                min_sz = Decimal(str(item.get("minTradeNum") or lot_sz))
                result[sym] = InstrumentSpec(
                    inst_id=sym,
                    ct_val=Decimal("1"),
                    min_sz=min_sz.normalize(),
                    lot_sz=lot_sz.normalize(),
                    tick_sz=tick_sz.normalize(),
                    max_lvr=float(item.get("maxLever") or 20.0),
                    min_notional=Decimal(str(item.get("minTradeUSDT") or "5")),
                    settle_ccy="USDT",
                )
            except Exception:
                continue
        if result:
            self._instruments_cache = result
            self._instruments_cache_ts = now
        return self._instruments_cache

    async def get_orderbook(self, inst_id: str, depth: int = 5) -> OrderBook:
        data = await self._request(
            "GET",
            "/api/v2/mix/market/merge-depth",
            params={"productType": self.product_type, "symbol": inst_id, "limit": str(depth)},
            auth=False,
        )
        book = data if isinstance(data, dict) else {}
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
            "symbol": inst_id,
            "productType": self.product_type,
            "marginCoin": "SUSDT" if self.is_demo else "USDT",
            "leverage": str(lever),
        }
        if self.position_mode == "hedge":
            body["holdSide"] = pos_side.lower()
        try:
            await self._request("POST", "/api/v2/mix/account/set-leverage", body=body)
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
        except ExchangeAPIError:
            return None
        if not isinstance(row, dict):
            return None
        return {
            "ordId": row.get("orderId"),
            "clOrdId": row.get("clientOid"),
            "accFillSz": float(row.get("baseVolume") or 0.0),
            "avgPx": float(row.get("priceAvg") or 0.0),
            "state": row.get("state"),
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
                "holdSide": pos_side.lower() if self.position_mode == "hedge" else ("long" if pos_side.lower() == "long" else "short"),
                "size": str(sz),
            }
            res = await self._request("POST", "/api/v2/mix/order/place-tpsl-order", body=body)
            return str((res or {}).get("orderId") or "")

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
