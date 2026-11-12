from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from fastapi import WebSocket
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import DEFAULT_SYSTEM_CONFIG, get_session_factory
from app.core.security import CredentialVault
from app.models.domain import CopyBinding, LeadSource, ProxyNode, TradingAccount
from app.services.adapters.factory import create_exchange_adapter
from app.services.analysis.pa_analyzer import PAAnalyzer
from app.services.engine.router import CopyRouter
from app.services.lead_collector.binance_bapi import BinanceBapiCollector
from app.services.proxy.manager import ProxyManager

logger = logging.getLogger("mirrorquant.orchestrator")


class WebSocketHub:
    def __init__(self):
        self._clients: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, message: Dict[str, Any]) -> None:
        async with self._lock:
            clients = list(self._clients)
        if not clients:
            return
        dead: List[WebSocket] = []
        for ws in clients:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)


class EngineOrchestrator:
    """Coordinates periodic lead collection, account sync, clock sync, reconciliation & WS streaming."""

    def __init__(self):
        self.ws_hub = WebSocketHub()
        self.pa_analyzer = PAAnalyzer()
        self.collector = BinanceBapiCollector(pa_analyzer=self.pa_analyzer)
        self.vault = CredentialVault()
        self.router = CopyRouter(vault=self.vault, broadcast_fn=self.ws_hub.broadcast)
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_lead_poll: Dict[int, float] = {}
        self._last_full_sweep: Dict[int, float] = {}
        self._last_account_poll: Dict[int, float] = {}
        self._last_clock_sync: Dict[int, float] = {}
        self._last_reconcile: Dict[int, float] = {}

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="mirrorquant-orchestrator")
        logger.info("EngineOrchestrator background loop started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("EngineOrchestrator stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Orchestrator tick error: %s", exc)
            await asyncio.sleep(2.0)

    async def tick(self) -> None:
        factory = get_session_factory()
        async with factory() as db:
            proxies = list((await db.scalars(select(ProxyNode).order_by(ProxyNode.priority))).all())
            leads = list(
                (await db.scalars(select(LeadSource).where(LeadSource.is_enabled == True))).all()  # noqa: E712
            )
            accounts = list(
                (await db.scalars(select(TradingAccount).where(TradingAccount.is_enabled == True))).all()  # noqa: E712
            )
            resilience_cfg = await self.router._get_config(
                db, "resilience", DEFAULT_SYSTEM_CONFIG["resilience"]
            )
            reconcile_sec = max(15, int(resilience_cfg.get("reconcile_interval_sec", 60)))

            now = time.time()
            for lead in leads:
                last = self._last_lead_poll.get(lead.id, 0.0)
                if now - last >= max(5, lead.poll_interval_sec):
                    self._last_lead_poll[lead.id] = now
                    await self.sync_lead(db, lead, proxies)

            for account in accounts:
                last = self._last_account_poll.get(account.id, 0.0)
                if now - last >= 15.0:
                    self._last_account_poll[account.id] = now
                    await self.sync_account(db, account, proxies, resilience_cfg=resilience_cfg)

            # Periodic position reconciliation & software TP/SL guard checks
            bindings = list(
                (await db.scalars(select(CopyBinding).where(CopyBinding.is_enabled == True))).all()  # noqa: E712
            )
            for b in bindings:
                if not b.baseline_seeded:
                    continue
                last_rec = self._last_reconcile.get(b.id, 0.0)
                if now - last_rec < reconcile_sec:
                    continue
                self._last_reconcile[b.id] = now
                lead = next((l for l in leads if l.id == b.lead_id), None)
                acct = next((a for a in accounts if a.id == b.account_id), None)
                if not lead or not acct or not lead.snapshot_json:
                    continue
                try:
                    snap = json.loads(lead.snapshot_json)
                    lead_open = snap.get("openPositions") or []
                    creds = self.vault.decrypt_dict(acct.encrypted_credentials)
                    proxy_url = (
                        ProxyManager.select_proxy_url(proxies, acct.proxy_id)
                        if acct.proxy_id is not None
                        else None
                    )
                    adapter = create_exchange_adapter(
                        exchange=acct.exchange,
                        credentials=creds,
                        environment=acct.environment,
                        proxy_url=proxy_url,
                        margin_mode=acct.margin_mode,
                        position_mode=acct.position_mode,
                    )
                    adapter._time_offset_ms = int(getattr(acct, "clock_offset_ms", 0) or 0)
                    live_pos = await adapter.get_positions()
                    instruments = await adapter.get_instruments()
                    await self.router.check_position_guards(
                        db=db,
                        lead=lead,
                        account=acct,
                        binding=b,
                        adapter=adapter,
                        local_positions=live_pos,
                        instruments=instruments,
                    )
                    if lead.position_show is not False:
                        await self.router.reconcile_orphan_positions(
                            db=db,
                            lead=lead,
                            account=acct,
                            binding=b,
                            adapter=adapter,
                            lead_open_positions=lead_open,
                            local_positions=live_pos,
                            instruments=instruments,
                            resilience_cfg=resilience_cfg,
                        )
                    await db.commit()
                except Exception as exc:
                    logger.debug("Reconcile/guard tick skipped for binding %s: %s", b.id, exc)

    async def sync_lead(
        self,
        db: AsyncSession,
        lead: LeadSource,
        proxies: Optional[List[ProxyNode]] = None,
    ) -> Dict[str, Any]:
        if proxies is None:
            proxies = list((await db.scalars(select(ProxyNode).order_by(ProxyNode.priority))).all())

        proxy_urls = ProxyManager.ordered_proxy_urls(proxies, preferred_proxy_id=lead.proxy_id)
        orders_store = json.loads(lead.orders_store_json or "{}")
        closed_cache = json.loads(lead.closed_cache_json or "[]")
        full_sweep = (time.time() - self._last_full_sweep.get(lead.id, 0.0)) > 600 or len(orders_store) < 100

        snapshot: Optional[Dict[str, Any]] = None
        last_err: Optional[Exception] = None
        for p_url in proxy_urls:
            try:
                snapshot = await self.collector.sync_snapshot(
                    portfolio_id=lead.portfolio_id,
                    orders_store=orders_store,
                    closed_cache=closed_cache,
                    proxy_url=p_url,
                    full_sweep=full_sweep,
                    with_analysis=True,
                )
                break
            except Exception as exc:
                last_err = exc

        if snapshot is None:
            lead.status = "error"
            lead.last_error = str(last_err)[:480] if last_err else "Sync failed"
            await db.commit()
            return {"ok": False, "error": lead.last_error}

        if full_sweep:
            self._last_full_sweep[lead.id] = time.time()

        profile = snapshot.get("profile") or {}
        lead.avatar_url = profile.get("avatarUrl") or lead.avatar_url
        lead.description = profile.get("description") or lead.description
        lead.position_show = profile.get("positionShow")
        lead.margin_balance = float(profile.get("marginBalance") or 0.0)
        lead.aum_amount = float(profile.get("aumAmount") or 0.0)
        lead.last_trade_time = int(profile.get("lastTradeTime") or 0)
        lead.last_sync_at = datetime.now(timezone.utc)
        lead.last_error = None
        lead.status = "running"

        lead.orders_store_json = json.dumps(orders_store, separators=(",", ":"))
        if snapshot.get("closed"):
            lead.closed_cache_json = json.dumps(snapshot["closed"], separators=(",", ":"))
        lead.snapshot_json = json.dumps(snapshot, separators=(",", ":"))
        await db.commit()

        await self.router.process_lead_events(
            db=db,
            lead=lead,
            raw_orders=list(orders_store.values()),
            raw_closed=snapshot.get("closed") or [],
            snapshot=snapshot,
            proxies=proxies,
        )

        await self.ws_hub.broadcast(
            {
                "type": "lead_update",
                "leadId": lead.id,
                "name": lead.name,
                "marginBalance": lead.margin_balance,
                "openPositions": snapshot.get("openPositions", []),
                "summary": snapshot.get("openPositionsSummary", {}),
                "updatedAt": int(time.time() * 1000),
            }
        )
        return {"ok": True, "snapshot": snapshot}

    async def sync_account(
        self,
        db: AsyncSession,
        account: TradingAccount,
        proxies: Optional[List[ProxyNode]] = None,
        resilience_cfg: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if proxies is None:
            proxies = list((await db.scalars(select(ProxyNode).order_by(ProxyNode.priority))).all())
        try:
            creds = self.vault.decrypt_dict(account.encrypted_credentials)
            proxy_url = (
                ProxyManager.select_proxy_url(proxies, account.proxy_id)
                if account.proxy_id is not None
                else None
            )
            adapter = create_exchange_adapter(
                exchange=account.exchange,
                credentials=creds,
                environment=account.environment,
                proxy_url=proxy_url,
                margin_mode=account.margin_mode,
                position_mode=account.position_mode,
            )
            clock_sec = int((resilience_cfg or {}).get("clock_sync_interval_sec", 300))
            now = time.time()
            if clock_sec > 0 and (now - self._last_clock_sync.get(account.id, 0.0)) >= clock_sec:
                try:
                    offset_ms = await adapter.sync_time()
                    account.clock_offset_ms = int(offset_ms)
                    self._last_clock_sync[account.id] = now
                except Exception:
                    pass
            else:
                adapter._time_offset_ms = int(getattr(account, "clock_offset_ms", 0) or 0)

            bal = await adapter.get_usdt_balance()
            positions = await adapter.get_positions()
            if bal.eq > account.hwm_equity:
                account.hwm_equity = bal.eq
            CopyRouter._refresh_day_start(account, bal.eq)
            account.usdt_equity = bal.eq
            account.avail_equity = bal.avail_eq
            account.total_notional = sum(p.notional_usd for p in positions)
            account.unrealized_pnl = sum(p.upl for p in positions)
            account.positions_json = json.dumps([p.to_dict() for p in positions])
            account.last_sync_at = datetime.now(timezone.utc)
            account.last_error = None
            account.status = "connected"
            await db.commit()

            await self.ws_hub.broadcast(
                {
                    "type": "account_update",
                    "accountId": account.id,
                    "name": account.name,
                    "usdtEquity": account.usdt_equity,
                    "unrealizedPnl": account.unrealized_pnl,
                    "positions": [p.to_dict() for p in positions],
                }
            )
            return {"ok": True, "usdtEquity": bal.eq, "positions": [p.to_dict() for p in positions]}
        except Exception as exc:
            account.status = "error"
            account.last_error = str(exc)[:480]
            await db.commit()
            return {"ok": False, "error": account.last_error}


orchestrator = EngineOrchestrator()
