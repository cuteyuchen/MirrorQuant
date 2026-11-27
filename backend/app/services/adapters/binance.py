from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import math
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from app.services.adapters.base import (
    AccountRiskInfo,
    BalanceInfo,
    BaseExchangeAdapter,
    CashflowEntry,
    CommissionRateInfo,
    DailyAccountSnapshotEntry,
    DatasetCapability,
    DatasetPage,
    ExchangeAPIError,
    ExchangePosition,
    ForceOrderEntry,
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

logger = logging.getLogger("mirrorquant.binance")


class BinanceAdapter(BaseExchangeAdapter):
    """Async Binance USDT-M Futures REST API adapter (Live & Testnet)."""

    exchange = "binance"
    LIVE_URL = "https://fapi.binance.com"
    TESTNET_URL = "https://testnet.binancefuture.com"
    # SAPI（钱包类接口）不在 fapi 域名上：充值/提现/划转历史都在 api 域名。
    SAPI_URL = "https://api.binance.com"
    SAPI_TESTNET_URL = "https://testnet.binance.vision"

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
        self.sapi_url = self.SAPI_TESTNET_URL if environment == "demo" else self.SAPI_URL

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
        base_url: Optional[str] = None,
    ) -> Any:
        self._check_rate_limit(auth)
        method = method.upper()
        # A mutation may already have reached the exchange when its response is lost.
        attempts = max(1, retries) if method == "GET" else 1
        base_params = dict(params or {})
        host = base_url or self.base_url

        async with self.request_client(auth) as client:
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

                url = f"{host}{path}" + (f"?{qs}" if qs else "")
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
                        self._note_rate_limit(resp, auth)

                    data = resp.json()
                    # 错误体必须可解析；未知格式不能伪装成确定未受理。
                    code_value = str(data.get("code", "")) if isinstance(data, dict) else ""
                    business_error = code_value not in ("", "0", "200")
                    if not 200 <= resp.status_code < 300 or business_error:
                        code = str(data.get("code", resp.status_code)) if isinstance(data, dict) else str(resp.status_code)
                        msg = data.get("msg", resp.text[:200]) if isinstance(data, dict) else resp.text[:200]
                        if code in ("-1003", "-1015"):
                            self._note_rate_limit(resp, auth)
                        raise api_error("binance", code, msg, data, resp.status_code)
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
            "totalEquity": bal.usdt_eq,
            "usdtEquity": bal.usdt_eq,
            "usdcEquity": getattr(bal, "usdc_eq", 0.0),
            "availEquity": bal.asset_balances.get("USDT", {}).get("available", 0.0),
            "openPositions": len(positions),
            "assetBalances": getattr(bal, "asset_balances", {}),
        }

    async def get_usdt_balance(self) -> BalanceInfo:
        """Legacy portfolio display, not a contract's usable collateral."""
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

    async def get_contract_balance(self, spec: Optional[InstrumentSpec] = None) -> BalanceInfo:
        currency = spec.settle_ccy if spec is not None else "USDT"
        if currency not in ("USDT", "USDC"):
            raise ValueError("Unsupported Binance settlement currency")
        # updateTime 是余额变动时间；请求往返耗时才用于新鲜度判定。
        # /fapi/v2/account 本身带 multiAssetsMargin，不再单独调高权重的
        # /fapi/v1/multiAssetsMargin，避免每次余额检查多花一份限流额度。
        started = time.monotonic()
        data = await self._request("GET", "/fapi/v2/account")
        if not isinstance(data, dict) or data.get("multiAssetsMargin") is not False:
            raise ValueError("Binance account collateral mode is missing or contradictory")
        if not 0 <= time.monotonic() - started <= 30:
            raise ValueError("Binance collateral request is stale")
        now_ms = int(time.time() * 1000) + self._time_offset_ms

        def validate_timestamp(value: Any) -> None:
            if value is None or (type(value) is int and value == 0) or value == "0":
                return
            if type(value) is not int and not (isinstance(value, str) and value.isascii() and value.isdecimal()):
                raise ValueError("Invalid Binance collateral timestamp")
            timestamp = int(value)
            if timestamp < 0 or timestamp > now_ms:
                raise ValueError("Binance collateral timestamp is future or invalid")

        def number(item: Dict[str, Any], field: str, signed: bool = False) -> float:
            value = item.get(field)
            if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
                raise ValueError(f"Invalid Binance collateral {field}")
            if isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", value) is None:
                raise ValueError(f"Invalid Binance collateral {field}")
            try:
                decimal_value = Decimal(str(value))
                parsed = float(decimal_value)
            except (ValueError, OverflowError, InvalidOperation) as exc:
                raise ValueError(f"Invalid Binance collateral {field}") from exc
            if (not decimal_value.is_finite() or not math.isfinite(parsed)
                    or (not signed and decimal_value < 0) or (decimal_value != 0 and parsed == 0)):
                raise ValueError(f"Invalid Binance collateral {field}")
            return parsed

        validate_timestamp(data.get("updateTime"))
        assets = data.get("assets")
        if not isinstance(assets, list):
            raise ValueError("Missing Binance collateral assets")
        balances = {}
        for item in assets:
            if not isinstance(item, dict):
                raise ValueError("Invalid Binance collateral asset")
            name = item.get("asset")
            if not isinstance(name, str) or not name or name != name.strip():
                raise ValueError("Invalid Binance collateral asset name")
            name = name.upper()
            if name in balances:
                raise ValueError("Duplicate Binance collateral asset")
            if "updateTime" in item:
                validate_timestamp(item["updateTime"])
            balances[name] = {
                "equity": number(item, "marginBalance"),
                "available": number(item, "availableBalance"),
                "wallet": number(item, "walletBalance"),
                "upl": number(item, "unrealizedProfit", signed=True),
            }
        if currency not in balances:
            raise ValueError(f"Missing Binance {currency} collateral asset")
        selected = balances[currency]
        return BalanceInfo(
            eq=selected["equity"], avail_eq=selected["available"],
            cash_bal=selected["wallet"], upl=selected["upl"], currency=currency, raw=data,
            usdt_eq=balances.get("USDT", {}).get("equity", 0.0),
            usdc_eq=balances.get("USDC", {}).get("equity", 0.0), asset_balances=balances,
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
            lever = float(p.get("leverage") or 1.0)
            mgn_mode = str(p.get("marginType") or "cross").lower()
            margin_usd = float(p.get("initialMargin") or p.get("positionInitialMargin") or 0.0)
            if margin_usd <= 0 and mgn_mode == "isolated":
                margin_usd = float(p.get("isolatedWallet") or 0.0)
            if margin_usd <= 0 and notional > 0 and lever > 0:
                margin_usd = notional / lever
            liq_price = safe_optional_float(p.get("liquidationPrice"), positive_only=True)
            maint_margin = safe_optional_float(p.get("maintMargin"))
            init_margin = safe_optional_float(p.get("initialMargin") or p.get("positionInitialMargin"))
            open_order_im = safe_optional_float(p.get("openOrderInitialMargin"))
            break_even = safe_optional_float(p.get("breakEvenPrice"), positive_only=True)
            iso_wallet = safe_optional_float(p.get("isolatedWallet"), positive_only=(mgn_mode != "isolated"))
            pos_margin_ratio = safe_optional_float(p.get("marginRatio"))
            if pos_margin_ratio is None and mgn_mode == "isolated" and maint_margin is not None and iso_wallet and iso_wallet > 0:
                pos_margin_ratio = maint_margin / iso_wallet
            positions.append(
                ExchangePosition(
                    inst_id=sym,
                    symbol=sym,
                    pos_side=pos_side,
                    mgn_mode=mgn_mode,
                    pos=abs(amt),
                    avail_pos=abs(amt),
                    avg_px=entry,
                    cur_px=mark,
                    upl=upl,
                    upl_ratio=(upl / margin_usd) if margin_usd > 0 else 0.0,
                    notional_usd=notional,
                    lever=lever,
                    u_time=int(p.get("updateTime") or 0),
                    margin_usd=margin_usd,
                    liquidation_price=liq_price,
                    maintenance_margin=maint_margin,
                    initial_margin=init_margin,
                    open_order_initial_margin=open_order_im,
                    break_even_price=break_even,
                    margin_ratio=pos_margin_ratio,
                    isolated_wallet=iso_wallet,
                )
            )
        return positions

    async def get_instruments(self, force_refresh: bool = False) -> Dict[str, InstrumentSpec]:
        cached = self._cached_instruments(force_refresh)
        if cached is not None:
            return cached
        info = await self._request("GET", "/fapi/v1/exchangeInfo", auth=False)
        result: Dict[str, InstrumentSpec] = {}
        for item in info.get("symbols", []):
            if (item.get("status") != "TRADING" or item.get("quoteAsset") != "USDT"
                    or item.get("marginAsset") != "USDT"
                    or item.get("contractType") not in ("PERPETUAL", "TRADIFI_PERPETUAL")):
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
        return self._cache_instruments(result)

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
        if all(float(row.get("leverage") or 0) == lever for row in rows):
            return True  # 已是目标杠杆：不重复设置，避免无谓写操作与瞬时失败。
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
            "newOrderRespType": "RESULT",  # 尽量取得终态成交数据，仍保留查单校验。
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

    async def cancel_order(self, inst_id: str, cl_ord_id: str) -> None:
        """撤销一笔普通挂单；撤单结果以随后的查单为准，失败时抛 ExchangeAPIError。"""
        await self._request("DELETE", "/fapi/v1/order",
                            params={"symbol": inst_id, "origClientOrderId": cl_ord_id})

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
            "accFillSz": float(row["executedQty"]) if row.get("executedQty") not in (None, "") else None,
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

    async def get_futures_transfer_history(
        self, asset: str = "USDT",
        start_ms: Optional[int] = None, end_ms: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """合约钱包资金进出：用 /fapi/v1/income 的 TRANSFER 记录。

        合约权益只被这类划转改变：外部充值/提现只动现货钱包，不动合约权益，
        所以算合约账户的现金流必须用划转记录，不能用充值/提现记录。
        注意不用 SAPI /sapi/v1/futures/transfer：实测它对本账户返回
        total 0（且零记录时不带 rows 键），漏掉了收入历史里明明存在的划转；
        而收入历史的 TRANSFER 条目是合约钱包资金进出的权威记录。
        income 为有符号数：正=转入（type 1），负=转出（type 2）。
        返回归一化行：{"asset", "amount", "type", "timestamp", "tranId"}。
        密钥缺权限时交易所报 403，调用方按“流水未知”处理。
        单日划转是低频操作，一次拉 limit 1000 足够，无需分页。
        """
        params: Dict[str, Any] = {
            "incomeType": "TRANSFER",
            "startTime": start_ms, "endTime": end_ms, "limit": 1000,
        }
        data = await self._request("GET", "/fapi/v1/income", params=params)
        if not isinstance(data, list):
            raise ValueError("Incomplete Binance futures transfer history")
        rows: List[Dict[str, Any]] = []
        for entry in data:
            if not isinstance(entry, dict) or entry.get("incomeType") != "TRANSFER":
                continue
            if entry.get("asset") != asset:
                continue
            try:
                amount = float(entry["income"])
                ts = int(entry["time"])
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(amount) or amount == 0:
                continue
            kind = 1 if amount > 0 else 2
            rows.append({"asset": asset, "amount": abs(amount), "type": kind,
                         "timestamp": ts, "tranId": entry.get("tranId")})
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
            try:
                out["slOrderId"] = await _submit_cond("STOP_MARKET", sl_px)
            except Exception:
                # 只撤回本次成功提交的 TP，不撤其他策略的保护单。
                if out["tpOrderId"]:
                    try:
                        await self._request("DELETE", "/fapi/v1/algoOrder",
                            params={"symbol": inst_id, "algoId": out["tpOrderId"]})
                    except Exception as rollback_error:
                        logger.error("TP rollback failed for %s (%s)", inst_id, type(rollback_error).__name__)
                        raise RuntimeError("SL placement failed and TP rollback failed; reconcile protective orders") from rollback_error
                raise
        return out

    async def cancel_tp_sl(self, inst_id: str, pos_side: Optional[str] = None) -> bool:
        try:
            open_orders = await self._request("GET", "/fapi/v1/openAlgoOrders", params={"symbol": inst_id})
            # 缺失订单列表不是空列表，撤单结果必须可核验。
            items = open_orders.get("orders") if isinstance(open_orders, dict) else open_orders
            if not isinstance(items, list):
                raise ValueError("Incomplete Binance protective-order inventory")
            cancelled = False
            for o in items:
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
        except Exception as exc:
            # 不把撤单失败伪装成无订单，且日志不携带签名 URL。
            logger.error("Protective cancellation failed for %s (%s)", inst_id, type(exc).__name__)
            raise

    _MAX_HISTORY_WINDOW_MS = 7 * 86_400_000 - 1
    _BINANCE_ORDER_RETENTION_DAYS = 90
    _INCOME_CATEGORY_MAP = {
        "REALIZED_PNL": "REALIZED_PNL",
        "FUNDING_FEE": "FUNDING_FEE",
        "SPECIAL_FUNDING_FEE": "SPECIAL_FUNDING_FEE",
        "COMMISSION": "COMMISSION",
        "COMMISSION_REBATE": "COMMISSION_REBATE",
        "API_REBATE": "COMMISSION_REBATE",
        "LIQUIDATION_FEE": "LIQUIDATION_FEE",
        "INSURANCE_CLEAR": "INSURANCE_CLEAR",
        "WELCOME_BONUS": "BONUS",
        "CONTEST_REWARD": "BONUS",
        "BONUS": "BONUS",
        "REFERRAL_KICKBACK": "REFERRAL_REBATE",
        "REFERRAL_REBATE": "REFERRAL_REBATE",
        "DELIVERED_SETTELMENT": "OTHER_TRADING_INCOME",
        "ADL_AUTO_CLOSE": "OTHER_TRADING_INCOME",
        "AUTO_EXCHANGE": "OTHER_EXTERNAL_FLOW",
        "COIN_SWAP_DEPOSIT": "OTHER_EXTERNAL_FLOW",
        "COIN_SWAP_WITHDRAW": "OTHER_EXTERNAL_FLOW",
    }

    @classmethod
    def _classify_order_category(cls, raw_type: str, *, algo: bool = False) -> str:
        upper = (raw_type or "").upper()
        if upper in ("STOP", "STOP_MARKET"):
            return "stop_loss"
        if upper in ("TAKE_PROFIT", "TAKE_PROFIT_MARKET"):
            return "take_profit"
        if upper in ("TRAILING_STOP", "TRAILING_STOP_MARKET"):
            return "trailing_stop"
        return "conditional" if algo else "ordinary"

    @staticmethod
    def _normalize_position_side(raw_ps: Any) -> str:
        upper = str(raw_ps or "BOTH").upper()
        if upper in ("LONG", "SHORT", "BOTH"):
            return upper
        return "BOTH"

    def get_dataset_capabilities(self) -> Dict[str, DatasetCapability]:
        return {
            "equity": DatasetCapability("equity", True, True),
            "open_orders": DatasetCapability("open_orders", True, True),
            "cashflow": DatasetCapability("cashflow", True, True, retention_days=365),
            "fills": DatasetCapability("fills", True, True, retention_days=90),
            "orders": DatasetCapability(
                "orders", True, True, retention_days=self._BINANCE_ORDER_RETENTION_DAYS,
                note="Binance retains futures order history for 90 days",
            ),
            "force_orders": DatasetCapability("force_orders", True, True, retention_days=365),
            "commission_rates": DatasetCapability("commission_rates", True, True),
            "daily_snapshots": DatasetCapability(
                "daily_snapshots",
                supported=(self.environment == "live"),
                complete=False,
                retention_days=30,
                note=(
                    "SAPI accountSnapshot unavailable on Binance Futures Testnet"
                    if self.environment == "demo"
                    else "Requires SAPI wallet read permission"
                ),
            ),
        }

    async def get_adl_quantiles(self) -> Dict[tuple[str, str], int]:
        data = await self._request("GET", "/fapi/v1/adlQuantile")
        rows = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
        quantiles: Dict[tuple[str, str], int] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "").upper()
            adl = row.get("adlQuantile")
            if not sym or not isinstance(adl, dict):
                continue
            for side_key, val in adl.items():
                if isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val):
                    quantiles[(sym, str(side_key).lower())] = int(val)
        return quantiles

    async def get_account_risk_snapshot(self) -> AccountRiskInfo:
        acct = await self._request("GET", "/fapi/v2/account")
        if not isinstance(acct, dict):
            raise ValueError("Invalid Binance account risk snapshot")
        dual_side: Optional[bool] = None
        try:
            dual = await self._request("GET", "/fapi/v1/positionSide/dual")
            if isinstance(dual, dict) and isinstance(dual.get("dualSidePosition"), bool):
                dual_side = dual["dualSidePosition"]
        except Exception:
            dual_side = None

        asset_balances: Dict[str, Dict[str, Optional[Decimal]]] = {}
        raw_assets = acct.get("assets") or []
        usdt_row: Dict[str, Any] = {}
        for item in raw_assets:
            if not isinstance(item, dict):
                continue
            name = str(item.get("asset") or "").upper()
            if not name:
                continue
            mb = safe_decimal(item.get("marginBalance"))
            avail = safe_decimal(item.get("availableBalance"))
            wb = safe_decimal(item.get("walletBalance"))
            upl = safe_decimal(item.get("unrealizedProfit"))
            im = safe_decimal(item.get("initialMargin"))
            mm = safe_decimal(item.get("maintMargin"))
            if name == "USDT":
                usdt_row = item
            if any(v is not None and v != 0 for v in (mb, avail, wb, upl, im, mm)) or name == "USDT":
                asset_balances[name] = {
                    "equity": mb,
                    "available": avail,
                    "wallet": wb,
                    "upl": upl,
                    "initialMargin": im,
                    "maintenanceMargin": mm,
                }

        equity = safe_decimal(usdt_row.get("marginBalance"))
        avail_bal = safe_decimal(usdt_row.get("availableBalance"))
        wallet_bal = safe_decimal(usdt_row.get("walletBalance"))
        upl_bal = safe_decimal(usdt_row.get("unrealizedProfit"))
        init_margin = safe_decimal(usdt_row.get("initialMargin"))
        maint_margin = safe_decimal(usdt_row.get("maintMargin"))
        margin_ratio: Optional[Decimal] = None
        margin_ratio_source: Optional[str] = None
        if maint_margin is not None and equity is not None and equity > 0:
            margin_ratio = maint_margin / equity
            margin_ratio_source = "derived_from_exchange_mmr"

        as_of_ms: Optional[int] = None
        if acct.get("updateTime") not in (None, "", 0, "0"):
            try:
                as_of_ms = int(acct["updateTime"])
            except (TypeError, ValueError):
                as_of_ms = None

        return AccountRiskInfo(
            currency="USDT",
            equity=equity,
            available_balance=avail_bal,
            wallet_balance=wallet_bal,
            unrealized_pnl=upl_bal,
            initial_margin=init_margin,
            maintenance_margin=maint_margin,
            margin_ratio=margin_ratio,
            margin_ratio_source=margin_ratio_source,
            can_trade=acct.get("canTrade") if isinstance(acct.get("canTrade"), bool) else None,
            can_deposit=acct.get("canDeposit") if isinstance(acct.get("canDeposit"), bool) else None,
            can_withdraw=acct.get("canWithdraw") if isinstance(acct.get("canWithdraw"), bool) else None,
            multi_assets_margin=(
                acct.get("multiAssetsMargin") if isinstance(acct.get("multiAssetsMargin"), bool) else None
            ),
            dual_side_position=dual_side,
            as_of_ms=as_of_ms,
            asset_balances=asset_balances,
        )

    async def _fetch_time_windows(
        self,
        path: str,
        start_ms: int,
        end_ms: int,
        *,
        limit: int,
        time_field: str = "time",
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> tuple[List[Dict[str, Any]], bool]:
        """Fetch time-windowed Binance endpoints without truncating full pages."""
        if end_ms < start_ms:
            return [], True
        windows: List[tuple[int, int]] = []
        cur = end_ms
        while cur >= start_ms:
            w_start = max(start_ms, cur - self._MAX_HISTORY_WINDOW_MS)
            windows.append((w_start, cur))
            cur = w_start - 1
        windows.reverse()

        collected: List[Dict[str, Any]] = []
        complete = True
        stack = list(reversed(windows))
        while stack:
            w_start, w_end = stack.pop()
            params = {**(extra_params or {}), "startTime": w_start, "endTime": w_end, "limit": limit}
            rows = await self._request("GET", path, params=params)
            if not isinstance(rows, list):
                raise ValueError(f"Malformed Binance response for {path}")
            if len(rows) < limit:
                collected.extend(r for r in rows if isinstance(r, dict))
                continue
            times = []
            for r in rows:
                if isinstance(r, dict) and r.get(time_field) is not None:
                    try:
                        times.append(int(r[time_field]))
                    except (TypeError, ValueError):
                        pass
            if w_end <= w_start:
                collected.extend(r for r in rows if isinstance(r, dict))
                complete = False
                continue
            max_t = max(times) if times else w_start
            min_t = min(times) if times else w_start
            if min_t < max_t < w_end:
                collected.extend(r for r in rows if isinstance(r, dict) and int(r.get(time_field) or 0) < max_t)
                stack.append((max_t, w_end))
            else:
                mid = (w_start + w_end) // 2
                stack.append((mid + 1, w_end))
                stack.append((w_start, mid))
        return collected, complete

    async def get_cashflow_history(
        self, start_ms: int, end_ms: int, cursor: Optional[Dict[str, Any]] = None
    ) -> DatasetPage:
        raw_rows, complete = await self._fetch_time_windows(
            "/fapi/v1/income", start_ms, end_ms, limit=1000, time_field="time"
        )
        entries: List[CashflowEntry] = []
        seen: set[tuple[Any, ...]] = set()
        for entry in raw_rows:
            raw_type = str(entry.get("incomeType") or "UNKNOWN").strip().upper() or "UNKNOWN"
            asset = str(entry.get("asset") or "").strip().upper()
            signed = safe_decimal(entry.get("income"))
            try:
                ts = int(entry["time"])
            except (KeyError, TypeError, ValueError):
                continue
            if not asset or signed is None:
                continue
            if raw_type in ("TRANSFER", "INTERNAL_TRANSFER", "CROSS_COLLATERAL_TRANSFER"):
                category = "TRANSFER_IN" if signed >= 0 else "TRANSFER_OUT"
            else:
                category = self._INCOME_CATEGORY_MAP.get(raw_type, "UNKNOWN")
            tran_id = entry.get("tranId")
            trade_id = entry.get("tradeId")
            tran_str = str(tran_id).strip() if tran_id not in (None, "") else ""
            trade_str = str(trade_id).strip() if trade_id not in (None, "") else ""
            external_id = f"{tran_str}:{trade_str}" if (tran_str and trade_str) else (tran_str or None)
            symbol = str(entry.get("symbol") or "").strip().upper() or None
            info_text = str(entry.get("info") or "")[:120]
            dedupe_key = make_dedupe_key(
                "binance", external_id, raw_type, asset, symbol, ts, str(signed), info_text
            )
            uniq = (external_id, raw_type) if external_id is not None else ("dedupe", dedupe_key)
            if uniq in seen:
                continue
            seen.add(uniq)
            entries.append(
                CashflowEntry(
                    exchange="binance",
                    external_id=external_id,
                    dedupe_key=dedupe_key,
                    asset=asset,
                    raw_type=raw_type,
                    category=category,
                    signed_amount=signed,
                    symbol=symbol,
                    occurred_at_ms=ts,
                    source="exchange_income",
                    metadata={
                        "info": info_text,
                        "tranId": tran_str or None,
                        "tradeId": trade_str or None,
                    },
                )
            )
        entries.sort(key=lambda item: (item.occurred_at_ms, item.dedupe_key))
        return DatasetPage(
            items=entries,
            complete=complete,
            coverage_start_ms=start_ms,
            coverage_end_ms=end_ms,
        )

    async def get_trade_fills(
        self, symbols: List[str], start_ms: int, end_ms: int, cursor: Optional[Dict[str, Any]] = None
    ) -> DatasetPage:
        clean_symbols = sorted({s.strip().upper() for s in symbols if s and s.strip()})
        if not clean_symbols or end_ms < start_ms:
            return DatasetPage(items=[], complete=True, coverage_start_ms=start_ms, coverage_end_ms=end_ms)
        fills: List[TradeFill] = []
        seen_ids: set[str] = set()
        complete = True

        for sym in clean_symbols:
            cur = start_ms
            while cur <= end_ms:
                w_end = min(end_ms, cur + self._MAX_HISTORY_WINDOW_MS)
                rows = await self._request(
                    "GET",
                    "/fapi/v1/userTrades",
                    params={"symbol": sym, "startTime": cur, "endTime": w_end, "limit": 1000},
                )
                if not isinstance(rows, list):
                    raise ValueError(f"Malformed Binance userTrades response for {sym}")
                batch = [r for r in rows if isinstance(r, dict)]
                while True:
                    for r in batch:
                        try:
                            ts = int(r["time"])
                        except (KeyError, TypeError, ValueError):
                            continue
                        if ts < cur or ts > w_end:
                            continue
                        qty = safe_decimal(r.get("qty"))
                        price = safe_decimal(r.get("price"))
                        if qty is None or price is None or qty <= 0:
                            continue
                        raw_fid = r.get("id")
                        fill_id = (
                            str(raw_fid)
                            if raw_fid not in (None, "")
                            else make_dedupe_key(sym, r.get("orderId"), ts, str(qty), str(price), r.get("side"))
                        )
                        if fill_id in seen_ids:
                            continue
                        seen_ids.add(fill_id)
                        side = str(r.get("side") or "").upper()
                        if side not in ("BUY", "SELL"):
                            side = "BUY" if r.get("buyer") else "SELL"
                        comm = safe_decimal(r.get("commission"))
                        quote_qty = safe_decimal(r.get("quoteQty"))
                        if quote_qty is None:
                            quote_qty = qty * price
                        fills.append(
                            TradeFill(
                                exchange="binance",
                                exchange_fill_id=fill_id,
                                exchange_order_id=str(r["orderId"]) if r.get("orderId") not in (None, "") else None,
                                client_order_id=None,
                                symbol=sym,
                                inst_id=sym,
                                side=side,
                                position_side=self._normalize_position_side(r.get("positionSide")),
                                qty=qty,
                                price=price,
                                quote_qty=quote_qty,
                                realized_pnl=safe_decimal(r.get("realizedPnl")),
                                commission_amount=comm,
                                commission_asset=(
                                    str(r.get("commissionAsset")).upper()
                                    if r.get("commissionAsset")
                                    else None
                                ),
                                maker=r.get("maker") if isinstance(r.get("maker"), bool) else None,
                                occurred_at_ms=ts,
                                metadata={"buyer": r.get("buyer"), "marginAsset": r.get("marginAsset")},
                            )
                        )
                    if len(batch) < 1000:
                        break
                    ids = [int(r["id"]) for r in batch if r.get("id") is not None]
                    if not ids:
                        complete = False
                        break
                    next_from_id = max(ids) + 1
                    next_rows = await self._request(
                        "GET",
                        "/fapi/v1/userTrades",
                        params={"symbol": sym, "fromId": next_from_id, "limit": 1000},
                    )
                    if not isinstance(next_rows, list):
                        raise ValueError(f"Malformed Binance userTrades pagination for {sym}")
                    batch = [r for r in next_rows if isinstance(r, dict)]
                    if not batch:
                        break
                    if all(int(r.get("time") or 0) > w_end for r in batch):
                        break
                cur = w_end + 1

        fills.sort(key=lambda item: (item.occurred_at_ms, item.exchange_fill_id))
        return DatasetPage(
            items=fills,
            complete=complete,
            coverage_start_ms=start_ms,
            coverage_end_ms=end_ms,
        )

    async def get_order_history(
        self,
        start_ms: int,
        end_ms: int,
        cursor: Optional[Dict[str, Any]] = None,
        symbols: Optional[List[str]] = None,
    ) -> DatasetPage:
        if end_ms < start_ms:
            return DatasetPage(items=[], complete=True, coverage_start_ms=start_ms, coverage_end_ms=end_ms)
        now_ms = int(time.time() * 1000) + self._time_offset_ms
        retention_floor = now_ms - self._BINANCE_ORDER_RETENTION_DAYS * 86_400_000
        effective_start = max(start_ms, retention_floor)
        clean_symbols = sorted({s.strip().upper() for s in (symbols or []) if s and s.strip()})
        entries: List[OrderHistoryEntry] = []
        seen_keys: set[str] = set()
        complete = effective_start == start_ms

        for sym in clean_symbols:
            cur = effective_start
            while cur <= end_ms:
                w_end = min(end_ms, cur + self._MAX_HISTORY_WINDOW_MS)
                rows = await self._request(
                    "GET",
                    "/fapi/v1/allOrders",
                    params={"symbol": sym, "startTime": cur, "endTime": w_end, "limit": 1000},
                )
                if not isinstance(rows, list):
                    raise ValueError(f"Malformed Binance allOrders response for {sym}")
                batch = [r for r in rows if isinstance(r, dict)]
                while True:
                    for r in batch:
                        created_ms = int(r.get("time") or r.get("updateTime") or 0)
                        updated_ms = int(r.get("updateTime") or r.get("time") or 0)
                        if created_ms and (created_ms < cur or created_ms > w_end):
                            continue
                        oid = str(r.get("orderId") or "").strip() or None
                        if not oid:
                            continue
                        dedupe_key = f"order:{oid}"
                        if dedupe_key in seen_keys:
                            continue
                        seen_keys.add(dedupe_key)
                        raw_type = str(r.get("type") or r.get("origType") or "LIMIT").upper()
                        limit_px = safe_decimal(r.get("price"))
                        if limit_px is not None and limit_px <= 0:
                            limit_px = None
                        trig_px = safe_decimal(r.get("stopPrice"))
                        if trig_px is not None and trig_px <= 0:
                            trig_px = None
                        avg_px = safe_decimal(r.get("avgPrice"))
                        if avg_px is not None and avg_px <= 0:
                            avg_px = None
                        close_pos = bool(r.get("closePosition")) if r.get("closePosition") is not None else None
                        qty = None if close_pos else safe_decimal(r.get("origQty"))
                        entries.append(
                            OrderHistoryEntry(
                                exchange="binance",
                                dedupe_key=dedupe_key,
                                exchange_order_id=oid,
                                client_order_id=str(r.get("clientOrderId") or "").strip() or None,
                                algo_id=None,
                                client_algo_id=None,
                                category=self._classify_order_category(raw_type, algo=False),
                                symbol=sym,
                                inst_id=sym,
                                side=str(r.get("side") or "").upper(),
                                position_side=self._normalize_position_side(r.get("positionSide")),
                                order_type=raw_type,
                                status=str(r.get("status") or "UNKNOWN").upper(),
                                qty=qty,
                                filled_qty=safe_decimal(r.get("executedQty")),
                                avg_price=avg_px,
                                limit_price=limit_px,
                                trigger_price=trig_px,
                                reduce_only=bool(r.get("reduceOnly")) if r.get("reduceOnly") is not None else None,
                                close_position=close_pos,
                                working_type=str(r.get("workingType")) if r.get("workingType") else None,
                                created_at_ms=created_ms,
                                updated_at_ms=updated_ms or created_ms,
                                metadata={"timeInForce": r.get("timeInForce"), "origType": r.get("origType")},
                            )
                        )
                    if len(batch) < 1000:
                        break
                    oids = [int(r["orderId"]) for r in batch if r.get("orderId") is not None]
                    if not oids:
                        complete = False
                        break
                    next_oid = max(oids) + 1
                    next_rows = await self._request(
                        "GET",
                        "/fapi/v1/allOrders",
                        params={"symbol": sym, "orderId": next_oid, "limit": 1000},
                    )
                    if not isinstance(next_rows, list):
                        raise ValueError(f"Malformed Binance allOrders pagination for {sym}")
                    batch = [r for r in next_rows if isinstance(r, dict)]
                    if not batch:
                        break
                    if all(int(r.get("time") or r.get("updateTime") or 0) > w_end for r in batch):
                        break
                cur = w_end + 1

        # Conditional / algo order history
        for sym in clean_symbols:
            cur = effective_start
            while cur <= end_ms:
                w_end = min(end_ms, cur + self._MAX_HISTORY_WINDOW_MS)
                page = 1
                seen_algo_in_win: set[str] = set()
                while True:
                    resp = await self._request(
                        "GET",
                        "/fapi/v1/allAlgoOrders",
                        params={"symbol": sym, "startTime": cur, "endTime": w_end, "page": page, "pageSize": 100},
                    )
                    algo_rows = resp.get("orders") if isinstance(resp, dict) else resp
                    if not isinstance(algo_rows, list):
                        raise ValueError("Malformed Binance allAlgoOrders response")
                    added_in_page = 0
                    for r in algo_rows:
                        if not isinstance(r, dict):
                            continue
                        aid = str(r.get("algoId") or "").strip()
                        if not aid or aid in seen_algo_in_win:
                            continue
                        seen_algo_in_win.add(aid)
                        added_in_page += 1
                        dedupe_key = f"algo:{aid}"
                        if dedupe_key in seen_keys:
                            continue
                        seen_keys.add(dedupe_key)
                        sym = str(r.get("symbol") or "").upper()
                        raw_type = str(r.get("orderType") or r.get("type") or r.get("algoType") or "CONDITIONAL").upper()
                        created_ms = int(r.get("createTime") or r.get("time") or r.get("updateTime") or 0)
                        updated_ms = int(r.get("updateTime") or r.get("triggerTime") or created_ms)
                        close_pos = bool(r.get("closePosition")) if r.get("closePosition") is not None else None
                        qty = None if close_pos else safe_decimal(r.get("quantity"))
                        trig_px = safe_decimal(r.get("triggerPrice"))
                        if trig_px is not None and trig_px <= 0:
                            trig_px = None
                        limit_px = safe_decimal(r.get("price"))
                        if limit_px is not None and limit_px <= 0:
                            limit_px = None
                        entries.append(
                            OrderHistoryEntry(
                                exchange="binance",
                                dedupe_key=dedupe_key,
                                exchange_order_id=None,
                                client_order_id=None,
                                algo_id=aid,
                                client_algo_id=str(r.get("clientAlgoId") or "").strip() or None,
                                category=self._classify_order_category(raw_type, algo=True),
                                symbol=sym,
                                inst_id=sym,
                                side=str(r.get("side") or "").upper(),
                                position_side=self._normalize_position_side(r.get("positionSide")),
                                order_type=raw_type,
                                status=str(r.get("algoStatus") or r.get("status") or "UNKNOWN").upper(),
                                qty=qty,
                                filled_qty=safe_decimal(r.get("executedQty")),
                                avg_price=safe_decimal(r.get("avgPrice")),
                                limit_price=limit_px,
                                trigger_price=trig_px,
                                reduce_only=bool(r.get("reduceOnly")) if r.get("reduceOnly") is not None else None,
                                close_position=close_pos,
                                working_type=str(r.get("workingType")) if r.get("workingType") else None,
                                created_at_ms=created_ms,
                                updated_at_ms=updated_ms or created_ms,
                                metadata={
                                    "actualOrderId": str(r.get("actualOrderId")) if r.get("actualOrderId") else None,
                                    "algoType": r.get("algoType"),
                                },
                            )
                        )
                    if len(algo_rows) < 100 or added_in_page == 0:
                        break
                    page += 1
                cur = w_end + 1

        entries.sort(key=lambda item: (item.updated_at_ms, item.dedupe_key))
        return DatasetPage(
            items=entries,
            complete=complete,
            coverage_start_ms=effective_start,
            coverage_end_ms=end_ms,
        )

    async def get_open_orders_normalized(self) -> List[OpenOrderEntry]:
        ordinary = await self._request("GET", "/fapi/v1/openOrders")
        conditional = await self._request("GET", "/fapi/v1/openAlgoOrders")
        if isinstance(conditional, dict):
            conditional = conditional.get("orders")
        if not isinstance(ordinary, list) or not isinstance(conditional, list):
            raise ValueError("Incomplete Binance pending-order inventory")

        entries: List[OpenOrderEntry] = []
        for o in ordinary:
            if not isinstance(o, dict):
                continue
            sym = str(o.get("symbol") or "").upper()
            raw_type = str(o.get("type") or o.get("origType") or "LIMIT").upper()
            close_pos = bool(o.get("closePosition")) if o.get("closePosition") is not None else False
            qty = None if close_pos else safe_decimal(o.get("origQty"))
            limit_px = safe_decimal(o.get("price"))
            if limit_px is not None and limit_px <= 0:
                limit_px = None
            trig_px = safe_decimal(o.get("stopPrice"))
            if trig_px is not None and trig_px <= 0:
                trig_px = None
            entries.append(
                OpenOrderEntry(
                    exchange="binance",
                    order_id=str(o.get("orderId") or o.get("clientOrderId") or ""),
                    client_order_id=str(o.get("clientOrderId") or "").strip() or None,
                    algo_id=None,
                    category=self._classify_order_category(raw_type, algo=False),
                    symbol=sym,
                    inst_id=sym,
                    side=str(o.get("side") or "").upper(),
                    position_side=self._normalize_position_side(o.get("positionSide")),
                    order_type=raw_type,
                    status=str(o.get("status") or "NEW").upper(),
                    qty=qty,
                    filled_qty=safe_decimal(o.get("executedQty")),
                    limit_price=limit_px,
                    trigger_price=trig_px,
                    reduce_only=bool(o.get("reduceOnly")) if o.get("reduceOnly") is not None else None,
                    close_position=close_pos,
                    working_type=str(o.get("workingType")) if o.get("workingType") else None,
                    created_at_ms=int(o["time"]) if o.get("time") else None,
                    updated_at_ms=int(o["updateTime"]) if o.get("updateTime") else None,
                )
            )
        for o in conditional:
            if not isinstance(o, dict):
                continue
            sym = str(o.get("symbol") or "").upper()
            raw_type = str(o.get("orderType") or o.get("type") or o.get("algoType") or "CONDITIONAL").upper()
            close_pos = bool(o.get("closePosition")) if o.get("closePosition") is not None else False
            qty = None if close_pos else safe_decimal(o.get("quantity"))
            if qty is not None and qty <= 0:
                qty = None
            trig_px = safe_decimal(o.get("triggerPrice"))
            if trig_px is not None and trig_px <= 0:
                trig_px = None
            limit_px = safe_decimal(o.get("price"))
            if limit_px is not None and limit_px <= 0:
                limit_px = None
            aid = str(o.get("algoId") or "")
            entries.append(
                OpenOrderEntry(
                    exchange="binance",
                    order_id=aid or str(o.get("clientAlgoId") or ""),
                    client_order_id=str(o.get("clientAlgoId") or "").strip() or None,
                    algo_id=aid or None,
                    category=self._classify_order_category(raw_type, algo=True),
                    symbol=sym,
                    inst_id=sym,
                    side=str(o.get("side") or "").upper(),
                    position_side=self._normalize_position_side(o.get("positionSide")),
                    order_type=raw_type,
                    status=str(o.get("algoStatus") or o.get("status") or "NEW").upper(),
                    qty=qty,
                    filled_qty=safe_decimal(o.get("executedQty")),
                    limit_price=limit_px,
                    trigger_price=trig_px,
                    reduce_only=bool(o.get("reduceOnly")) if o.get("reduceOnly") is not None else None,
                    close_position=close_pos,
                    working_type=str(o.get("workingType")) if o.get("workingType") else None,
                    created_at_ms=int(o["createTime"]) if o.get("createTime") else None,
                    updated_at_ms=int(o["updateTime"]) if o.get("updateTime") else None,
                )
            )
        return entries

    async def get_force_orders(self, start_ms: int, end_ms: int) -> DatasetPage:
        raw_rows, complete = await self._fetch_time_windows(
            "/fapi/v1/forceOrders", start_ms, end_ms, limit=100, time_field="time"
        )
        items: List[ForceOrderEntry] = []
        seen: set[str] = set()
        for r in raw_rows:
            sym = str(r.get("symbol") or "").upper()
            ts = int(r.get("time") or r.get("updateTime") or 0)
            if not sym or ts <= 0:
                continue
            oid = str(r.get("orderId") or "").strip() or None
            qty = safe_decimal(r.get("origQty"))
            filled = safe_decimal(r.get("executedQty"))
            avg_px = safe_decimal(r.get("averagePrice") or r.get("avgPrice") or r.get("price"))
            side = str(r.get("side") or "").upper()
            pos_side = self._normalize_position_side(r.get("positionSide"))
            dedupe_key = (
                f"force:{oid}"
                if oid
                else f"force:{make_dedupe_key('binance', sym, side, pos_side, ts, str(qty), str(avg_px))}"
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            items.append(
                ForceOrderEntry(
                    exchange="binance",
                    dedupe_key=dedupe_key,
                    exchange_order_id=oid,
                    symbol=sym,
                    inst_id=sym,
                    side=side,
                    position_side=pos_side,
                    order_type=str(r.get("origType") or r.get("type") or "LIQUIDATION").upper(),
                    status=str(r.get("status") or "FILLED").upper(),
                    qty=qty,
                    filled_qty=filled,
                    avg_price=avg_px,
                    occurred_at_ms=ts,
                    metadata={"timeInForce": r.get("timeInForce")},
                )
            )
        items.sort(key=lambda item: (item.occurred_at_ms, item.dedupe_key))
        return DatasetPage(
            items=items,
            complete=complete,
            coverage_start_ms=start_ms,
            coverage_end_ms=end_ms,
        )

    async def get_commission_rates(self, symbols: List[str]) -> List[CommissionRateInfo]:
        rates: List[CommissionRateInfo] = []
        for sym in sorted({s.strip().upper() for s in symbols if s and s.strip()}):
            data = await self._request("GET", "/fapi/v1/commissionRate", params={"symbol": sym})
            if not isinstance(data, dict):
                continue
            rates.append(
                CommissionRateInfo(
                    symbol=sym,
                    inst_id=sym,
                    maker_rate=safe_decimal(data.get("makerCommissionRate")),
                    taker_rate=safe_decimal(data.get("takerCommissionRate")),
                )
            )
        return rates

    async def get_daily_account_snapshots(self, start_ms: int, end_ms: int) -> DatasetPage:
        if self.environment == "demo":
            return DatasetPage(
                items=[],
                complete=False,
                supported=False,
                error_code="UNSUPPORTED_ON_TESTNET",
                error_message="SAPI accountSnapshot unavailable on testnet",
            )
        try:
            resp = await self._request(
                "GET",
                "/sapi/v1/accountSnapshot",
                params={"type": "FUTURES", "startTime": start_ms, "endTime": end_ms, "limit": 30},
                base_url=self.sapi_url,
            )
        except ExchangeAPIError as exc:
            if str(exc.code) in ("-1003", "-1015", "429", "RATE_LIMITED"):
                raise
            return DatasetPage(
                items=[],
                complete=False,
                supported=False,
                error_code=str(exc.code),
                error_message=str(exc.msg),
            )
        vos = resp.get("snapshotVos") if isinstance(resp, dict) else None
        if not isinstance(vos, list):
            return DatasetPage(items=[], complete=False, supported=False, error_code="INVALID_SAPI_RESPONSE")
        items: List[DailyAccountSnapshotEntry] = []
        for vo in vos:
            if not isinstance(vo, dict):
                continue
            ts = int(vo.get("updateTime") or 0)
            data = vo.get("data") if isinstance(vo.get("data"), dict) else {}
            assets = data.get("assets") if isinstance(data.get("assets"), list) else []
            for asset_row in assets:
                if not isinstance(asset_row, dict):
                    continue
                ccy = str(asset_row.get("asset") or "").upper()
                if ccy not in ("USDT", "USDC"):
                    continue
                eq = safe_decimal(asset_row.get("marginBalance"))
                wb = safe_decimal(asset_row.get("walletBalance"))
                upl = safe_decimal(asset_row.get("unrealizedProfit"))
                if eq is None:
                    continue
                if ccy == "USDC" and eq == 0 and (wb is None or wb == 0):
                    continue
                items.append(
                    DailyAccountSnapshotEntry(
                        currency=ccy,
                        day_start_ms=ts,
                        captured_at_ms=ts,
                        equity=eq,
                        wallet_balance=wb,
                        unrealized_pnl=upl,
                    )
                )
        return DatasetPage(
            items=items,
            complete=False,
            coverage_start_ms=start_ms,
            coverage_end_ms=end_ms,
            supported=True,
        )
