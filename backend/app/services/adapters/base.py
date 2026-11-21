from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from email.utils import parsedate_to_datetime
import math
import hashlib
import random
import time
from typing import Any, ClassVar, Dict, List, Optional

from app.services.proxy.manager import ProxyManager


class ExchangeAPIError(RuntimeError):
    def __init__(self, exchange: str, code: str, msg: str, data: Any = None):
        super().__init__(f"{exchange.upper()} API Error [{code}]: {msg}")
        self.exchange = exchange
        self.code = code
        self.msg = msg
        self.data = data


class OrderRejected(ExchangeAPIError):
    """确定未被受理的订单；不需要创建未知结果隔离。"""


def api_error(exchange: str, code: str, msg: str, data: Any = None, http_status: int = 200) -> ExchangeAPIError:
    # 只确认已知拒绝码；未识别数字码和重复客户单号也必须先查单。
    # 下表已于 2026-11-15 逐码对照各交易所官方错误码文档；新增条目前必须先核对文档，
    # 只收录“请求确定未被受理为新订单”的码。查单/撤单类（订单不存在）与文档查不到的码一律不收录。
    rejected_codes = {
        # 币安 U 本位合约：-2011/-2013 属撤单、查单语义，-4112 文档无此码，均已移除。
        "binance": {
            "-1002", "-1003", "-1013", "-1014", "-1015", "-1020", "-1021", "-1022",
            "-1100", "-1101", "-1102", "-1103", "-1104", "-1105", "-1106", "-1111",
            "-1114", "-1115", "-1116", "-1117", "-1118", "-1119", "-1121", "-1128", "-1130",
            "-2010", "-2014", "-2015", "-2018", "-2019", "-2020",
            "-2021", "-2022", "-2024", "-2025", "-2026", "-2027", "-2028",
            "-4001", "-4002", "-4003", "-4004", "-4005", "-4013", "-4014", "-4015",
            "-4016", "-4023", "-4024", "-4028", "-4045", "-4050", "-4051", "-4060",
            "-4061", "-4062", "-4131", "-4164",
        },
        # OKX：51011/51016 指向既有订单身份，不能仅凭错误文案证明未受理；
        # 51004/51119 文档查不到、51603 是“订单不存在”，均已移除。
        "okx": {
            "50011", "50061", "50101", "50102", "50103", "50104", "50105", "50106",
            "50107", "50108", "50109", "50110", "50111", "50112", "50113", "50114",
            "50115", "51000", "51001", "51005", "51006", "51007", "51008",
            "51009", "51010", "51015", "51020", "51100", "51101", "51102",
            "51103", "51104", "51105", "51112", "51120", "51121", "51122",
            "51123", "51131", "51201", "51202", "51203", "51204", "51205",
        },
        # Bybit（实验性）：110001/170213 是“订单不存在”，已移除。
        "bybit": {
            "10001", "10002", "10003", "10004", "10005", "10006", "10007", "10008",
            "10009", "10010", "10018", "10024", "10027", "110003", "110004",
            "110006", "110007", "110012", "110013", "110017", "110020", "110021",
            "110032", "110043",
        },
        # Bitget（实验性）：官方错误码页无法取得、未能核实，故不收录任何码，一律按结果未知查单。
        "bitget": set(),
    }
    code = str(code)
    uncertain = (http_status >= 500 or http_status == 408 or 300 <= http_status < 400
                 or code not in rejected_codes.get(exchange, set())
                 or "duplicate" in str(msg).lower())
    error_type = ExchangeAPIError if uncertain else OrderRejected
    return error_type(exchange, str(code), msg, data)


@dataclass
class BalanceInfo:
    eq: float
    avail_eq: float
    cash_bal: float
    upl: float
    currency: str = "USDT"
    raw: Dict[str, Any] = field(default_factory=dict)
    usdt_eq: float = 0.0
    usdc_eq: float = 0.0
    asset_balances: Dict[str, Dict[str, float]] = field(default_factory=dict)


async def get_collateral_balance(adapter: Any, spec: Optional[InstrumentSpec] = None) -> BalanceInfo:
    """Use contract collateral when supported; legacy balances are USDT-only."""
    currency = spec.settle_ccy if spec is not None else "USDT"
    reader = getattr(adapter, "get_contract_balance", None)
    if callable(reader):
        balance = await reader(spec)
    else:
        if currency != "USDT":
            raise ValueError("Legacy adapter cannot confirm non-USDT collateral")
        balance = await adapter.get_usdt_balance()
    if balance.currency != currency:
        raise ValueError("Collateral balance currency does not match contract settlement")
    for name in ("eq", "avail_eq", "cash_bal", "upl"):
        value = getattr(balance, name)
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)) or not math.isfinite(value):
            raise ValueError(f"Invalid collateral {name}")
        if name != "upl" and value < 0:
            raise ValueError(f"Negative collateral {name}")
    return balance


