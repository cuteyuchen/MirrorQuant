from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional


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
    exchange: str = "base"

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
