from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from fastapi import WebSocket
from sqlalchemy import JSON, cast, inspect, select, type_coerce
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import DEFAULT_SYSTEM_CONFIG, get_session_factory
from app.core.config import get_settings
from app.core.security import CredentialVault
from app.core.secret_mask import mask_sensitive_payload, redact_proxy_userinfo
from app.models.domain import CopyBinding, LeadSource, NotificationOutbox, ProxyNode, TradingAccount
from app.maintenance.retention import cleanup_retained_records
from app.services.account_detail.serializers import dt_to_ms
from app.services.account_detail.snapshots import capture_equity_snapshot
from app.services.account_detail.sync import due_detail_datasets, sync_account_detail
from app.services.adapters.factory import create_exchange_adapter
from app.services.adapters.base import BalanceInfo, ExchangeAPIError, get_collateral_balance
from app.services.analysis.pa_analyzer import PAAnalyzer
from app.services.engine.cashflow import cash_flow_is_fresh, fetch_today_net_flow
from app.core.time_util import BEIJING_TZ
from app.services.engine.router import CopyRouter
from app.services.engine.auto_blacklist import reconcile_auto_blacklist
from app.services.engine.risk import RiskEngine
from app.services.engine.equity import display_equity, equity_baseline, persist_equity, risk_state
from app.services.engine.serialization import serialized_execution
from app.services.engine.sizer import exact
from app.services.lead_collector.binance_bapi import (
    BinanceBapiCollector, STORE_MAX, check_source_fill_update, describe_exception,
    is_authoritative_snapshot, is_routable_snapshot, validate_source_fill,
)
from app.services.lead_collector.freshness import lead_freshness
from app.services.notifier.outbox import enqueue_notification
from app.services.proxy.manager import ProxyEgressError, ProxyManager

logger = logging.getLogger("mirrorquant.orchestrator")
SYNC_FAILURE_ERROR_INTERVAL = 600.0
SYNC_FAILURE_WARN_INTERVAL = 300.0
SYNC_FAILURE_FIELDS = (
    "consecutiveSyncFailures", "firstSyncFailureAtMs", "lastFailureStartedAtMs", "syncFailureNotified",
)
# 名额监控：币安跟单组合的详情页链接模板（按 portfolio_id 生成）。
SLOT_COPY_LINK_TEMPLATE = (
    "https://www.binance.com/zh-CN/copy-trading/lead-details/{portfolio_id}?timeRange=30D&isSmartFilter=true"
)
SLOT_MIN_COPY_FALLBACK = "300"


