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
    AccountRiskInfo,
    BalanceInfo,
    BaseExchangeAdapter,
    CashflowEntry,
    CommissionRateInfo,
    DatasetCapability,
    DatasetPage,
    ExchangeAPIError,
    ExchangePosition,
    InstrumentSpec,
    OpenOrderEntry,
    OrderBook,
    OrderHistoryEntry,
    OrderResult,
    TradeFill,
    api_error,
    make_dedupe_key,
    safe_decimal,
    safe_optional_float,
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
        self._check_rate_limit(auth)  # 私有冷却不得串扰其他账户。
        method = method.upper()
        # A mutation may already have reached the exchange when its response is lost.
        attempts = max(1, retries) if method == "GET" else 1
        clean_params = [(k, v) for k, v in sorted((params or {}).items()) if v is not None]
        query_str = urlencode(clean_params)
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
        url = f"{self.base_url}{path}" + (f"?{query_str}" if query_str else "")
        recv_window = "5000"

        last_err: Optional[Exception] = None
        async with self.request_client(auth) as client:
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
                        self._note_rate_limit(resp, auth)
                    # 状态码不能替代可解析的业务拒绝证据。
                    payload = resp.json()
                    if not isinstance(payload, dict):
                        raise ExchangeAPIError("bybit", "AMBIGUOUS", "Malformed API response")
                    ret_code = int(payload.get("retCode", -1))
                    if not 200 <= resp.status_code < 300 or ret_code != 0:
                        if ret_code in (10006, 10018):
                            self._note_rate_limit(resp, auth)
                        raise api_error("bybit", str(ret_code), payload.get("retMsg", ""), payload, resp.status_code)
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
            cur_px = float(p.get("markPrice") or p.get("lastPrice") or avg_px or 0.0)
            notional = abs(float(p.get("positionValue") or (sz * cur_px)))
            upl = float(p.get("unrealisedPnl") or 0.0)
            lever = float(p.get("leverage") or 1.0)
            margin_usd = float(p.get("positionIM") or 0.0)
            if margin_usd <= 0 and notional > 0 and lever > 0:
                margin_usd = notional / lever
            mgn_mode = "isolated" if int(p.get("tradeMode") or 0) == 1 else "cross"
            liq_price = safe_optional_float(p.get("liqPrice"), positive_only=True)
            maint_margin = safe_optional_float(p.get("positionMM"))
            init_margin = safe_optional_float(p.get("positionIM"))
            adl_q = int(p["adlRankIndicator"]) if p.get("adlRankIndicator") not in (None, "") else None
            iso_wallet = safe_optional_float(p.get("positionBalance")) if mgn_mode == "isolated" else None
            pos_mr = (maint_margin / iso_wallet) if (mgn_mode == "isolated" and maint_margin is not None and iso_wallet and iso_wallet > 0) else None
            positions.append(
                ExchangePosition(
                    inst_id=sym,
                    symbol=sym,
                    pos_side=pos_side,
                    mgn_mode=mgn_mode,
                    pos=abs(sz),
                    avail_pos=abs(sz),
                    avg_px=avg_px,
                    cur_px=cur_px,
                    upl=upl,
                    upl_ratio=(upl / margin_usd) if margin_usd > 0 else 0.0,
                    notional_usd=notional,
                    lever=lever,
                    c_time=int(p.get("createdTime") or 0),
                    u_time=int(p.get("updatedTime") or 0),
                    margin_usd=margin_usd,
                    liquidation_price=liq_price,
                    maintenance_margin=maint_margin,
                    initial_margin=init_margin,
                    adl_quantile=adl_q,
                    margin_ratio=pos_mr,
                    isolated_wallet=iso_wallet,
                )
            )
        return positions

    async def get_instruments(self, force_refresh: bool = False) -> Dict[str, InstrumentSpec]:
        cached = self._cached_instruments(force_refresh)
        if cached is not None:
            return cached
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
        return self._cache_instruments(result)

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

    async def cancel_order(self, inst_id: str, cl_ord_id: str) -> None:
        """撤销一笔普通挂单；撤单结果以随后的查单为准，失败时抛 ExchangeAPIError。"""
        await self._request("POST", "/v5/order/cancel",
                            body={"category": "linear", "symbol": inst_id, "orderLinkId": cl_ord_id})

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
            "accFillSz": float(row["cumExecQty"]) if row.get("cumExecQty") not in (None, "") else None,
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

    @staticmethod
    def _bybit_pos_side(position_idx: Any) -> str:
        try:
            idx = int(position_idx or 0)
        except (TypeError, ValueError):
            idx = 0
        if idx == 1:
            return "LONG"
        if idx == 2:
            return "SHORT"
        return "BOTH"

    def get_dataset_capabilities(self) -> Dict[str, DatasetCapability]:
        return {
            "equity": DatasetCapability("equity", True, True),
            "open_orders": DatasetCapability("open_orders", True, True),
            "cashflow": DatasetCapability("cashflow", True, False, retention_days=180, note="Bybit UTA transaction log window"),
            "fills": DatasetCapability("fills", True, False, retention_days=180, note="Bybit execution history window"),
            "orders": DatasetCapability("orders", True, False, retention_days=180, note="Bybit order history window"),
            "force_orders": DatasetCapability("force_orders", False, False, note="Not supported separately on Bybit"),
            "commission_rates": DatasetCapability("commission_rates", True, True),
            "daily_snapshots": DatasetCapability("daily_snapshots", False, False, note="Not supported on Bybit"),
        }

    async def get_account_risk_snapshot(self) -> AccountRiskInfo:
        res = await self._request("GET", "/v5/account/wallet-balance", params={"accountType": "UNIFIED"})
        acct = (res.get("list") or [{}])[0]
        coins = acct.get("coin") or []
        asset_balances: Dict[str, Dict[str, Optional[Decimal]]] = {}
        usdt: Dict[str, Any] = {}
        single_collateral = all(c.get("coin") == "USDT" or (safe_decimal(c.get("equity")) or Decimal("0")) == 0 for c in coins)
        for coin in coins:
            if not isinstance(coin, dict):
                continue
            name = str(coin.get("coin") or "").upper()
            if not name:
                continue
            eq = safe_decimal(coin.get("equity"))
            wb = safe_decimal(coin.get("walletBalance"))
            upl = safe_decimal(coin.get("unrealisedPnl"))
            im = safe_decimal(coin.get("totalPositionIM"))
            mm = safe_decimal(coin.get("totalPositionMM"))
            if name == "USDT":
                usdt = coin
            if any(v is not None and v != 0 for v in (eq, wb, upl, im, mm)) or name == "USDT":
                avail = safe_decimal(acct.get("totalAvailableBalance")) if (name == "USDT" and single_collateral) else None
                asset_balances[name] = {
                    "equity": eq, "available": avail, "wallet": wb,
                    "upl": upl, "initialMargin": im, "maintenanceMargin": mm,
                }
        equity = safe_decimal(usdt.get("equity"))
        avail_bal = safe_decimal(acct.get("totalAvailableBalance")) if single_collateral else None
        wallet_bal = safe_decimal(usdt.get("walletBalance"))
        upl_bal = safe_decimal(usdt.get("unrealisedPnl"))
        init_margin = safe_decimal(usdt.get("totalPositionIM") or acct.get("totalInitialMargin"))
        maint_margin = safe_decimal(usdt.get("totalPositionMM") or acct.get("totalMaintenanceMargin"))
        margin_ratio = safe_decimal(acct.get("accountMMRate"))
        source = "exchange" if margin_ratio is not None else None
        if margin_ratio is None and maint_margin is not None and equity is not None and equity > 0:
            margin_ratio = maint_margin / equity
            source = "derived_from_exchange_mmr"
        return AccountRiskInfo(
            currency="USDT",
            equity=equity,
            available_balance=avail_bal,
            wallet_balance=wallet_bal,
            unrealized_pnl=upl_bal,
            initial_margin=init_margin,
            maintenance_margin=maint_margin,
            margin_ratio=margin_ratio,
            margin_ratio_source=source,
            multi_assets_margin=(not single_collateral),
            dual_side_position=(self.position_mode == "hedge"),
            asset_balances=asset_balances,
        )

    async def get_open_orders_normalized(self) -> List[OpenOrderEntry]:
        rows = await self.get_open_orders()
        entries: List[OpenOrderEntry] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "").upper()
            stop_type = str(row.get("stopOrderType") or "")
            if stop_type in ("TakeProfit", "PartialTakeProfit"):
                category = "take_profit"
            elif stop_type in ("StopLoss", "PartialStopLoss"):
                category = "stop_loss"
            elif stop_type == "TrailingStop":
                category = "trailing_stop"
            elif stop_type or row.get("triggerPrice") not in (None, "", "0"):
                category = "conditional"
            else:
                category = "ordinary"
            close_pos = str(row.get("tpslMode") or "").lower() == "full"
            qty = None if close_pos else safe_decimal(row.get("qty"))
            lmt = safe_decimal(row.get("price"))
            trig = safe_decimal(row.get("triggerPrice"))
            entries.append(OpenOrderEntry(
                exchange="bybit",
                order_id=str(row.get("orderId") or row.get("orderLinkId") or ""),
                client_order_id=str(row.get("orderLinkId") or "") or None,
                algo_id=str(row.get("orderId")) if category != "ordinary" and row.get("orderId") else None,
                category=category,
                symbol=sym,
                inst_id=sym,
                side=str(row.get("side") or "").upper(),
                position_side=self._bybit_pos_side(row.get("positionIdx")),
                order_type=str(row.get("orderType") or "LIMIT").upper(),
                status=str(row.get("orderStatus") or "NEW").upper(),
                qty=qty,
                filled_qty=safe_decimal(row.get("cumExecQty")),
                limit_price=lmt if lmt and lmt > 0 else None,
                trigger_price=trig if trig and trig > 0 else None,
                reduce_only=bool(row.get("reduceOnly")) if row.get("reduceOnly") is not None else None,
                close_position=close_pos,
                working_type=str(row.get("triggerBy")) if row.get("triggerBy") else None,
                created_at_ms=int(row["createdTime"]) if row.get("createdTime") else None,
                updated_at_ms=int(row["updatedTime"]) if row.get("updatedTime") else None,
            ))
        return entries

    async def get_cashflow_history(
        self, start_ms: int, end_ms: int, cursor: Optional[Dict[str, Any]] = None
    ) -> DatasetPage:
        items: List[CashflowEntry] = []
        seen: set[str] = set()
        window = 7 * 86_400_000 - 1
        cur = start_ms
        while cur <= end_ms:
            w_end = min(end_ms, cur + window)
            rows = await self._all_pages("/v5/account/transaction-log", {
                "accountType": "UNIFIED", "category": "linear",
                "startTime": cur, "endTime": w_end, "limit": 50,
            })
            for row in rows:
                tx_id = str(row.get("id") or "").strip() or None
                ts = int(row.get("transactionTime") or 0)
                ccy = str(row.get("currency") or "").upper()
                amt = safe_decimal(row.get("cashFlow"))
                if amt is None:
                    amt = safe_decimal(row.get("change"))
                if not ccy or amt is None or ts <= 0:
                    continue
                raw_type = str(row.get("type") or "UNKNOWN").upper()
                if "TRANSFER" in raw_type:
                    category = "TRANSFER_IN" if amt >= 0 else "TRANSFER_OUT"
                elif raw_type == "SETTLEMENT":
                    category = "FUNDING_FEE"
                elif raw_type == "TRADE":
                    category = "REALIZED_PNL"
                elif raw_type == "BONUS":
                    category = "BONUS"
                else:
                    category = "UNKNOWN"
                sym = str(row.get("symbol") or "").upper() or None
                dkey = make_dedupe_key("bybit", tx_id, raw_type, ccy, sym, ts, str(amt))
                if dkey in seen:
                    continue
                seen.add(dkey)
                items.append(CashflowEntry(
                    exchange="bybit", external_id=tx_id, dedupe_key=dkey,
                    asset=ccy, raw_type=raw_type, category=category,
                    signed_amount=amt, symbol=sym, occurred_at_ms=ts,
                    source="exchange_income", metadata={"orderId": row.get("orderId")},
                ))
            cur = w_end + 1
        items.sort(key=lambda i: (i.occurred_at_ms, i.dedupe_key))
        return DatasetPage(items=items, complete=True, coverage_start_ms=start_ms, coverage_end_ms=end_ms)

    async def get_trade_fills(
        self, symbols: List[str], start_ms: int, end_ms: int, cursor: Optional[Dict[str, Any]] = None
    ) -> DatasetPage:
        items: List[TradeFill] = []
        seen: set[str] = set()
        window = 7 * 86_400_000 - 1
        cur = start_ms
        while cur <= end_ms:
            w_end = min(end_ms, cur + window)
            rows = await self._all_pages("/v5/execution/list", {
                "category": "linear", "startTime": cur, "endTime": w_end, "limit": 100,
            })
            for row in rows:
                sym = str(row.get("symbol") or "").upper()
                qty = safe_decimal(row.get("execQty"))
                px = safe_decimal(row.get("execPrice"))
                ts = int(row.get("execTime") or 0)
                if not sym or qty is None or px is None or qty <= 0 or ts <= 0:
                    continue
                fid = str(row.get("execId") or "") or make_dedupe_key(sym, row.get("orderId"), ts, str(qty), str(px))
                if fid in seen:
                    continue
                seen.add(fid)
                items.append(TradeFill(
                    exchange="bybit", exchange_fill_id=fid,
                    exchange_order_id=str(row.get("orderId") or "") or None,
                    client_order_id=str(row.get("orderLinkId") or "") or None,
                    symbol=sym, inst_id=sym,
                    side=str(row.get("side") or "").upper(),
                    position_side="BOTH",
                    qty=qty, price=px, quote_qty=safe_decimal(row.get("execValue")) or (qty * px),
                    realized_pnl=safe_decimal(row.get("execPnl")),
                    commission_amount=safe_decimal(row.get("execFee")),
                    commission_asset="USDT",
                    maker=row.get("isMaker") if isinstance(row.get("isMaker"), bool) else None,
                    occurred_at_ms=ts, metadata={"execType": row.get("execType")},
                ))
            cur = w_end + 1
        items.sort(key=lambda i: (i.occurred_at_ms, i.exchange_fill_id))
        return DatasetPage(items=items, complete=True, coverage_start_ms=start_ms, coverage_end_ms=end_ms)

    async def get_order_history(
        self, start_ms: int, end_ms: int, cursor: Optional[Dict[str, Any]] = None, symbols: Optional[List[str]] = None
    ) -> DatasetPage:
        items: List[OrderHistoryEntry] = []
        seen: set[str] = set()
        window = 7 * 86_400_000 - 1
        cur = start_ms
        while cur <= end_ms:
            w_end = min(end_ms, cur + window)
            rows = await self._all_pages("/v5/order/history", {
                "category": "linear", "startTime": cur, "endTime": w_end, "limit": 50,
            })
            for row in rows:
                oid = str(row.get("orderId") or "").strip()
                sym = str(row.get("symbol") or "").upper()
                if not oid or not sym:
                    continue
                dkey = f"order:{oid}"
                if dkey in seen:
                    continue
                seen.add(dkey)
                c_ms = int(row.get("createdTime") or row.get("updatedTime") or 0)
                u_ms = int(row.get("updatedTime") or c_ms)
                lmt = safe_decimal(row.get("price"))
                trig = safe_decimal(row.get("triggerPrice"))
                avg = safe_decimal(row.get("avgPrice"))
                items.append(OrderHistoryEntry(
                    exchange="bybit", dedupe_key=dkey, exchange_order_id=oid,
                    client_order_id=str(row.get("orderLinkId") or "") or None,
                    algo_id=None, client_algo_id=None,
                    category="conditional" if row.get("stopOrderType") else "ordinary",
                    symbol=sym, inst_id=sym,
                    side=str(row.get("side") or "").upper(),
                    position_side=self._bybit_pos_side(row.get("positionIdx")),
                    order_type=str(row.get("orderType") or "LIMIT").upper(),
                    status=str(row.get("orderStatus") or "UNKNOWN").upper(),
                    qty=safe_decimal(row.get("qty")),
                    filled_qty=safe_decimal(row.get("cumExecQty")),
                    avg_price=avg if avg and avg > 0 else None,
                    limit_price=lmt if lmt and lmt > 0 else None,
                    trigger_price=trig if trig and trig > 0 else None,
                    reduce_only=bool(row.get("reduceOnly")) if row.get("reduceOnly") is not None else None,
                    close_position=str(row.get("tpslMode") or "").lower() == "full",
                    working_type=str(row.get("triggerBy")) if row.get("triggerBy") else None,
                    created_at_ms=c_ms, updated_at_ms=u_ms, metadata={},
                ))
            cur = w_end + 1
        items.sort(key=lambda i: (i.updated_at_ms, i.dedupe_key))
        return DatasetPage(items=items, complete=True, coverage_start_ms=start_ms, coverage_end_ms=end_ms)

    async def get_commission_rates(self, symbols: List[str]) -> List[CommissionRateInfo]:
        res = await self._request("GET", "/v5/account/fee-rate", params={"category": "linear"})
        rows = res.get("list") or []
        by_sym = {str(r.get("symbol") or "").upper(): r for r in rows if isinstance(r, dict)}
        out: List[CommissionRateInfo] = []
        for sym in sorted({s.strip().upper() for s in symbols if s and s.strip()}):
            r = by_sym.get(sym) or (rows[0] if rows else {})
            if not isinstance(r, dict):
                continue
            out.append(CommissionRateInfo(
                symbol=sym, inst_id=sym,
                maker_rate=safe_decimal(r.get("makerFeeRate")),
                taker_rate=safe_decimal(r.get("takerFeeRate")),
            ))
        return out
