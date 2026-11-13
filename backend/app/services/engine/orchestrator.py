from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from fastapi import WebSocket
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import DEFAULT_SYSTEM_CONFIG, get_session_factory
from app.core.config import get_settings
from app.core.security import CredentialVault
from app.core.secret_mask import mask_sensitive_payload, redact_proxy_userinfo
from app.models.domain import CopyBinding, LeadSource, ProxyNode, TradingAccount
from app.services.adapters.factory import create_exchange_adapter
from app.services.analysis.pa_analyzer import PAAnalyzer
from app.services.engine.router import CopyRouter
from app.services.engine.risk import RiskEngine
from app.services.engine.serialization import serialized_execution
from app.services.engine.sizer import exact
from app.services.lead_collector.binance_bapi import (
    BinanceBapiCollector, STORE_MAX, check_source_fill_update, is_authoritative_snapshot, validate_source_fill,
)
from app.services.lead_collector.freshness import lead_freshness
from app.services.notifier.outbox import enqueue_notification
from app.services.proxy.manager import ProxyEgressError, ProxyManager

logger = logging.getLogger("mirrorquant.orchestrator")


class WebSocketHub:
    def __init__(self):
        self._clients: Set[WebSocket] = set()
        self._validators = {}
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket, validator=None) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)
            self._validators[ws] = validator

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)
            self._validators.pop(ws, None)

    async def broadcast(self, message: Dict[str, Any]) -> None:
        message = mask_sensitive_payload(message)
        async with self._lock:
            clients = list(self._clients)
        if not clients:
            return
        async def send(ws: WebSocket) -> None:
            try:
                validator = self._validators.get(ws)
                if validator is None or not await asyncio.wait_for(validator(), timeout=1.0):
                    await asyncio.wait_for(ws.close(code=1008), timeout=1.0)
                    await self.disconnect(ws)
                    return
                await asyncio.wait_for(ws.send_json(message), timeout=1.0)
            except Exception:
                await self.disconnect(ws)

        await asyncio.gather(*(send(ws) for ws in clients))


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
        self._risk_task: Optional[asyncio.Task] = None
        self.last_heartbeat_monotonic: Optional[float] = None
        self._last_lead_poll: Dict[int, float] = {}
        self._last_full_sweep: Dict[int, float] = {}
        self._last_account_poll: Dict[int, float] = {}
        self._last_clock_sync: Dict[int, float] = {}
        self._last_reconcile: Dict[int, float] = {}

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.last_heartbeat_monotonic = None
        self._task = asyncio.create_task(self._loop(), name="mirrorquant-orchestrator")
        self._risk_task = asyncio.create_task(self._risk_loop(), name="mirrorquant-risk-monitor")
        logger.info("EngineOrchestrator background loop started")

    async def stop(self) -> None:
        self._running = False
        self.last_heartbeat_monotonic = None
        tasks = [task for task in (self._task, self._risk_task) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = self._risk_task = None
        logger.info("EngineOrchestrator stopped")

    @property
    def is_running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    def heartbeat_age_sec(self) -> Optional[float]:
        if self.last_heartbeat_monotonic is None:
            return None
        return max(0.0, time.monotonic() - self.last_heartbeat_monotonic)

    async def _risk_loop(self) -> None:
        while self._running:
            try:
                await self.risk_tick()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Independent risk monitor failed")
            await asyncio.sleep(2.0)

    async def _loop(self) -> None:
        while self._running:
            self.last_heartbeat_monotonic = time.monotonic()
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
            # Refresh account health and calibrate clocks before admitting lead work.
            for account in accounts:
                last = self._last_account_poll.get(account.id, 0.0)
                if now - last >= 15.0:
                    self._last_account_poll[account.id] = now
                    await self.sync_account(db, account, proxies, resilience_cfg=resilience_cfg)

            for lead in leads:
                last = self._last_lead_poll.get(lead.id, 0.0)
                if now - last >= max(5, lead.poll_interval_sec):
                    self._last_lead_poll[lead.id] = now
                    await self.sync_lead(db, lead, proxies)

    async def risk_tick(self) -> None:
        """Protect owned exposure even while ingestion fails or entry controls are disabled."""
        factory = get_session_factory()
        async with factory() as db:
            binding_ids = list((await db.scalars(select(CopyBinding.id))).all())
        semaphore = asyncio.Semaphore(4)

        async def check_binding(binding_id: int) -> None:
            async with semaphore, factory() as db:
                try:
                    binding = await db.get(CopyBinding, binding_id)
                    if binding is None:
                        return
                    lead = await db.get(LeadSource, binding.lead_id)
                    account = await db.get(TradingAccount, binding.account_id)
                    if lead is None or account is None:
                        return
                    proxies = list((await db.scalars(select(ProxyNode).order_by(ProxyNode.priority))).all())
                    resilience_cfg = await self.router._get_config(db, "resilience", DEFAULT_SYSTEM_CONFIG["resilience"])
                    credentials = self.vault.decrypt_dict(account.encrypted_credentials)
                    proxy_url = ProxyManager.select_proxy_url(proxies, account.proxy_id, exchange=account.exchange)
                    adapter = create_exchange_adapter(
                        exchange=account.exchange, credentials=credentials,
                        environment=account.environment, proxy_url=proxy_url,
                        margin_mode=account.margin_mode, position_mode=account.position_mode,
                    )
                    adapter._time_offset_ms = int(getattr(account, "clock_offset_ms", 0) or 0)
                    await db.commit()
                    live_positions, instruments = await asyncio.wait_for(
                        asyncio.gather(adapter.get_positions(), adapter.get_instruments()), timeout=20.0
                    )
                    await self._check_account_equity(db, binding, account, adapter)
                    await self.router.check_position_guards(
                        db=db, lead=lead, account=account, binding=binding, adapter=adapter,
                        local_positions=live_positions, instruments=instruments,
                    )
                    now = time.time()
                    interval = max(15, int(resilience_cfg.get("reconcile_interval_sec", 60)))
                    snapshot = json.loads(lead.snapshot_json or "{}")
                    if lead.status == "running" and now - self._last_reconcile.get(binding.id, 0.0) >= interval and is_authoritative_snapshot(snapshot):
                        self._last_reconcile[binding.id] = now
                        # Guard orders may have changed physical exposure.
                        live_positions = await adapter.get_positions()
                        await self.router.reconcile_orphan_positions(
                            db=db, lead=lead, account=account, binding=binding, adapter=adapter,
                            lead_open_positions=snapshot["openPositions"], local_positions=live_positions,
                            instruments=instruments, resilience_cfg=resilience_cfg,
                        )
                    await db.commit()
                except Exception:
                    await db.rollback()
                    logger.exception("Risk monitor failed for binding %s", binding_id)

        await asyncio.gather(*(check_binding(binding_id) for binding_id in binding_ids))

    @serialized_execution
    async def _check_account_equity(self, db: AsyncSession, binding: CopyBinding, account: TradingAccount, adapter: Any) -> None:
        """Continuously latch configured loss breakers; protection remains active."""
        balance = await asyncio.wait_for(adapter.get_usdt_balance(), timeout=15.0)
        await db.refresh(binding)
        await db.refresh(account)
        if not math.isfinite(balance.eq) or not math.isfinite(balance.avail_eq):
            binding.circuit_breaker_tripped = True
            binding.circuit_breaker_reason = "Invalid exchange account equity; entries suspended"
            await db.commit()
            return
        account.hwm_equity = max(account.hwm_equity, balance.eq)
        CopyRouter._refresh_day_start(account, balance.eq)
        account.usdt_equity = balance.eq
        account.avail_equity = balance.avail_eq
        checks = (
            RiskEngine.check_drawdown(account.hwm_equity, balance.eq, binding.max_drawdown_pct),
            RiskEngine.check_daily_loss(account.day_start_equity, balance.eq, binding.daily_max_loss_pct),
        )
        for result in checks:
            if not result.allowed:
                binding.circuit_breaker_tripped = True
                binding.circuit_breaker_reason = result.reason
                break
        await db.commit()

    async def sync_lead(
        self,
        db: AsyncSession,
        lead: LeadSource,
        proxies: Optional[List[ProxyNode]] = None,
    ) -> Dict[str, Any]:
        started_ms = int(time.time() * 1000)
        if proxies is None:
            proxies = list((await db.scalars(select(ProxyNode).order_by(ProxyNode.priority))).all())

        try:
            proxy_urls = ProxyManager.ordered_proxy_urls(
                proxies, preferred_proxy_id=lead.proxy_id, exchange=self.collector.exchange,
            )
        except ProxyEgressError as exc:
            error = str(exc)
            published = await self._record_lead_sync_failure(db, lead, started_ms, error)
            return {"ok": False, "error": error, "superseded": not published}
        orders_store = json.loads(lead.orders_store_json or "{}")
        closed_cache = json.loads(lead.closed_cache_json or "[]")
        full_sweep = (time.time() - self._last_full_sweep.get(lead.id, 0.0)) > 600 or len(orders_store) < 100

        snapshot: Optional[Dict[str, Any]] = None
        last_err: Optional[Exception] = None
        for p_url in proxy_urls:
            try:
                snapshot = await asyncio.wait_for(
                    self.collector.sync_snapshot(
                        portfolio_id=lead.portfolio_id,
                        orders_store=orders_store,
                        closed_cache=closed_cache,
                        proxy_url=p_url,
                        full_sweep=full_sweep,
                        with_analysis=False,
                    ),
                    timeout=30.0,
                )
                break
            except Exception as exc:
                last_err = exc

        if snapshot is None:
            error = redact_proxy_userinfo(str(last_err))[:480] if last_err else "Sync failed"
            published = await self._record_lead_sync_failure(db, lead, started_ms, error)
            return {"ok": False, "error": error, "superseded": not published}

        if full_sweep:
            self._last_full_sweep[lead.id] = time.time()

        if not await self._publish_lead_snapshot(db, lead, snapshot, orders_store, started_ms):
            return {"ok": True, "superseded": True}

        if is_authoritative_snapshot(snapshot):
            await self.router.process_lead_events(
                db=db,
                lead=lead,
                raw_orders=list(orders_store.values()),
                raw_closed=(snapshot.get("closed") or []) if (snapshot.get("meta") or {}).get("closedHistoryComplete") else [],
                snapshot=snapshot,
                proxies=proxies,
            )
        else:
            logger.warning("Lead %s snapshot is unverified; financial routing deferred", lead.id)

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

    @serialized_execution
    async def _record_lead_sync_failure(self, db: AsyncSession, lead: LeadSource, started_ms: int, error: str) -> bool:
        await db.refresh(lead)
        last_sync_ms = lead.last_sync_at.replace(tzinfo=timezone.utc).timestamp() * 1000 if lead.last_sync_at else 0
        if last_sync_ms > started_ms:
            return False
        current = json.loads(lead.snapshot_json or "{}")
        if started_ms <= ((current.get("meta") or {}).get("storeRebuiltAtMs") or 0):
            return False
        meta = current.setdefault("openPositionsMeta", {})
        if (meta.get("lastFailureStartedAtMs") or 0) >= started_ms:
            return False
        failures = int(meta.get("consecutiveSyncFailures") or 0) + 1
        meta.update(authoritative=False, complete=False, lastFailureStartedAtMs=started_ms,
                    consecutiveSyncFailures=failures)
        meta.setdefault("firstSyncFailureAtMs", started_ms)
        freshness = lead_freshness(lead, current, now_ms=int(time.time() * 1000))
        age = freshness["snapshotAgeSec"]
        settings = get_settings()
        overdue = age is None or age > settings.lead_snapshot_stale_sec
        exhausted = failures >= settings.lead_sync_failure_limit
        logger.error("Lead %s sync failed; consecutiveFailures=%s snapshotAgeSec=%s", lead.id, failures, age)
        if (overdue or exhausted) and not meta.get("syncFailureNotified"):
            config = await self.router._get_config(db, "notifications", DEFAULT_SYSTEM_CONFIG["notifications"])
            enqueue_notification(db, config, "Lead snapshot stale / sync failure",
                                 f"Lead {lead.id}: consecutiveFailures={failures}, snapshotAgeSec={age}. "
                                 "Snapshot is unverified; check lead sync health.", event_type="error")
            meta["syncFailureNotified"] = True
        lead.snapshot_json = json.dumps(current, separators=(",", ":"))
        lead.status = "error"
        lead.last_error = redact_proxy_userinfo(error)[:480]
        await db.commit()
        return True

    @serialized_execution
    async def _publish_lead_snapshot(self, db: AsyncSession, lead: LeadSource,
                                    snapshot: Dict[str, Any], orders_store: Dict[str, Any], started_ms: int) -> bool:
        """Publish observation times monotonically in a short transaction after collection."""
        await db.refresh(lead)
        current = json.loads(lead.snapshot_json or "{}")
        current_meta = current.get("openPositionsMeta") or {}
        if started_ms <= ((current.get("meta") or {}).get("storeRebuiltAtMs") or 0):
            return False
        current_observed = max(current_meta.get("asOf") or 0, current_meta.get("lastFailureStartedAtMs") or 0,
                               (current.get("meta") or {}).get("collectionStartedAtMs") or 0)
        incoming_observed = (snapshot.get("openPositionsMeta") or {}).get("asOf") or 0
        if (incoming_observed and current_observed > incoming_observed) or (
            not incoming_observed and current_observed > started_ms
        ):
            return False
        # Concurrent collectors can start from different stores. Keep every observed
        # cumulative fill until per-binding ingestion has durably acknowledged it.
        merged = json.loads(lead.orders_store_json or "{}")
        for key, record in orders_store.items():
            validate_source_fill(record.get("q"), record.get("p"))
            previous = merged.get(key)
            if previous is not None and record.get("oid") in (None, ""):
                check_source_fill_update(previous, record)
            if previous is None or exact(record.get("q") or 0) >= exact(previous.get("q") or 0):
                merged[key] = record
            for legacy_key in [*(record.get("legacySourceKeys") or []), *(record.get("legacyKeys") or [])]:
                if legacy_key != key:
                    merged.pop(legacy_key, None)
        orders_store.clear()
        retained = sorted(merged.values(), key=lambda record: (record.get("ot") or record.get("t") or 0, record.get("h") or ""))[-STORE_MAX:]
        orders_store.update({record["h"]: record for record in retained})
        snapshot.setdefault("meta", {})["collectionStartedAtMs"] = started_ms
        if (current.get("meta") or {}).get("storeRebuiltAtMs"):
            snapshot["meta"]["storeRebuiltAtMs"] = current["meta"]["storeRebuiltAtMs"]
        if (current.get("meta") or {}).get("rebuildDigest"):
            snapshot["meta"]["rebuildDigest"] = current["meta"]["rebuildDigest"]
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

        return True

    @serialized_execution
    async def sync_account(
        self,
        db: AsyncSession,
        account: TradingAccount,
        proxies: Optional[List[ProxyNode]] = None,
        resilience_cfg: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        await db.refresh(account)
        if proxies is None:
            proxies = list((await db.scalars(select(ProxyNode).order_by(ProxyNode.priority))).all())
        try:
            creds = self.vault.decrypt_dict(account.encrypted_credentials)
            proxy_url = ProxyManager.select_proxy_url(proxies, account.proxy_id, exchange=account.exchange)
            adapter = create_exchange_adapter(
                exchange=account.exchange,
                credentials=creds,
                environment=account.environment,
                proxy_url=proxy_url,
                margin_mode=account.margin_mode,
                position_mode=account.position_mode,
            )
            adapter._time_offset_ms = int(getattr(account, "clock_offset_ms", 0) or 0)
            clock_sec = int((resilience_cfg or {}).get("clock_sync_interval_sec", 300))
            now = time.time()
            if clock_sec > 0 and (now - self._last_clock_sync.get(account.id, 0.0)) >= clock_sec:
                try:
                    offset_ms = await adapter.sync_time()
                    account.clock_offset_ms = int(offset_ms)
                    self._last_clock_sync[account.id] = now
                except Exception:
                    logger.warning("Clock calibration failed for account %s; retaining prior offset", account.id)

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
            account.last_error = redact_proxy_userinfo(str(exc))[:480]
            await db.commit()
            return {"ok": False, "error": account.last_error}


orchestrator = EngineOrchestrator()