@dataclass
class ExchangePosition:
    inst_id: str
    symbol: str
    pos_side: str  # "long" | "short" | "net"
    mgn_mode: str  # "cross" | "isolated"
    pos: float
    avail_pos: float
    avg_px: float
    upl: float
    upl_ratio: float
    notional_usd: float
    lever: float
    cur_px: float = 0.0
    c_time: int = 0
    u_time: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "instId": self.inst_id,
            "symbol": self.symbol,
            "posSide": self.pos_side,
            "mgnMode": self.mgn_mode,
            "pos": self.pos,
            "availPos": self.avail_pos,
            "avgPx": self.avg_px,
            "curPx": self.cur_px,
            "markPx": self.cur_px,
            "currentPrice": self.cur_px,
            "markPrice": self.cur_px,
            "upl": self.upl,
            "uplRatio": self.upl_ratio,
            "notionalUsd": self.notional_usd,
            "lever": self.lever,
            "cTime": self.c_time,
            "uTime": self.u_time,
        }


@dataclass
class InstrumentSpec:
    inst_id: str
    ct_val: Decimal
    min_sz: Decimal
    lot_sz: Decimal
    tick_sz: Decimal
    max_lvr: float = 20.0
    min_notional: Decimal = Decimal("0")
    settle_ccy: str = "USDT"
    base_ccy: str = ""
    quote_ccy: str = "USDT"
    contract_type: str = "linear"
    max_sz: Optional[Decimal] = None
    market_min_sz: Optional[Decimal] = None
    market_lot_sz: Optional[Decimal] = None
    market_max_sz: Optional[Decimal] = None

    def __post_init__(self) -> None:
        for name in ("ct_val", "min_sz", "lot_sz", "tick_sz", "max_sz",
                     "market_min_sz", "market_lot_sz", "market_max_sz"):
            value = getattr(self, name)
            if value is not None and (not Decimal(str(value)).is_finite() or value <= 0):
                raise ValueError(f"Invalid {name} for {self.inst_id}")
        if not Decimal(str(self.min_notional)).is_finite() or self.min_notional < 0:
            raise ValueError(f"Invalid minimum notional for {self.inst_id}")
        if not Decimal(str(self.max_lvr)).is_finite() or self.max_lvr < 1:
            raise ValueError(f"Invalid leverage for {self.inst_id}")

    def validate_order(self, ord_type: str, quantity: Any, price: Any = None,
                       reduce_only: bool = False) -> None:
        qty = Decimal(str(quantity))
        market = ord_type.lower() == "market"
        minimum = self.market_min_sz if market and self.market_min_sz is not None else self.min_sz
        step = self.market_lot_sz if market and self.market_lot_sz is not None else self.lot_sz
        maximum = self.market_max_sz if market else self.max_sz
        if (not qty.is_finite() or qty <= 0 or qty < minimum or qty % step != 0
                or (maximum is not None and qty > maximum)):
            raise ValueError(f"Order quantity fails {self.inst_id} {ord_type} filters")
        if price is not None:
            px = Decimal(str(price))
            if not px.is_finite() or px <= 0 or (not market and px % self.tick_sz != 0):
                raise ValueError(f"Order price fails {self.inst_id} filters")
            if not reduce_only and qty * px * self.ct_val < self.min_notional:
                raise ValueError(f"Order below {self.inst_id} minimum notional")
        elif not market:
            raise ValueError("Limit/IOC order requires a price")


@dataclass
class OrderBook:
    bids: List[List[float]]
    asks: List[List[float]]


@dataclass
class OrderResult:
    ord_id: str
    cl_ord_id: str
    fill_sz: float = 0.0
    avg_px: float = 0.0
    state: str = "submitted"
    raw: Dict[str, Any] = field(default_factory=dict)


