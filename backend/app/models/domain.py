from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="admin", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    totp_secret_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    totp_pending_secret_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    session_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class LoginAttemptGuard(Base):
    __tablename__ = "login_attempt_guards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guard_key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False, index=True)
    key_type: Mapped[str] = mapped_column(String(16), nullable=False)  # ip | user
    key_value: Mapped[str] = mapped_column(String(128), nullable=False)
    fail_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_fail_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    alert_sent_level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class ProxyNode(Base):
    __tablename__ = "proxy_nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    url: Mapped[str] = mapped_column(String(255), nullable=False)  # e.g. http://127.0.0.1:7890
    region: Mapped[str] = mapped_column(String(32), default="AUTO", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_healthy: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    latency_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class LeadSource(Base):
    __tablename__ = "lead_sources"
    __table_args__ = (UniqueConstraint("platform", "portfolio_id", name="uq_lead_platform_portfolio"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), default="binance_bapi", nullable=False)
    portfolio_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    avatar_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    poll_interval_sec: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    proxy_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("proxy_nodes.id", ondelete="SET NULL"), nullable=True
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_public_dashboard: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    position_show: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    margin_balance: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    aum_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    last_trade_time: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="idle", nullable=False)

    snapshot_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    orders_store_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    closed_cache_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    seen_order_keys_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    seen_closed_ids_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    bindings: Mapped[list["CopyBinding"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan"
    )


class TradingAccount(Base):
    __tablename__ = "trading_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)  # okx | binance | bybit | bitget
    environment: Mapped[str] = mapped_column(String(16), default="demo", nullable=False)  # demo | live
    encrypted_credentials: Mapped[str] = mapped_column(Text, nullable=False)
    api_key_masked: Mapped[str] = mapped_column(String(64), nullable=False)
    proxy_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("proxy_nodes.id", ondelete="SET NULL"), nullable=True
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    margin_mode: Mapped[str] = mapped_column(String(16), default="cross", nullable=False)
    position_mode: Mapped[str] = mapped_column(String(16), default="hedge", nullable=False)

    usdt_equity: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    avail_equity: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    hwm_equity: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    day_start_equity: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    day_start_date: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    clock_offset_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_notional: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    positions_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    last_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="idle", nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    bindings: Mapped[list["CopyBinding"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class CopyBinding(Base):
    __tablename__ = "copy_bindings"
    __table_args__ = (UniqueConstraint("lead_id", "account_id", name="uq_binding_lead_account"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    lead_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("lead_sources.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("trading_accounts.id", ondelete="CASCADE"), nullable=False
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    risk_scale: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    max_total_leverage: Mapped[float] = mapped_column(Float, default=8.0, nullable=False)
    max_single_leverage: Mapped[float] = mapped_column(Float, default=4.0, nullable=False)
    max_instrument_leverage: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    max_drawdown_pct: Mapped[float] = mapped_column(Float, default=30.0, nullable=False)
    daily_max_loss_pct: Mapped[float] = mapped_column(Float, default=10.0, nullable=False)
    max_slippage_pct: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    max_spread_pct: Mapped[float] = mapped_column(Float, default=0.3, nullable=False)
    ioc_offset_pct: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    order_mode: Mapped[str] = mapped_column(String(32), default="ioc_then_market", nullable=False)

    tp_sl_mode: Mapped[str] = mapped_column(String(32), default="follow_lead", nullable=False)  # follow_lead | independent | hybrid_guard
    enable_stop_loss: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    stop_loss_pct: Mapped[float] = mapped_column(Float, default=5.0, nullable=False)
    enable_take_profit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    take_profit_pct: Mapped[float] = mapped_column(Float, default=10.0, nullable=False)
    enable_trailing_stop: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    trailing_callback_pct: Mapped[float] = mapped_column(Float, default=2.0, nullable=False)
    hard_stop_loss_pct: Mapped[float] = mapped_column(Float, default=15.0, nullable=False)

    symbol_whitelist_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    symbol_blacklist_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    symbol_map_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    circuit_breaker_tripped: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    circuit_breaker_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    baseline_seeded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    lead: Mapped["LeadSource"] = relationship(back_populates="bindings")
    account: Mapped["TradingAccount"] = relationship(back_populates="bindings")
    debts: Mapped[list["BindingDebt"]] = relationship(
        back_populates="binding", cascade="all, delete-orphan"
    )
    guards: Mapped[list["PositionGuardState"]] = relationship(
        back_populates="binding", cascade="all, delete-orphan"
    )
    allocations: Mapped[list["PositionAllocation"]] = relationship(
        back_populates="binding", cascade="all, delete-orphan"
    )


class PositionAllocation(Base):
    __tablename__ = "position_allocations"
    __table_args__ = (
        UniqueConstraint("binding_id", "inst_id", "pos_side", name="uq_pos_alloc_binding_inst_side"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    binding_id: Mapped[int] = mapped_column(Integer, ForeignKey("copy_bindings.id", ondelete="CASCADE"), nullable=False)
    account_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    inst_id: Mapped[str] = mapped_column(String(64), nullable=False)
    pos_side: Mapped[str] = mapped_column(String(16), nullable=False)
    owned_qty: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    entry_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    owned_qty_exact: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    entry_cost_exact: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # Generations keep old history/stop intents from closing a reopened allocation.
    generation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    opened_at_ms: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    binding: Mapped["CopyBinding"] = relationship(back_populates="allocations")


class BindingSourceProgress(Base):
    __tablename__ = "binding_source_progress"
    binding_id: Mapped[int] = mapped_column(Integer, ForeignKey("copy_bindings.id", ondelete="CASCADE"), primary_key=True)
    source_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    cumulative_qty: Mapped[str] = mapped_column(String(100), default="0", nullable=False)
    cumulative_notional: Mapped[str] = mapped_column(String(100), default="0", nullable=False)


class BindingDelivery(Base):
    __tablename__ = "binding_deliveries"
    binding_id: Mapped[int] = mapped_column(Integer, ForeignKey("copy_bindings.id", ondelete="CASCADE"), primary_key=True)
    source_event_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    source_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="PENDING", nullable=False)


class OrderIntent(Base):
    __tablename__ = "order_intents"
    __table_args__ = (UniqueConstraint("binding_id", "source_event_id", name="uq_intent_binding_source"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    binding_id: Mapped[int] = mapped_column(Integer, ForeignKey("copy_bindings.id", ondelete="RESTRICT"), nullable=False)
    account_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    inst_id: Mapped[str] = mapped_column(String(64), nullable=False)
    pos_side: Mapped[str] = mapped_column(String(16), nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(100), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_qty: Mapped[str] = mapped_column(String(100), nullable=False)
    filled_qty: Mapped[str] = mapped_column(String(100), default="0", nullable=False)
    avg_px: Mapped[str] = mapped_column(String(100), default="0", nullable=False)
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    target_usd: Mapped[str] = mapped_column(String(100), default="0", nullable=False)
    source_time_ms: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    exchange_order_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class PositionGuardState(Base):
    __tablename__ = "position_guard_states"
    __table_args__ = (
        UniqueConstraint("binding_id", "inst_id", "pos_side", name="uq_pos_guard_binding_inst_side"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    binding_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("copy_bindings.id", ondelete="CASCADE"), nullable=False
    )
    inst_id: Mapped[str] = mapped_column(String(64), nullable=False)
    pos_side: Mapped[str] = mapped_column(String(16), nullable=False)  # long | short
    entry_px: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    peak_px: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    trough_px: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    binding: Mapped["CopyBinding"] = relationship(back_populates="guards")


class BindingDebt(Base):
    __tablename__ = "binding_debts"
    __table_args__ = (
        UniqueConstraint("binding_id", "symbol", "position_side", name="uq_debt_binding_sym_side"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    binding_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("copy_bindings.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    position_side: Mapped[str] = mapped_column(String(16), nullable=False)  # LONG | SHORT
    pending_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    pending_usd_exact: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    binding: Mapped["CopyBinding"] = relationship(back_populates="debts")


class ExecutionRecord(Base):
    __tablename__ = "execution_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    binding_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("copy_bindings.id", ondelete="SET NULL"), nullable=True
    )
    lead_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    account_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    lead_name: Mapped[str] = mapped_column(String(120), nullable=False)
    account_name: Mapped[str] = mapped_column(String(120), nullable=False)

    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    inst_id: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    position_side: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    lead_qty: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    lead_price: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    lead_notional: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    my_qty: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    my_price: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    my_notional: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    slippage_pct: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    order_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # NULL means not yet reconciled, not a fabricated zero cash flow.
    fee: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # positive charge, negative rebate
    funding: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # positive receipt, negative payment
    exchange_fill_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    details: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Expiring claims permit recovery after a worker dies during delivery.
    claimed_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    username: Mapped[str] = mapped_column(String(64), default="system", nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    details: Mapped[str] = mapped_column(Text, default="", nullable=False)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class SystemConfig(Base):
    __tablename__ = "system_configs"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