def _parse_copy_count(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isascii() and text.isdecimal():
            return int(text)
    return None


def copy_slot_opened(previous_profile: Dict[str, Any], new_profile: Dict[str, Any]) -> Optional[tuple]:
    """Detect a full -> open copy-slot transition between two snapshots.

    Returns (current, maximum) only on the transition; None otherwise
    (still full, still open, or counts unknown). Because the previous
    profile is the persisted snapshot, no extra state is needed: while a
    slot stays open every round sees previous=open and stays silent, and
    once it refills the next opening is a fresh edge again.
    """
    prev_current = _parse_copy_count((previous_profile or {}).get("currentCopyCount"))
    prev_max = _parse_copy_count((previous_profile or {}).get("maxCopyCount"))
    new_current = _parse_copy_count((new_profile or {}).get("currentCopyCount"))
    new_max = _parse_copy_count((new_profile or {}).get("maxCopyCount"))
    if None in (prev_current, prev_max, new_current, new_max):
        return None
    if prev_max <= 0 or new_max <= 0:
        return None
    if prev_current >= prev_max and new_current < new_max:
        return (new_current, new_max)
    return None


class WebSocketHub:
    """Fan-out for live updates, scoped per connected user.

    Broadcasts carrying an ``ownerUserId`` (account updates, execution
    events) only reach that user and admins; broadcasts without one (lead
    updates, which are visible to every logged-in user) reach everyone.
    """

    def __init__(self):
        self._clients: Set[WebSocket] = set()
        self._validators = {}
        self._audiences: Dict[WebSocket, tuple] = {}
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket, validator=None, user_id=None, is_admin=False) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)
            self._validators[ws] = validator
            self._audiences[ws] = (user_id, bool(is_admin))

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)
            self._validators.pop(ws, None)
            self._audiences.pop(ws, None)

    def _may_receive(self, ws: WebSocket, message: Dict[str, Any]) -> bool:
        owner = message.get("ownerUserId")
        if owner is None:
            return True
        user_id, is_admin = self._audiences.get(ws, (None, False))
        return bool(is_admin) or user_id == owner

    async def broadcast(self, message: Dict[str, Any]) -> None:
        message = mask_sensitive_payload(message)
        async with self._lock:
            clients = [ws for ws in list(self._clients) if self._may_receive(ws, message)]
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
        self._lead_sync_in_flight: Set[int] = set()
        self._lead_sync_tasks: Set[asyncio.Task] = set()
        self._detail_sync_in_flight: Set[int] = set()
        self._detail_sync_tasks: Set[asyncio.Task] = set()
        self._detail_stagger_until: Dict[int, float] = {}
        self._last_risk_snapshot_at: Dict[int, float] = {}
        self._last_full_sweep: Dict[int, float] = {}
        self._last_history_sync: Dict[int, float] = {}
        self._last_account_poll: Dict[int, float] = {}
        self._last_clock_sync: Dict[int, float] = {}
        self._last_reconcile: Dict[int, float] = {}
        self._last_auto_blacklist: Dict[int, float] = {}
        self._last_unverified_logged: Dict[int, str] = {}
        self._sync_failure_log_at: Dict[int, float] = {}
        self._sync_failure_warn_at: Dict[int, float] = {}
        self._sync_failure_error_window: Dict[int, List[float]] = {}
        self._last_retention_cleanup = 0.0  # 清理只在主轮询低频执行。

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
        for task in list(self._lead_sync_tasks):
            task.cancel()
        if self._lead_sync_tasks:
            await asyncio.gather(*self._lead_sync_tasks, return_exceptions=True)
        for task in list(getattr(self, "_detail_sync_tasks", set())):
            task.cancel()
        if getattr(self, "_detail_sync_tasks", None):
            await asyncio.gather(*self._detail_sync_tasks, return_exceptions=True)
        self._task = self._risk_task = None
        self._lead_sync_tasks.clear()
        self._lead_sync_in_flight.clear()
        if hasattr(self, "_detail_sync_tasks"):
            self._detail_sync_tasks.clear()
        if hasattr(self, "_detail_sync_in_flight"):
            self._detail_sync_in_flight.clear()
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
            # 0.5 秒：tick 本身只做派发（各 lead 同步在后台任务里），
            # 睡 2 秒会让 2 秒轮询的 lead 实际间隔拖到 4-5 秒以上。
            # 派发仍受各 lead 的 poll_interval_sec 与 in-flight 守卫约束，
            # 不会因此多发请求。
            await asyncio.sleep(0.5)

    async def tick(self) -> None:
        factory = get_session_factory()
        async with factory() as db:
            if time.monotonic() - self._last_retention_cleanup >= get_settings().retention_cleanup_interval_sec:
                try:
                    await cleanup_retained_records(db)
                except Exception:
                    # 维护故障与账户同步隔离，失败事务回滚后仍遵守低频重试。
                    await db.rollback()
                    logger.exception("Retention cleanup failed")
                finally:
                    self._last_retention_cleanup = time.monotonic()
            proxies = list((await db.scalars(select(ProxyNode).order_by(ProxyNode.priority))).all())
            leads = list(
                (await db.scalars(select(LeadSource).where(
                    LeadSource.is_enabled == True,  # noqa: E712
                    LeadSource.deleted_at.is_(None)))).all()
            )
            accounts = list(
                (await db.scalars(select(TradingAccount).where(
                    TradingAccount.is_enabled == True,  # noqa: E712
                    TradingAccount.deleted_at.is_(None)))).all()
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

            if isinstance(db, AsyncSession):
                await self._schedule_account_detail_syncs(db, accounts, now)

            for lead in leads:
                last = self._last_lead_poll.get(lead.id, 0.0)
                # 并行轮询：各 lead 独立会话并发同步，不再顺序等待。顺序轮询时
                # 一轮要等全部 lead 串行同步完（每人约 3 秒），金木的有效间隔
                # 被拖到 10 秒以上；并行后每人的间隔只取决于自己的同步耗时。
                # 下限 1 秒（原 5 秒钳制会挡住 2 秒以内的交易级轮询）。
                # 同一 lead 正在同步时不重复派发（_lead_sync_in_flight），
                # 避免同步耗时超过间隔时同一 lead 的同步重叠。
                if (lead.id not in self._lead_sync_in_flight
                        and now - last >= max(1, lead.poll_interval_sec)):
                    self._last_lead_poll[lead.id] = now
                    self._lead_sync_in_flight.add(lead.id)
                    task = asyncio.create_task(
                        self._sync_lead_isolated(lead.id), name=f"mirrorquant-lead-sync-{lead.id}")
                    self._lead_sync_tasks.add(task)
                    task.add_done_callback(self._discard_lead_sync_task)

    def _discard_lead_sync_task(self, task: asyncio.Task) -> None:
        self._lead_sync_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.exception("Lead sync task failed: %s", exc)

    def _discard_detail_sync_task(self, task: asyncio.Task) -> None:
        tasks = getattr(self, "_detail_sync_tasks", None)
        if tasks is not None:
            tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("Account detail sync task error: %s", redact_proxy_userinfo(str(exc)))

    async def _schedule_account_detail_syncs(
        self, db: AsyncSession, accounts: List[TradingAccount], now: float
    ) -> None:
        in_flight = getattr(self, "_detail_sync_in_flight", None)
        if in_flight is None:
            in_flight = self._detail_sync_in_flight = set()
        tasks = getattr(self, "_detail_sync_tasks", None)
        if tasks is None:
            tasks = self._detail_sync_tasks = set()
        stagger = getattr(self, "_detail_stagger_until", None)
        if stagger is None:
            stagger = self._detail_stagger_until = {}

        if in_flight or tasks:
            return
        now_ms = int(now * 1000)
        for idx, account in enumerate(accounts):
            if account.id in in_flight or inspect(account, raiseerr=False) is None:
                continue
            if account.id not in stagger:
                stagger[account.id] = now + (idx * 15.0)
            if now < stagger[account.id]:
                continue
            try:
                due = await due_detail_datasets(db, account.id, now_ms)
            except Exception as exc:
                logger.warning("Failed checking due detail datasets for account %s: %s", account.id, redact_proxy_userinfo(str(exc)))
                continue
            if not due:
                continue
            in_flight.add(account.id)
            task = asyncio.create_task(
                self._sync_detail_isolated(account.id, due),
                name=f"mirrorquant-detail-sync-{account.id}",
            )
            tasks.add(task)
            task.add_done_callback(self._discard_detail_sync_task)
            # Detail sync is background work: run one account at a time so
            # historical backfill cannot burst exchange quota across accounts.
            break

    async def _sync_detail_isolated(self, account_id: int, datasets: List[str]) -> None:
        """Background non-blocking account detail dataset sync; never alters trading state."""
        try:
            await sync_account_detail(None, account_id, datasets=datasets, manual=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Account %s detail sync failed: %s", account_id, redact_proxy_userinfo(str(exc)))
        finally:
            in_flight = getattr(self, "_detail_sync_in_flight", None)
            if in_flight is not None:
                in_flight.discard(account_id)

    async def _capture_risk_tick_snapshot(
        self, db: AsyncSession, account: TradingAccount, live_positions: List[Any]
    ) -> None:
        if not isinstance(db, AsyncSession) or inspect(account, raiseerr=False) is None:
            return
        last_snapshot_at = getattr(self, "_last_risk_snapshot_at", None)
        if last_snapshot_at is None:
            last_snapshot_at = self._last_risk_snapshot_at = {}
        now_mono = time.monotonic()
        if now_mono - last_snapshot_at.get(account.id, 0.0) < 30.0:
            return
        last_snapshot_at[account.id] = now_mono
        try:
            state = risk_state(account)
            if not state.get("display_usdt_confirmed"):
                return
            usdt_asset = (state.get("assets") or {}).get("USDT") or {}
            eq_val = usdt_asset.get("equity", account.usdt_equity)
            av_val = usdt_asset.get("available", account.avail_equity)
            if eq_val is None:
                return
            bal_ms = dt_to_ms(account.last_sync_at)
            now_ms = int(time.time() * 1000)
            upl_total = sum(float(getattr(p, "upl", 0.0) or 0.0) for p in live_positions)
            bal_proxy = BalanceInfo(
                eq=float(eq_val),
                avail_eq=float(av_val or 0.0),
                cash_bal=None,  # type: ignore[arg-type]
                upl=upl_total,
                currency="USDT",
                as_of_ms=bal_ms,
            )
            async with db.begin_nested():
                await capture_equity_snapshot(
                    db,
                    account,
                    bal_proxy,
                    live_positions,
                    source="risk_tick",
                    balance_as_of_ms=bal_ms,
                    positions_as_of_ms=now_ms,
                    now_ms=now_ms,
                )
        except Exception as exc:
            if "database is locked" in str(exc).lower():
                logger.debug(
                    "Risk-tick equity snapshot deferred for account %s: database locked",
                    account.id,
                )
                return
            logger.warning(
                "Risk-tick equity snapshot failed for account %s: %s",
                account.id,
                redact_proxy_userinfo(str(exc)),
            )

    async def _sync_lead_isolated(self, lead_id: int) -> None:
        """一个 lead 独占一个会话：与其他 lead 的同步并发跑，互不串会话。"""
        try:
            factory = get_session_factory()
            async with factory() as db:
                lead = await db.get(LeadSource, lead_id)
                if lead is None or lead.deleted_at is not None or not lead.is_enabled:
                    return
                await self.sync_lead(db, lead, proxies=None)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Lead %s isolated sync failed", lead_id)
        finally:
            self._lead_sync_in_flight.discard(lead_id)

    async def risk_tick(self) -> None:
        """Protect owned exposure even while ingestion fails or entry controls are disabled."""
        factory = get_session_factory()
        async with factory() as db:
            binding_ids = list((await db.scalars(select(CopyBinding.id).where(
                CopyBinding.deleted_at.is_(None)))).all())
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
                        account_id=account.id,  # 独立风控与轮询共享账户连接。
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
                    if isinstance(live_positions, list) and all(hasattr(p, "to_dict") for p in live_positions):
                        await self._persist_account_positions(db, account, live_positions)
                        await self._capture_risk_tick_snapshot(db, account, live_positions)
                    await self.router.check_position_guards(
                        db=db, lead=lead, account=account, binding=binding, adapter=adapter,
                        local_positions=live_positions, instruments=instruments,
                    )
                    if await self.router._has_resting(db, binding.id):
                        # 在簿限价单约每 2 秒核对一次：及时发现成交并通知，到期自动撤单。
                        await self.router.reconcile_pending_orders(db, account, adapter, instruments)
                    now = time.time()
                    interval = max(15, int(resilience_cfg.get("reconcile_interval_sec", 60)))
                    # Auto-blacklist also runs while the binding is disabled: isolating
                    # manual symbols is what makes a later re-enable safe.
                    if (getattr(account, "is_enabled", True)
                            and hasattr(adapter, "get_open_orders_normalized")
                            and now - self._last_auto_blacklist.get(binding.id, 0.0) >= interval):
                        self._last_auto_blacklist[binding.id] = now
                        await self._reconcile_auto_blacklist(
                            db, binding, account, adapter, positions=live_positions
                        )
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

    async def _reconcile_auto_blacklist(
        self, db: AsyncSession, binding: CopyBinding, account: TradingAccount, adapter: Any,
        *, positions: Optional[List[Any]] = None,
    ) -> None:
        """Auto-blacklist symbols with unowned exposure; failures never block protection.

        Positions already fetched by the risk tick are not reused: the tick may
        run after guard orders changed physical exposure, so a fresh inventory
        is required before removing an auto entry. Pending orders come from the
        complete normalized snapshot; if the exchange cannot provide a complete
        view, the check is skipped instead of guessing that nothing is resting.
        """
        if not bool(getattr(binding, "auto_blacklist_manual", True)):
            return
        if not asyncio.iscoroutinefunction(getattr(adapter, "get_open_orders_normalized", None)):
            return
        try:
            if positions is None:
                positions = await asyncio.wait_for(adapter.get_positions(), timeout=15.0)
            open_orders = await asyncio.wait_for(adapter.get_open_orders_normalized(), timeout=15.0)
            notify_cfg = await self.router._get_config(
                db, "notifications", DEFAULT_SYSTEM_CONFIG["notifications"]
            )
            async with db.begin_nested():
                await reconcile_auto_blacklist(
                    db, binding, account,
                    positions=positions, open_orders=open_orders, notify_cfg=notify_cfg,
                )
            await db.commit()
        except Exception as exc:
            # The savepoint already rolled back only this feature's writes;
            # never roll back the caller's transaction from here.
            pass
            logger.warning(
                "Auto-blacklist reconciliation failed for binding %s: %s",
                binding.id, redact_proxy_userinfo(str(exc))[:300],
            )

    @serialized_execution
    async def _check_account_equity(self, db: AsyncSession, binding: CopyBinding, account: TradingAccount, adapter: Any) -> bool:
        """Continuously latch configured loss breakers; protection remains active."""
        await db.refresh(binding)
        await db.refresh(account)
        try:
            balance = await asyncio.wait_for(get_collateral_balance(adapter), timeout=15.0)
            observations = await persist_equity(db, account, balance, all_assets=True)
            if not set(risk_state(account)["assets"]).issubset(observations):
                raise ValueError("Previously observed collateral currency is missing")
            checks = []
            for currency, observation in observations.items():
                hwm, day_start = equity_baseline(account, currency)
                for check in RiskEngine.loss_breaker_checks(binding, hwm, day_start, observation.eq):
                    if check.reason:
                        check.reason += f" [{currency}]"
                    checks.append(check)
        except Exception as exc:
            # 抵押品检查失败只在熔断开关打开时闩锁：开关关闭即“全跟到底”，一次瞬时
            # 余额失败不得永久停掉开仓；逐笔风控门仍会在数据确实无效时拦单。
            # 失败原因记 warning（不记 traceback/ERROR），避免触发日志监控的硬异常，
            # 同时下次再发生时日志里能找到原文。
            logger.warning(
                "Collateral balance check failed for binding %s: %s",
                binding.id, redact_proxy_userinfo(str(exc))[:400],
            )
            if getattr(binding, "enable_circuit_breaker", True):
                binding.circuit_breaker_tripped = True
                binding.circuit_breaker_reason = "Invalid exchange collateral balance; entries suspended"
            account.status = "error"
            account.last_error = redact_proxy_userinfo(str(exc))[:400]
            await db.commit()
            return False
        RiskEngine.clear_disabled_loss_breaker(binding)
        for result in checks:
            if not result.allowed:
                binding.circuit_breaker_tripped = True
                binding.circuit_breaker_reason = result.reason
                break
        await db.commit()

        return True

    @serialized_execution
    async def _persist_account_positions(self, db: AsyncSession, account: TradingAccount, positions: List[Any]) -> None:
        """Persist live positions and unrealized PnL alongside periodic equity checks."""
        await db.refresh(account)
        account.total_notional = sum(p.notional_usd for p in positions)
        account.unrealized_pnl = sum(p.upl for p in positions)
        account.positions_json = json.dumps([p.to_dict() for p in positions])
        account.last_sync_at = datetime.now(timezone.utc)
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
            error = describe_exception(exc)
            published = await self._record_lead_sync_failure(db, lead, started_ms, error)
            return {"ok": False, "error": error, "superseded": not published}
        orders_store = json.loads(lead.orders_store_json or "{}")
        closed_cache = json.loads(lead.closed_cache_json or "[]")
        previous_snapshot = json.loads(lead.snapshot_json or "{}")
        full_sweep = (time.time() - self._last_full_sweep.get(lead.id, 0.0)) > 600 or len(orders_store) < 100
        # 轻量同步：公开带单员的高频轮询只拉 detail + 持仓（快路只认
        # 持仓），订单/闭仓历史每 30 秒才全量刷新一次。隐藏带单员
        # 没有持仓快路、订单流是唯一信号源，永远全量同步。
        # 轻量一次只剩 2 个请求、约 0.5 秒，全量约 4 秒；轻量把金木
        # 的有效间隔从约 5-7 秒压到约 2 秒，同时把 BAPI 请求量从
        # 每分钟约 150-300 次降到约 70 次，不再撞 90801003。
        prev_meta = (previous_snapshot or {}).get("openPositionsMeta") or {}
        is_public = ((previous_snapshot or {}).get("profile") or {}).get("positionShow") is True or prev_meta.get("positionShow") is True
        last_history = self._last_history_sync.get(lead.id, 0.0)
        light = bool(is_public and (time.time() - last_history) < 30.0 and orders_store)
        if not light:
            self._last_history_sync[lead.id] = time.time()

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
                        with_analysis=True,
                        previous_snapshot=previous_snapshot,
                        light=light,
                    ),
                    timeout=45.0,
                )
                break
            except Exception as exc:
                last_err = exc

        if snapshot is None:
            error = describe_exception(last_err)[:480]
            published = await self._record_lead_sync_failure(db, lead, started_ms, error)
            return {"ok": False, "error": error, "superseded": not published}

        if full_sweep:
            self._last_full_sweep[lead.id] = time.time()

        if not await self._publish_lead_snapshot(db, lead, snapshot, orders_store, started_ms):
            return {"ok": True, "superseded": True}

        try:
            await self._check_copy_slot(db, lead, previous_snapshot, snapshot)
        except Exception as exc:
            # 名额监控绝不能影响跟单主流程：只记日志。
            logger.warning("Lead %s slot-watch check failed: %s", lead.id, describe_exception(exc))

        if is_routable_snapshot(snapshot):
            self._last_unverified_logged[lead.id] = "verified"
            await self.router.process_lead_events(
                db=db,
                lead=lead,
                raw_orders=list(orders_store.values()),
                raw_closed=(snapshot.get("closed") or []) if (snapshot.get("meta") or {}).get("closedHistoryComplete") else [],
                snapshot=snapshot,
                proxies=proxies,
            )
        else:
            log = logger.debug if self._last_unverified_logged.get(lead.id) == "unverified" else logger.warning
            log("Lead %s snapshot is unverified; financial routing deferred", lead.id)
            self._last_unverified_logged[lead.id] = "unverified"

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

    async def _check_copy_slot(self, db: AsyncSession, lead: LeadSource,
                               previous_snapshot: Dict[str, Any], snapshot: Dict[str, Any]) -> None:
        """Notify once when a watched lead's copy-trading slot opens.

        Edge detection compares the persisted previous snapshot's profile
        against the freshly published one (see copy_slot_opened), so no
        extra state is needed. The alert goes to the lead's own notification
        channels when configured, otherwise to the system notification
        route (e.g. the DingTalk monitoring group). The outbox enqueue is
        the only side effect; any failure propagates to the caller, which
        logs it and continues the main follow flow.
        """
        if not lead.slot_watch_enabled:
            return
        opened = copy_slot_opened(
            (previous_snapshot or {}).get("profile") or {},
            (snapshot or {}).get("profile") or {},
        )
        if opened is None:
            return
        current, maximum = opened
        profile = (snapshot or {}).get("profile") or {}
        nickname = profile.get("nickname") or lead.name
        min_copy = profile.get("fixedAmountMinCopyUsd")
        min_copy_text = (
            f"{float(min_copy):g}"
            if isinstance(min_copy, (int, float)) and min_copy > 0
            else SLOT_MIN_COPY_FALLBACK
        )
        link = SLOT_COPY_LINK_TEMPLATE.format(portfolio_id=lead.portfolio_id)
        message = (
            f"【{nickname}】跟单名额空出来了！\n"
            f"当前跟单人数：{current}/{maximum}\n"
            f"最低跟单金额：{min_copy_text} U\n"
            f"马上抢：{link}"
        )
        cfg = None
        if lead.notification_config_json:
            try:
                lead_cfg = json.loads(lead.notification_config_json)
            except (ValueError, TypeError):
                lead_cfg = None
            if lead_cfg and any(bool(lead_cfg.get(f"{ch}_enabled"))
                                for ch in ("webhook", "telegram", "wecom", "dingtalk")):
                cfg = lead_cfg
        if cfg is None:
            cfg = await self.router._get_config(db, "notifications", DEFAULT_SYSTEM_CONFIG["notifications"])
        enqueue_notification(
            db, cfg, "MirrorQuant", message, event_type="LEAD_SLOT_OPEN",
            event_key=f"lead-slot-open:{lead.id}:{current}/{maximum}",
            lead_id=lead.id, destination_type="lead",
        )
        await db.commit()

    @serialized_execution
    async def _record_lead_sync_failure(self, db: AsyncSession, lead: LeadSource, started_ms: int, error: str) -> bool:
        await db.refresh(lead)
        last_sync_ms = lead.last_sync_at.replace(tzinfo=timezone.utc).timestamp() * 1000 if lead.last_sync_at else 0
        if last_sync_ms > started_ms:
            return False
        current = json.loads(lead.snapshot_json or "{}")
        if started_ms <= ((current.get("meta") or {}).get("storeRebuiltAtMs") or 0):
            return False
        if started_ms < ((current.get("meta") or {}).get("collectionStartedAtMs") or 0):
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
        error = redact_proxy_userinfo(error).strip() or "Sync failed (empty message)"
        now = time.monotonic()
        last_error = self._sync_failure_log_at.get(lead.id)
        error_window = self._sync_failure_error_window.setdefault(lead.id, [])
        # Keep the closed window boundary: even a window including both endpoints
        # must contain at most two ERRORs, across recovery/re-failure cycles too.
        error_window[:] = [at for at in error_window if now - at <= SYNC_FAILURE_ERROR_INTERVAL]
        error_due = last_error is None or now - last_error >= SYNC_FAILURE_ERROR_INTERVAL
        last_warn = self._sync_failure_warn_at.get(lead.id)
        if error_due and len(error_window) < 2:
            error_window.append(now)
            self._sync_failure_log_at[lead.id] = now
            self._sync_failure_warn_at[lead.id] = now
            log = logger.error
        elif last_warn is None or now - last_warn >= SYNC_FAILURE_WARN_INTERVAL:
            self._sync_failure_warn_at[lead.id] = now
            log = logger.warning
        else:
            log = logger.debug
        log("Lead %s sync failed; consecutiveFailures=%s snapshotAgeSec=%s: %s", lead.id, failures, age, error)
        if (overdue or exhausted) and not meta.get("syncFailureNotified"):
            event_key = f"lead-sync-failure:{lead.id}"
            # SQLite's JSON accessor reads TEXT directly; Postgres needs a cast.
            payload = (type_coerce(NotificationOutbox.payload, JSON) if db.get_bind().dialect.name == "sqlite"
                       else cast(NotificationOutbox.payload, JSON))
            latest = await db.scalar(select(NotificationOutbox).where(
                payload["event_key"].as_string() == event_key,
            ).order_by(NotificationOutbox.created_at.desc(), NotificationOutbox.id.desc()).limit(1))
            recovered = latest is None or (
                lead.last_sync_at is not None
                and lead.last_sync_at.replace(tzinfo=timezone.utc) > latest.created_at.replace(tzinfo=timezone.utc)
            )
            if recovered:
                config = await self.router._get_config(db, "notifications", DEFAULT_SYSTEM_CONFIG["notifications"])
                enqueue_notification(db, config, "Lead snapshot stale / sync failure",
                                     f"Lead {lead.id}: consecutiveFailures={failures}, snapshotAgeSec={age}. "
                                     "Snapshot is unverified; check lead sync health.", event_type="error",
                                     event_key=event_key)
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
        if (snapshot.get("openPositionsMeta") or {}).get("partial") is True:
            # A stale fallback retains the original holdings timestamp. Order
            # concurrent publications by collection time, not that old timestamp.
            incoming_observed = max(incoming_observed, started_ms)
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
        # Older callers omit detailOk; only an explicit degradation preserves the
        # incident. New collector snapshots always carry this success marker.
        partial = (snapshot["meta"].get("detailOk") is False
                   or (snapshot.get("openPositionsMeta") or {}).get("partial") is True)
        incoming_meta = snapshot.setdefault("openPositionsMeta", {})
        for field in SYNC_FAILURE_FIELDS:
            incoming_meta.pop(field, None)
            if partial and field in current_meta:
                incoming_meta[field] = current_meta[field]
        profile = snapshot.get("profile") or {}
        lead.avatar_url = profile.get("avatarUrl") or lead.avatar_url
        lead.description = profile.get("description") or lead.description
        lead.position_show = profile.get("positionShow")
        if not partial or profile.get("marginBalance") is not None:
            lead.margin_balance = float(profile.get("marginBalance") or 0.0)
        if not partial or profile.get("aumAmount") is not None:
            lead.aum_amount = float(profile.get("aumAmount") or 0.0)
        if not partial or profile.get("lastTradeTime") is not None:
            lead.last_trade_time = int(profile.get("lastTradeTime") or 0)
        if partial:
            reason = snapshot["meta"].get("detailError") or incoming_meta.get("error") or "snapshot unavailable"
            lead.last_error = "degraded: " + redact_proxy_userinfo(str(reason))[:470]
        else:
            # last_sync_at records full recovery, so a partial publication cannot
            # rearm an already delivered (or failed) outbox incident.
            # Use the attempt clock so subsequent failures are not falsely superseded.
            lead.last_sync_at = datetime.fromtimestamp(started_ms / 1000, tz=timezone.utc)
            lead.last_error = None
        lead.status = "running"

        lead.orders_store_json = json.dumps(orders_store, separators=(",", ":"))
        if snapshot.get("closed"):
            lead.closed_cache_json = json.dumps(snapshot["closed"], separators=(",", ":"))
        lead.snapshot_json = json.dumps(snapshot, separators=(",", ":"))
        await db.commit()

        if not partial:
            if current_meta.get("consecutiveSyncFailures") or lead.id in self._sync_failure_log_at:
                logger.info("Lead %s sync recovered", lead.id)
            self._sync_failure_log_at.pop(lead.id, None)
            # The rolling ERROR budget and WARNING timer survive recovery so
            # alternating successes/failures cannot flood either log level.

        return True

    async def _refresh_today_cash_flow(self, account: TradingAccount, adapter: Any) -> Optional[Dict[str, Any]]:
        """拉当日现货<->合约净划转，供看板算现金流调整后的今日盈亏。

        返回 {"net_flow", "date", "updated_at_ms"} 交 persist_equity 写入；
        已有新鲜记录时返回 None（不重复拉），拉不到时也返回 None（不改已存
        记录、不让同步失败）。拉不到且当天无任何记录时，看板今日盈亏为 null。
        """
        today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
        now_ms = int(time.time() * 1000)
        try:
            state = risk_state(account)
        except (ValueError, TypeError):
            state = {"assets": {}}
        if cash_flow_is_fresh(state, today, now_ms):
            return None
        try:
            net_flow = await fetch_today_net_flow(adapter, "USDT", today)
        except ExchangeAPIError as exc:
            if exc.code == "UNSUPPORTED_CASH_FLOW":
                logger.debug("Cash-flow history unsupported for account %s", account.id)
            else:
                logger.warning("Cash-flow fetch failed for account %s: %s",
                               account.id, redact_proxy_userinfo(str(exc))[:200])
            return None
        except Exception as exc:
            logger.warning("Cash-flow fetch failed for account %s: %s",
                           account.id, redact_proxy_userinfo(str(exc))[:200])
            return None
        return {"net_flow": net_flow, "date": today, "updated_at_ms": now_ms}

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
                account_id=account.id,  # 凭据或出口变化由工厂自动失效。
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

            bal = await get_collateral_balance(adapter)
            positions = await adapter.get_positions()
            cash_flow = await self._refresh_today_cash_flow(account, adapter)
            await persist_equity(db, account, bal, all_assets=True, cash_flow=cash_flow)
            equity_display = display_equity(bal)
            account.total_notional = sum(p.notional_usd for p in positions)
            account.unrealized_pnl = sum(p.upl for p in positions)
            account.positions_json = json.dumps([p.to_dict() for p in positions])
            account.last_sync_at = datetime.now(timezone.utc)
            account.last_error = None
            account.status = "connected"
            await db.commit()

            try:
                now_ms = int(time.time() * 1000)
                saved = await capture_equity_snapshot(
                    db,
                    account,
                    bal,
                    positions,
                    source="account_sync",
                    balance_as_of_ms=getattr(bal, "as_of_ms", None) or now_ms,
                    positions_as_of_ms=now_ms,
                    now_ms=now_ms,
                )
                if saved and isinstance(db, AsyncSession) and inspect(account, raiseerr=False) is not None:
                    await db.commit()
            except Exception as snap_exc:
                try:
                    await db.rollback()
                except Exception:
                    pass
                logger.warning(
                    "Account sync equity snapshot failed for %s: %s",
                    account.id,
                    redact_proxy_userinfo(str(snap_exc)),
                )

            await self.ws_hub.broadcast(
                {
                    "type": "account_update",
                    "accountId": account.id,
                    "ownerUserId": getattr(account, "owner_user_id", None),
                    "name": account.name,
                    "totalEquity": equity_display["totalEquity"],
                    "usdtEquity": equity_display["usdtEquity"],
                    "usdcEquity": equity_display["usdcEquity"],
                    "unrealizedPnl": account.unrealized_pnl,
                    "positions": [p.to_dict() for p in positions],
                }
            )
            return {
                "ok": True,
                **equity_display,
                "positions": [p.to_dict() for p in positions],
                "assetBalances": getattr(bal, "asset_balances", {}),
            }
        except Exception as exc:
            account.status = "error"
            account.last_error = redact_proxy_userinfo(str(exc))[:480]
            await db.commit()
            return {"ok": False, "error": account.last_error}


orchestrator = EngineOrchestrator()
