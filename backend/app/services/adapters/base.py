from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from email.utils import parsedate_to_datetime
import math
import random
import time
from typing import Any, ClassVar, Dict, List, Optional


class ExchangeAPIError(RuntimeError):
    def __init__(self, exchange: str, code: str, msg: str, data: Any = None):
        super().__init__(f"{exchange.upper()} API Error [{code}]: {msg}")
        self.exchange = exchange
        self.code = code
        self.msg = msg
        self.data = data


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
    # Shared by all clients/accounts in this worker because public limits can be IP-wide.
    _rate_limit_until: ClassVar[Dict[str, float]] = {}

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

    def _check_rate_limit(self) -> None:
        remaining = self._rate_limit_until.get(self.exchange, 0) - time.monotonic()
        if remaining > 0:
            raise ExchangeAPIError(self.exchange, "RATE_LIMITED", f"Exchange cooldown active for {remaining:.2f}s")

    def _note_rate_limit(self, response: Any) -> None:
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
        self._rate_limit_until[self.exchange] = max(self._rate_limit_until.get(self.exchange, 0), deadline)

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
            raise ExchangeAPIError(self.exchange, "UNSUPPORTED_MODE", "New entries require cross margin and hedge mode")

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
        if ord_type.lower() not in ("market", "limit", "ioc"):
            raise ValueError("Unsupported order type")
        spec = (await self.get_instruments()).get(inst_id)
        if spec is None:
            raise ValueError(f"Unqualified instrument: {inst_id}")
        ref_px = px
        if px is None and not reduce_only:
            book = await self.get_orderbook(inst_id, depth=1)
            if not book.bids or not book.asks:
                raise ValueError("Missing price for final order validation")
            ref_px = str(min(book.bids[0][0], book.asks[0][0]))
        spec.validate_order(ord_type, sz, ref_px, reduce_only)

    async def get_max_leverage(self, inst_id: str, target_notional: Any) -> int:
        """Return an exchange-confirmed tier limit. Concrete adapters must qualify it."""
        raise ExchangeAPIError(self.exchange, "UNQUALIFIED_TIERS", "Exchange leverage tiers unavailable")

    async def get_open_orders(self) -> List[Dict[str, Any]]:
        """Read all ordinary and conditional pending orders, or reject an incomplete view."""
        raise ExchangeAPIError(self.exchange, "UNQUALIFIED_ORDERS", "Complete pending-order inventory unavailable")

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