class BaseExchangeAdapter(ABC):
    exchange: ClassVar[str] = "base"
    # 私有额度按账户与出口隔离，公共接口只共享同一出口的冷却。
    _rate_limit_until: ClassVar[dict[tuple, float]] = {}
    _contract_cache: ClassVar[dict[tuple, tuple[float, dict]]] = {}
    _live_clients: ClassVar[set] = set()

    def __init__(
        self,
        credentials: Dict[str, str],
        environment: str = "demo",
        proxy_url: Optional[str] = None,
        margin_mode: str = "cross",
        position_mode: str = "hedge",
        timeout: float = 12.0,
    ):
        self.credentials = credentials
        self.environment = environment
        self.proxy_url = proxy_url
        self.margin_mode = margin_mode
        self.position_mode = position_mode
        self.timeout = timeout
        self._time_offset_ms: int = 0
        self._clock_calibrated_at: float = 0.0
        self._clock_uncertainty_ms: float = 0.0
        self._http_clients: dict = {}

    def _rate_limit_key(self, auth: bool) -> tuple:
        route = hashlib.sha256((self.proxy_url or "direct").encode()).hexdigest()
        identity = hashlib.sha256(self.credentials.get("api_key", "").strip().encode()).hexdigest() if auth else "public"
        return self.exchange, self.environment, "private" if auth else "public", route, identity

    def _check_rate_limit(self, auth: bool = True) -> None:
        remaining = self._rate_limit_until.get(self._rate_limit_key(auth), 0) - time.monotonic()
        if remaining > 0:
            raise OrderRejected(self.exchange, "RATE_LIMITED", f"Exchange cooldown active for {remaining:.2f}s")

    def _note_rate_limit(self, response: Any, auth: bool = True) -> None:
        delay = 1.0
        headers = response.headers
        retry_after = headers.get("Retry-After")
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                try:
                    delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
                except (TypeError, ValueError, OverflowError):
                    pass
        reset = headers.get("X-Bapi-Limit-Reset-Timestamp")
        if reset:
            try:
                delay = max(delay, float(reset) / 1000 - time.time())
            except ValueError:
                pass
        if not math.isfinite(delay):
            delay = 1.0
        deadline = time.monotonic() + max(1.0, delay) + random.uniform(0.05, 0.25)
        key = self._rate_limit_key(auth)
        self._rate_limit_until[key] = max(self._rate_limit_until.get(key, 0), deadline)

    @asynccontextmanager
    async def request_client(self, auth: bool = True):
        # 公私请求池分离，连接在账户配置失效或应用关闭时释放。
        client = self._http_clients.get(auth)
        if client is None or client.is_closed:
            client = ProxyManager.build_client(
                proxy_url=self.proxy_url, timeout=self.timeout, exchange=self.exchange)
            self._http_clients[auth] = client
            self._live_clients.add(client)
        yield client

    async def aclose(self) -> None:
        # 先摘除旧池，后续请求不能在清理期间重用旧凭据的连接。
        clients = list(self._http_clients.values())
        self._http_clients.clear()
        await ProxyManager.close_clients(clients)
        self._live_clients.difference_update(clients)

    def _contract_cache_key(self) -> tuple:
        return self.exchange, self.environment, getattr(self, "base_url", getattr(self, "BASE_URL", ""))

    def _cached_instruments(self, force_refresh: bool) -> Optional[dict]:
        # 合约元数据与凭据无关，按交易所环境共享十分钟缓存。
        cached = self._contract_cache.get(self._contract_cache_key())
        if not force_refresh and cached is not None and time.monotonic() - cached[0] < 600:
            return cached[1]
        return None

    def _cache_instruments(self, instruments: dict) -> dict:
        self._contract_cache[self._contract_cache_key()] = (time.monotonic(), instruments)
        return instruments

    def _calibrate_time(self, server_ms: int, sent_ms: float) -> int:
        received_ms = time.time() * 1000
        rtt = received_ms - sent_ms
        if server_ms <= 0 or not 0 <= rtt <= 2000:
            raise RuntimeError("Exchange clock sample is missing or too uncertain")
        self._time_offset_ms = int(server_ms - (sent_ms + received_ms) / 2)
        self._clock_uncertainty_ms = rtt / 2
        self._clock_calibrated_at = time.time()
        return self._time_offset_ms

    def _assert_supported_entry_mode(self, mgn_mode: str) -> None:
        # A one-way account can net another binding's opposite position. Until
        # a netting policy exists, new allocation-owned entries require hedge mode.
        if self.position_mode != "hedge" or mgn_mode != "cross" or self.margin_mode != "cross":
            raise OrderRejected(self.exchange, "UNSUPPORTED_MODE", "New entries require cross margin and hedge mode")

    def _symbol_matches(self, lead_symbol: str, inst_id: str,
                        instruments: Dict[str, InstrumentSpec]) -> bool:
        spec = instruments.get(inst_id)
        if spec is None or spec.contract_type != "linear" or spec.settle_ccy != "USDT":
            return False
        lead_base = lead_symbol.removesuffix("USDT")
        target_base = spec.base_ccy or inst_id.removesuffix("-USDT-SWAP").removesuffix("USDT")
        return lead_symbol.endswith("USDT") and target_base == lead_base and spec.quote_ccy == "USDT"

    async def validate_order(self, inst_id: str, ord_type: str, sz: str,
                             px: Optional[str] = None, reduce_only: bool = False) -> None:
        # 明显无效的本地参数先拒绝，不让额外行情请求掩盖拒单原因。
        if ord_type.lower() not in ("market", "limit", "ioc"):
            raise OrderRejected(self.exchange, "LOCAL_VALIDATION", "Unsupported order type")
        spec = (await self.get_instruments()).get(inst_id)
        if spec is None:
            raise OrderRejected(self.exchange, "LOCAL_VALIDATION", f"Unqualified instrument: {inst_id}")
        ref_px = px
        if px is None and not reduce_only:
            book = await self.get_orderbook(inst_id, depth=1)
            if not book.bids or not book.asks:
                raise OrderRejected(self.exchange, "LOCAL_VALIDATION", "Missing price for final order validation")
            ref_px = str(min(book.bids[0][0], book.asks[0][0]))
        try:
            # 只包装提交前的本地校验，响应解析错误仍属于结果不确定。
            spec.validate_order(ord_type, sz, ref_px, reduce_only)
        except (ValueError, ArithmeticError) as exc:
            raise OrderRejected(self.exchange, "LOCAL_VALIDATION", str(exc)) from exc

    async def get_max_leverage(self, inst_id: str, target_notional: Any) -> int:
        """Return an exchange-confirmed tier limit. Concrete adapters must qualify it."""
        raise ExchangeAPIError(self.exchange, "UNQUALIFIED_TIERS", "Exchange leverage tiers unavailable")

    async def get_futures_transfer_history(
        self, asset: str = "USDT",
        start_ms: Optional[int] = None, end_ms: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """现货 <-> 合约钱包划转历史；未实现的交易所按“流水未知”处理。"""
        raise ExchangeAPIError(self.exchange, "UNSUPPORTED_CASH_FLOW",
                               "Futures transfer history unavailable")

    async def get_open_orders(self) -> List[Dict[str, Any]]:
        """Read all ordinary and conditional pending orders, or reject an incomplete view."""
        raise ExchangeAPIError(self.exchange, "UNQUALIFIED_ORDERS", "Complete pending-order inventory unavailable")

    async def cancel_order(self, inst_id: str, cl_ord_id: str) -> None:
        """撤销一笔普通挂单；撤单结果以随后的查单为准，失败时抛 ExchangeAPIError。"""
        raise ExchangeAPIError(self.exchange, "UNSUPPORTED_CANCEL", "Order cancellation unavailable")

    async def sync_time(self) -> int:
        """Synchronize clock offset (server_ms - local_ms) to prevent timestamp drift rejections."""
        return self._time_offset_ms

    async def place_tp_sl(
        self,
        inst_id: str,
        pos_side: str,
        sz: str,
        tp_px: Optional[str] = None,
        sl_px: Optional[str] = None,
        td_mode: str = "cross",
    ) -> Dict[str, Any]:
        """Place conditional TP/SL guard order(s) for an open position."""
        return {"ok": False, "unsupported": True, "details": f"TP/SL orders not supported on {self.exchange}"}

    async def cancel_tp_sl(self, inst_id: str, pos_side: Optional[str] = None) -> bool:
        """Cancel pending conditional TP/SL orders for an instrument."""
        return False

    @abstractmethod
    def resolve_symbol(
        self,
        lead_symbol: str,
        instruments: Dict[str, InstrumentSpec],
        symbol_map: Optional[Dict[str, str]] = None,
    ) -> tuple[str, bool]:
        raise NotImplementedError

    @abstractmethod
    async def test_connection(self) -> Dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def get_usdt_balance(self) -> BalanceInfo:
        raise NotImplementedError

    async def get_contract_balance(self, spec: Optional[InstrumentSpec] = None) -> BalanceInfo:
        """Backward-compatible collateral reader for USDT-only adapters."""
        if spec is not None and spec.settle_ccy != "USDT":
            raise ValueError("Adapter cannot confirm non-USDT collateral")
        return await self.get_usdt_balance()

    @abstractmethod
    async def get_positions(self) -> List[ExchangePosition]:
        raise NotImplementedError

    @abstractmethod
    async def get_instruments(self, force_refresh: bool = False) -> Dict[str, InstrumentSpec]:
        raise NotImplementedError

    @abstractmethod
    async def get_orderbook(self, inst_id: str, depth: int = 5) -> OrderBook:
        raise NotImplementedError

    @abstractmethod
    async def ensure_leverage(
        self,
        inst_id: str,
        lever: int,
        pos_side: str = "long",
        mgn_mode: str = "cross",
    ) -> bool:
        raise NotImplementedError

    @abstractmethod
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
        raise NotImplementedError

    @abstractmethod
    async def get_order(
        self,
        inst_id: str,
        ord_id: Optional[str] = None,
        cl_ord_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        raise NotImplementedError
