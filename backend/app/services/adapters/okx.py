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
        self._check_rate_limit(auth)
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
        async with self.request_client(auth) as client:
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
                        self._note_rate_limit(resp, auth)
                    # 先解析业务响应，HTML/损坏错误体仍是结果不确定。
                    payload = resp.json()
                    if not isinstance(payload, dict):
                        raise ExchangeAPIError("okx", "AMBIGUOUS", "Malformed API response")
                    code = str(payload.get("code", ""))
                    msg = payload.get("msg", "")
                    if resp.status_code != 200 or code != "0":
                        # 聚合失败码必须展开为实际订单的业务错误。
                        rows = payload.get("data")
                        if code in ("1", "2") and isinstance(rows, list) and rows and isinstance(rows[0], dict):
                            code = str(rows[0].get("sCode") or code)
                            msg = rows[0].get("sMsg") or msg
                        if code in ("50011", "50061"):
                            self._note_rate_limit(resp, auth)
                        raise api_error("okx", code, msg, payload.get("data"), resp.status_code)
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
            "totalEquity": bal.eq,
            "usdcEquity": 0.0,
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
            lever = float(p.get("lever") or 1.0)
            notional_val = abs(float(p.get("notionalUsd") or 0.0))
            raw_margin = abs(float(p.get("imr") or p.get("margin") or 0.0))
            margin_usd = raw_margin if raw_margin > 0 else ((notional_val / lever) if (notional_val > 0 and lever > 0) else 0.0)
            upl = float(p.get("upl") or 0.0)
            raw_upl_ratio = p.get("uplRatio")
            upl_ratio = float(raw_upl_ratio) if raw_upl_ratio not in (None, "") else ((upl / margin_usd) if margin_usd > 0 else 0.0)
            mgn_mode = str(p.get("mgnMode") or "cross").lower()
            liq_price = safe_optional_float(p.get("liqPx"), positive_only=True)
            maint_margin = safe_optional_float(p.get("mmr"))
            init_margin = safe_optional_float(p.get("imr") or p.get("margin"))
            break_even = safe_optional_float(p.get("bePx"), positive_only=True)
            raw_adl = str(p.get("adl") or "").strip()
            adl_q = int(raw_adl) if raw_adl.isascii() and raw_adl.isdecimal() else None
            iso_wallet = safe_optional_float(p.get("margin")) if mgn_mode == "isolated" else None
            # OKX mgnRatio is equity / mmr (higher is safer); invert to fraction mmr / equity.
            raw_mr = safe_optional_float(p.get("mgnRatio"), positive_only=True)
            pos_margin_ratio = (1.0 / raw_mr) if raw_mr else None
            if pos_margin_ratio is None and mgn_mode == "isolated" and maint_margin is not None and iso_wallet and iso_wallet > 0:
                pos_margin_ratio = maint_margin / iso_wallet
            positions.append(
                ExchangePosition(
                    inst_id=inst_id,
                    symbol=base_sym,
                    pos_side=logical_side,
                    mgn_mode=mgn_mode,
                    pos=abs(pos_sz),
                    avail_pos=abs(float(p.get("availPos") or 0.0)),
                    avg_px=float(p.get("avgPx") or 0.0),
                    cur_px=float(p.get("markPx") or p.get("last") or p.get("idxPx") or p.get("avgPx") or 0.0),
                    upl=upl,
                    upl_ratio=upl_ratio,
                    notional_usd=notional_val,
                    lever=lever,
                    c_time=int(p.get("cTime") or 0),
                    u_time=int(p.get("uTime") or 0),
                    margin_usd=margin_usd,
                    liquidation_price=liq_price,
                    maintenance_margin=maint_margin,
                    initial_margin=init_margin,
                    break_even_price=break_even,
                    adl_quantile=adl_q,
                    margin_ratio=pos_margin_ratio,
                    isolated_wallet=iso_wallet,
                )
            )
        return positions

    async def get_instruments(self, force_refresh: bool = False) -> Dict[str, InstrumentSpec]:
        cached = self._cached_instruments(force_refresh)
        if cached is not None:
            return cached
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
        return self._cache_instruments(result)

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
            raise api_error("okx", s_code, item.get("sMsg", ""), item)
        return OrderResult(
            ord_id=str(item.get("ordId") or ""),
            cl_ord_id=str(item.get("clOrdId") or cl_ord_id or ""),
            raw=item,
        )

    async def cancel_order(self, inst_id: str, cl_ord_id: str) -> None:
        """撤销一笔普通挂单；撤单结果以随后的查单为准，失败时抛 ExchangeAPIError。"""
        data = await self._request("POST", "/api/v5/trade/cancel-order",
                                   body={"instId": inst_id, "clOrdId": cl_ord_id[:32]})
        item = (data or [{}])[0]
        if str(item.get("sCode", "0")) != "0":
            raise ExchangeAPIError(self.exchange, str(item.get("sCode")), str(item.get("sMsg") or "cancel rejected"))

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
            "accFillSz": float(row["accFillSz"]) if row.get("accFillSz") not in (None, "") else None,
            "avgPx": float(row.get("avgPx") or 0.0),
            "state": row.get("state"),
            "raw": row,
        }

    async def get_open_orders(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        queries = [("/api/v5/trade/orders-pending", {}, "ordId")]
        # move_order 不是该端点接受的 ordType（OKX 实测报 51000，整个查询失败），已剔除。
        queries.extend(("/api/v5/trade/orders-algo-pending", {"ordType": kind}, "algoId")
                       for kind in ("conditional", "oco", "trigger", "iceberg", "twap"))
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
            raise api_error("okx", s_code, item.get("sMsg", "TP/SL algo order rejected"), item)
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

    @staticmethod
    def _okx_symbol(inst_id: str) -> str:
        if inst_id.endswith("-USDT-SWAP"):
            return inst_id.removesuffix("-USDT-SWAP") + "USDT"
        if inst_id.endswith("-USDC-SWAP"):
            return inst_id.removesuffix("-USDC-SWAP") + "USDC"
        return inst_id

    @staticmethod
    def _okx_pos_side(raw: Any) -> str:
        val = str(raw or "net").lower()
        if val == "long":
            return "LONG"
        if val == "short":
            return "SHORT"
        return "BOTH"

    def get_dataset_capabilities(self) -> Dict[str, DatasetCapability]:
        return {
            "equity": DatasetCapability("equity", True, True),
            "open_orders": DatasetCapability("open_orders", True, True),
            "cashflow": DatasetCapability("cashflow", True, False, retention_days=90, note="OKX bills-archive retains up to 3 months"),
            "fills": DatasetCapability("fills", True, False, retention_days=90, note="OKX fills-history retains up to 3 months"),
            "orders": DatasetCapability("orders", True, False, retention_days=90, note="OKX orders-history-archive retains up to 3 months"),
            "force_orders": DatasetCapability("force_orders", False, False, note="OKX does not expose a dedicated force-order endpoint"),
            "commission_rates": DatasetCapability("commission_rates", True, True),
            "daily_snapshots": DatasetCapability("daily_snapshots", False, False, note="Not supported on OKX"),
        }

    async def get_account_risk_snapshot(self) -> AccountRiskInfo:
        bal_list = await self._request("GET", "/api/v5/account/balance")
        cfg_list = []
        try:
            cfg_list = await self._request("GET", "/api/v5/account/config")
        except Exception:
            cfg_list = []
        bal = bal_list[0] if isinstance(bal_list, list) and bal_list else {}
        cfg = cfg_list[0] if isinstance(cfg_list, list) and cfg_list else {}
        asset_balances: Dict[str, Dict[str, Optional[Decimal]]] = {}
        usdt_det: Dict[str, Any] = {}
        for det in bal.get("details", []) if isinstance(bal, dict) else []:
            if not isinstance(det, dict):
                continue
            ccy = str(det.get("ccy") or "").upper()
            if not ccy:
                continue
            eq = safe_decimal(det.get("eq"))
            avail = safe_decimal(det.get("availEq"))
            wb = safe_decimal(det.get("cashBal"))
            upl = safe_decimal(det.get("upl"))
            im = safe_decimal(det.get("imr"))
            mm = safe_decimal(det.get("mmr"))
            if ccy == "USDT":
                usdt_det = det
            if any(v is not None and v != 0 for v in (eq, avail, wb, upl, im, mm)) or ccy == "USDT":
                asset_balances[ccy] = {
                    "equity": eq, "available": avail, "wallet": wb,
                    "upl": upl, "initialMargin": im, "maintenanceMargin": mm,
                }
        equity = safe_decimal(usdt_det.get("eq"))
        avail_bal = safe_decimal(usdt_det.get("availEq"))
        wallet_bal = safe_decimal(usdt_det.get("cashBal"))
        upl_bal = safe_decimal(usdt_det.get("upl"))
        init_margin = safe_decimal(usdt_det.get("imr"))
        maint_margin = safe_decimal(usdt_det.get("mmr"))
        margin_ratio: Optional[Decimal] = None
        source: Optional[str] = None
        if maint_margin is not None and equity is not None and equity > 0:
            margin_ratio = maint_margin / equity
            source = "derived_from_exchange_mmr"
        else:
            raw_mr = safe_decimal(usdt_det.get("mgnRatio") or (bal.get("mgnRatio") if isinstance(bal, dict) else None))
            if raw_mr is not None and raw_mr > 0:
                margin_ratio = Decimal("1") / raw_mr
                source = "derived_from_exchange_mmr"
        dual_side = True if cfg.get("posMode") == "long_short_mode" else (False if cfg.get("posMode") == "net_mode" else None)
        as_of_ms = int(bal["uTime"]) if isinstance(bal, dict) and bal.get("uTime") else None
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
            can_trade=True if cfg.get("acctLv") else None,
            multi_assets_margin=(str(cfg.get("acctLv")) in ("3", "4")) if cfg.get("acctLv") is not None else None,
            dual_side_position=dual_side,
            as_of_ms=as_of_ms,
            asset_balances=asset_balances,
        )

    async def get_open_orders_normalized(self) -> List[OpenOrderEntry]:
        raw_rows = await self.get_open_orders()
        entries: List[OpenOrderEntry] = []
        for row in raw_rows:
            if not isinstance(row, dict):
                continue
            inst_id = str(row.get("instId") or "")
            sym = self._okx_symbol(inst_id)
            side = str(row.get("side") or "").upper()
            pos_side = self._okx_pos_side(row.get("posSide"))
            close_fraction = str(row.get("closeFraction") or "").strip() == "1"
            qty = None if close_fraction else safe_decimal(row.get("sz"))
            created_ms = int(row["cTime"]) if row.get("cTime") else None
            updated_ms = int(row["uTime"]) if row.get("uTime") else created_ms
            if row.get("algoId"):
                aid = str(row["algoId"])
                ord_type = str(row.get("ordType") or "conditional").lower()
                tp_trig = safe_decimal(row.get("tpTriggerPx"))
                sl_trig = safe_decimal(row.get("slTriggerPx"))
                if tp_trig is not None and tp_trig > 0 and sl_trig is not None and sl_trig > 0:
                    tp_px = safe_decimal(row.get("tpOrdPx"))
                    sl_px = safe_decimal(row.get("slOrdPx"))
                    entries.append(OpenOrderEntry(
                        exchange="okx", order_id=f"{aid}:tp", client_order_id=row.get("algoClOrdId"),
                        algo_id=aid, category="take_profit", symbol=sym, inst_id=inst_id,
                        side=side, position_side=pos_side, order_type=ord_type.upper(),
                        status=str(row.get("state") or "live").upper(), qty=qty, filled_qty=None,
                        limit_price=tp_px if tp_px is not None and tp_px > 0 else None,
                        trigger_price=tp_trig, reduce_only=bool(row.get("reduceOnly")) if row.get("reduceOnly") is not None else True,
                        close_position=close_fraction, working_type=row.get("tpTriggerPxType"),
                        created_at_ms=created_ms, updated_at_ms=updated_ms,
                    ))
                    entries.append(OpenOrderEntry(
                        exchange="okx", order_id=f"{aid}:sl", client_order_id=row.get("algoClOrdId"),
                        algo_id=aid, category="stop_loss", symbol=sym, inst_id=inst_id,
                        side=side, position_side=pos_side, order_type=ord_type.upper(),
                        status=str(row.get("state") or "live").upper(), qty=qty, filled_qty=None,
                        limit_price=sl_px if sl_px is not None and sl_px > 0 else None,
                        trigger_price=sl_trig, reduce_only=bool(row.get("reduceOnly")) if row.get("reduceOnly") is not None else True,
                        close_position=close_fraction, working_type=row.get("slTriggerPxType"),
                        created_at_ms=created_ms, updated_at_ms=updated_ms,
                    ))
                    continue
                if tp_trig is not None and tp_trig > 0:
                    category, trig, lmt, wtype = "take_profit", tp_trig, safe_decimal(row.get("tpOrdPx")), row.get("tpTriggerPxType")
                elif sl_trig is not None and sl_trig > 0:
                    category, trig, lmt, wtype = "stop_loss", sl_trig, safe_decimal(row.get("slOrdPx")), row.get("slTriggerPxType")
                elif "move" in ord_type:
                    category, trig, lmt, wtype = "trailing_stop", safe_decimal(row.get("activePx")), None, None
                else:
                    category, trig, lmt, wtype = "conditional", safe_decimal(row.get("triggerPx")), safe_decimal(row.get("orderPx")), row.get("triggerPxType")
                if lmt is not None and lmt <= 0:
                    lmt = None
                entries.append(OpenOrderEntry(
                    exchange="okx", order_id=aid, client_order_id=row.get("algoClOrdId"),
                    algo_id=aid, category=category, symbol=sym, inst_id=inst_id,
                    side=side, position_side=pos_side, order_type=ord_type.upper(),
                    status=str(row.get("state") or "live").upper(), qty=qty, filled_qty=None,
                    limit_price=lmt, trigger_price=trig if trig and trig > 0 else None,
                    reduce_only=bool(row.get("reduceOnly")) if row.get("reduceOnly") is not None else None,
                    close_position=close_fraction, working_type=wtype,
                    created_at_ms=created_ms, updated_at_ms=updated_ms,
                ))
            else:
                oid = str(row.get("ordId") or row.get("clOrdId") or "")
                lmt = safe_decimal(row.get("px"))
                entries.append(OpenOrderEntry(
                    exchange="okx", order_id=oid, client_order_id=row.get("clOrdId"),
                    algo_id=None, category="ordinary", symbol=sym, inst_id=inst_id,
                    side=side, position_side=pos_side,
                    order_type=str(row.get("ordType") or "LIMIT").upper(),
                    status=str(row.get("state") or "live").upper(), qty=qty,
                    filled_qty=safe_decimal(row.get("accFillSz")),
                    limit_price=lmt if lmt and lmt > 0 else None, trigger_price=None,
                    reduce_only=bool(row.get("reduceOnly")) if row.get("reduceOnly") is not None else None,
                    close_position=False, working_type=None,
                    created_at_ms=created_ms, updated_at_ms=updated_ms,
                ))
        return entries

    async def get_cashflow_history(
        self, start_ms: int, end_ms: int, cursor: Optional[Dict[str, Any]] = None
    ) -> DatasetPage:
        items: List[CashflowEntry] = []
        seen: set[str] = set()
        after = None
        seen_cursors: set[str] = set()
        complete = True
        while True:
            params: Dict[str, Any] = {"instType": "SWAP", "begin": start_ms, "end": end_ms, "limit": 100}
            if after:
                params["after"] = after
            page = await self._request("GET", "/api/v5/account/bills-archive", params=params)
            if not isinstance(page, list):
                raise ValueError("Malformed OKX bills-archive response")
            for row in page:
                if not isinstance(row, dict):
                    continue
                bill_id = str(row.get("billId") or "").strip() or None
                ts = int(row.get("ts") or 0)
                ccy = str(row.get("ccy") or "").upper()
                amt = safe_decimal(row.get("balChg"))
                if not ccy or amt is None or ts <= 0:
                    continue
                raw_type = f"OKX_{row.get('type')}_{row.get('subType')}".strip("_")
                t_code = str(row.get("type") or "")
                sub_code = str(row.get("subType") or "")
                if t_code == "1":
                    category = "TRANSFER_IN" if amt >= 0 else "TRANSFER_OUT"
                elif t_code == "8":
                    category = "FUNDING_FEE"
                elif t_code == "2":
                    category = "REALIZED_PNL"
                elif t_code in ("5", "6"):
                    category = "LIQUIDATION_FEE"
                elif sub_code in ("173", "174"):
                    category = "FUNDING_FEE"
                else:
                    category = "UNKNOWN"
                inst_id = str(row.get("instId") or "") or None
                sym = self._okx_symbol(inst_id) if inst_id else None
                d_key = make_dedupe_key("okx", bill_id, raw_type, ccy, sym, ts, str(amt))
                if d_key in seen:
                    continue
                seen.add(d_key)
                items.append(CashflowEntry(
                    exchange="okx", external_id=bill_id, dedupe_key=d_key,
                    asset=ccy, raw_type=raw_type, category=category,
                    signed_amount=amt, symbol=sym, occurred_at_ms=ts,
                    source="exchange_bill", metadata={"ordId": row.get("ordId"), "subType": row.get("subType")},
                ))
            if len(page) < 100:
                break
            after = page[-1].get("billId")
            if not after or after in seen_cursors:
                complete = False
                break
            seen_cursors.add(after)
        items.sort(key=lambda i: (i.occurred_at_ms, i.dedupe_key))
        return DatasetPage(items=items, complete=complete, coverage_start_ms=start_ms, coverage_end_ms=end_ms)

    async def get_trade_fills(
        self, symbols: List[str], start_ms: int, end_ms: int, cursor: Optional[Dict[str, Any]] = None
    ) -> DatasetPage:
        items: List[TradeFill] = []
        seen: set[str] = set()
        after = None
        seen_cursors: set[str] = set()
        complete = True
        while True:
            params: Dict[str, Any] = {"instType": "SWAP", "begin": start_ms, "end": end_ms, "limit": 100}
            if after:
                params["after"] = after
            page = await self._request("GET", "/api/v5/trade/fills-history", params=params)
            if not isinstance(page, list):
                raise ValueError("Malformed OKX fills-history response")
            for row in page:
                if not isinstance(row, dict):
                    continue
                inst_id = str(row.get("instId") or "")
                sym = self._okx_symbol(inst_id)
                qty = safe_decimal(row.get("fillSz"))
                px = safe_decimal(row.get("fillPx"))
                ts = int(row.get("fillTime") or row.get("ts") or 0)
                if not inst_id or qty is None or px is None or qty <= 0 or ts <= 0:
                    continue
                fid = str(row.get("tradeId") or row.get("billId") or "") or make_dedupe_key(
                    inst_id, row.get("ordId"), ts, str(qty), str(px), row.get("side")
                )
                if fid in seen:
                    continue
                seen.add(fid)
                raw_fee = safe_decimal(row.get("fee"))
                # OKX reports fee as negative for paid fees and positive for rebates; invert to positive-cost convention.
                comm = (-raw_fee) if raw_fee is not None else None
                exec_type = str(row.get("execType") or "").upper()
                items.append(TradeFill(
                    exchange="okx", exchange_fill_id=fid,
                    exchange_order_id=str(row.get("ordId") or "") or None,
                    client_order_id=str(row.get("clOrdId") or "") or None,
                    symbol=sym, inst_id=inst_id,
                    side=str(row.get("side") or "").upper(),
                    position_side=self._okx_pos_side(row.get("posSide")),
                    qty=qty, price=px, quote_qty=qty * px,
                    realized_pnl=safe_decimal(row.get("fillPnl")),
                    commission_amount=comm,
                    commission_asset=str(row.get("feeCcy")).upper() if row.get("feeCcy") else None,
                    maker=(exec_type == "M") if exec_type in ("M", "T") else None,
                    occurred_at_ms=ts, metadata={"subType": row.get("subType")},
                ))
            if len(page) < 100:
                break
            after = page[-1].get("billId")
            if not after or after in seen_cursors:
                complete = False
                break
            seen_cursors.add(after)
        items.sort(key=lambda i: (i.occurred_at_ms, i.exchange_fill_id))
        return DatasetPage(items=items, complete=complete, coverage_start_ms=start_ms, coverage_end_ms=end_ms)

    async def get_order_history(
        self, start_ms: int, end_ms: int, cursor: Optional[Dict[str, Any]] = None, symbols: Optional[List[str]] = None
    ) -> DatasetPage:
        items: List[OrderHistoryEntry] = []
        seen: set[str] = set()
        after = None
        seen_cursors: set[str] = set()
        complete = True
        while True:
            params: Dict[str, Any] = {"instType": "SWAP", "begin": start_ms, "end": end_ms, "limit": 100}
            if after:
                params["after"] = after
            page = await self._request("GET", "/api/v5/trade/orders-history-archive", params=params)
            if not isinstance(page, list):
                raise ValueError("Malformed OKX orders-history-archive response")
            for row in page:
                if not isinstance(row, dict):
                    continue
                oid = str(row.get("ordId") or "").strip()
                inst_id = str(row.get("instId") or "")
                if not oid or not inst_id:
                    continue
                dkey = f"order:{oid}"
                if dkey in seen:
                    continue
                seen.add(dkey)
                c_ms = int(row.get("cTime") or row.get("uTime") or 0)
                u_ms = int(row.get("uTime") or c_ms)
                lmt = safe_decimal(row.get("px"))
                avg = safe_decimal(row.get("avgPx"))
                items.append(OrderHistoryEntry(
                    exchange="okx", dedupe_key=dkey, exchange_order_id=oid,
                    client_order_id=str(row.get("clOrdId") or "") or None,
                    algo_id=None, client_algo_id=None, category="ordinary",
                    symbol=self._okx_symbol(inst_id), inst_id=inst_id,
                    side=str(row.get("side") or "").upper(),
                    position_side=self._okx_pos_side(row.get("posSide")),
                    order_type=str(row.get("ordType") or "LIMIT").upper(),
                    status=str(row.get("state") or "UNKNOWN").upper(),
                    qty=safe_decimal(row.get("sz")),
                    filled_qty=safe_decimal(row.get("accFillSz")),
                    avg_price=avg if avg and avg > 0 else None,
                    limit_price=lmt if lmt and lmt > 0 else None,
                    trigger_price=None,
                    reduce_only=bool(row.get("reduceOnly")) if row.get("reduceOnly") is not None else None,
                    close_position=False, working_type=None,
                    created_at_ms=c_ms, updated_at_ms=u_ms, metadata={},
                ))
            if len(page) < 100:
                break
            after = page[-1].get("ordId")
            if not after or after in seen_cursors:
                complete = False
                break
            seen_cursors.add(after)
        items.sort(key=lambda i: (i.updated_at_ms, i.dedupe_key))
        return DatasetPage(items=items, complete=complete, coverage_start_ms=start_ms, coverage_end_ms=end_ms)

    async def get_commission_rates(self, symbols: List[str]) -> List[CommissionRateInfo]:
        data = await self._request("GET", "/api/v5/account/trade-fee", params={"instType": "SWAP"})
        row = data[0] if isinstance(data, list) and data else {}
        maker = safe_decimal(row.get("makerU") or row.get("maker"))
        taker = safe_decimal(row.get("takerU") or row.get("taker"))
        # OKX trade-fee uses negative numbers to represent fee rate deduction.
        if maker is not None:
            maker = abs(maker)
        if taker is not None:
            taker = abs(taker)
        return [
            CommissionRateInfo(symbol=self._okx_symbol(sym), inst_id=sym, maker_rate=maker, taker_rate=taker)
            for sym in sorted({s.strip().upper() for s in symbols if s and s.strip()})
        ]
