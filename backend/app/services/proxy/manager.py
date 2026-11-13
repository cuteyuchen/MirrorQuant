from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Sequence

import httpx

from app.core.config import get_settings
from app.core.secret_mask import redact_proxy_userinfo
from app.models.domain import ProxyNode


class ProxyEgressError(RuntimeError):
    """No permitted healthy egress route is available."""


@dataclass
class ProxyTestResult:
    healthy: bool
    latency_ms: Optional[float]
    error: Optional[str] = None
    status_code: Optional[int] = None


class ProxyManager:
    """Manages proxy selection, failover ordering, and httpx client creation."""

    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
    }

    @staticmethod
    def _direct_egress_allowed(exchange: Optional[str]) -> bool:
        settings = get_settings()
        exchange = exchange.strip().lower() if exchange else None
        if exchange in settings.proxy_required_exchanges:
            return False
        if exchange in settings.direct_egress_exchanges:
            return True
        return settings.allow_direct_egress

    @staticmethod
    def build_client(
        proxy_url: Optional[str] = None,
        timeout: float = 15.0,
        headers: Optional[dict] = None,
        exchange: Optional[str] = None,
    ) -> httpx.AsyncClient:
        if not proxy_url and not ProxyManager._direct_egress_allowed(exchange):
            raise ProxyEgressError("Direct egress is disabled by egress policy; configure a healthy proxy")
        merged_headers = dict(ProxyManager.DEFAULT_HEADERS)
        if headers:
            merged_headers.update(headers)
        kwargs: dict = {
            "timeout": httpx.Timeout(timeout),
            "headers": merged_headers,
            "follow_redirects": True,
            # Only the selected route may determine egress, including direct routes.
            "trust_env": False,
        }
        if proxy_url:
            kwargs["proxy"] = proxy_url
        return httpx.AsyncClient(**kwargs)

    @staticmethod
    def select_proxy_url(
        nodes: Sequence[ProxyNode],
        preferred_proxy_id: Optional[int] = None,
        exchange: Optional[str] = None,
    ) -> Optional[str]:
        return ProxyManager.ordered_proxy_urls(nodes, preferred_proxy_id, exchange=exchange)[0]

    @staticmethod
    def ordered_proxy_urls(
        nodes: Sequence[ProxyNode],
        preferred_proxy_id: Optional[int] = None,
        exchange: Optional[str] = None,
    ) -> list[Optional[str]]:
        enabled = [n for n in nodes if n.is_enabled]
        ordered: list[Optional[str]] = []
        if preferred_proxy_id is not None:
            for n in enabled:
                if n.id == preferred_proxy_id and n.is_healthy and n.url not in ordered:
                    ordered.append(n.url)
        healthy = sorted(
            [n for n in enabled if n.is_healthy],
            key=lambda n: (n.priority, n.latency_ms if n.latency_ms is not None else 99999.0),
        )
        for n in healthy:
            if n.url not in ordered:
                ordered.append(n.url)
        if ProxyManager._direct_egress_allowed(exchange):
            ordered.append(None)
        if not ordered:
            raise ProxyEgressError("No healthy enabled proxy is available and direct egress is disabled by egress policy")
        return ordered

    @classmethod
    async def test_proxy(
        cls,
        proxy_url: Optional[str],
        test_url: str = "https://fapi.binance.com/fapi/v1/time",
        timeout: float = 10.0,
    ) -> ProxyTestResult:
        t0 = time.perf_counter()
        try:
            async with cls.build_client(proxy_url=proxy_url, timeout=timeout) as client:
                resp = await client.get(test_url)
            latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            if resp.status_code < 400:
                return ProxyTestResult(
                    healthy=True,
                    latency_ms=latency_ms,
                    status_code=resp.status_code,
                )
            return ProxyTestResult(
                healthy=False,
                latency_ms=latency_ms,
                status_code=resp.status_code,
                error=f"HTTP {resp.status_code}",
            )
        except Exception as exc:
            return ProxyTestResult(
                healthy=False,
                latency_ms=None,
                error=redact_proxy_userinfo(str(exc))[:400],
            )
