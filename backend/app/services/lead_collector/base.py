from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class BaseLeadCollector(ABC):
    """Abstract interface for copy-trading lead data collectors."""

    platform: str = "unknown"

    @abstractmethod
    async def fetch_detail(self, portfolio_id: str, proxy_url: Optional[str] = None) -> Dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def fetch_order_history(
        self,
        portfolio_id: str,
        page_number: int = 1,
        page_size: int = 100,
        proxy_url: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    async def fetch_position_history(
        self,
        portfolio_id: str,
        page_number: int = 1,
        page_size: int = 100,
        proxy_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def sync_snapshot(
        self,
        portfolio_id: str,
        orders_store: Dict[str, Dict[str, Any]],
        closed_cache: List[Dict[str, Any]],
        proxy_url: Optional[str] = None,
        full_sweep: bool = False,
        with_analysis: bool = True,
    ) -> Dict[str, Any]:
        raise NotImplementedError
