from __future__ import annotations

from datetime import datetime
import os
from typing import Any, ClassVar, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_serializer, field_validator
from pydantic.alias_generators import to_camel

from app.core.time_util import to_utc_iso
from app.core.notification_policy import https_notification_url, notification_outbox_path
from app.core.secret_mask import MASK_SENTINEL, is_mask_sentinel


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class UpdateModel(CamelModel):
    # 省略字段代表不改；显式 null 仅允许用于数据库可空列。
    nullable_fields: ClassVar[frozenset[str]] = frozenset()

    @field_validator("*")
    @classmethod
    def reject_null_column(cls, value, info):
        if value is None and info.field_name not in cls.nullable_fields:
            raise ValueError("Field cannot be null; omit it to keep the current value")
        return value


class SetupRequest(CamelModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(CamelModel):
    username: str = Field(min_length=1, max_length=64)  # 与用户表长度一致，限制恶意输入。
    password: str = Field(max_length=128)
    captcha_token: Optional[str] = None
    captcha_answer: Optional[str] = None
    totp_code: Optional[str] = None


class ChangePasswordRequest(CamelModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class TotpVerifyRequest(CamelModel):
    code: str = Field(min_length=6, max_length=12)


class TotpSetupRequest(CamelModel):
    current_password: str


class TotpDisableRequest(CamelModel):
    current_password: str
    code: Optional[str] = None


class UserOut(CamelModel):
    id: int
    username: str
    role: str
    is_active: bool
    totp_enabled: bool = False
    created_at: datetime
    last_login_at: Optional[datetime] = None

    @field_serializer("created_at", "last_login_at", when_used="json")
    def serialize_time(self, value: Optional[datetime]) -> Optional[str]:
        return to_utc_iso(value)


class ProxyCreate(CamelModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=3, max_length=255)
    region: str = Field(default="AUTO", max_length=32)
    priority: int = Field(default=10, ge=1, le=1000)
    is_enabled: bool = True


class ProxyUpdate(UpdateModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    url: Optional[str] = Field(default=None, min_length=3, max_length=255)
    region: Optional[str] = Field(default=None, max_length=32)
    priority: Optional[int] = Field(default=None, ge=1, le=1000)
    is_enabled: Optional[bool] = None

    @field_validator("url", mode="before")
    @classmethod
    def normalize_preserved_url(cls, value):
        # 空串是保留旧地址及凭据的占位符，转换后仍执行原长度和 null 校验。
        return MASK_SENTINEL if value == "" else value


class LeadCreate(CamelModel):
    name: str = Field(min_length=1, max_length=120)
    platform: str = Field(default="binance_bapi", pattern="^(binance_bapi)$")
    portfolio_id: str = Field(min_length=3, max_length=100)
    description: Optional[str] = None
    poll_interval_sec: int = Field(default=15, ge=5, le=3600)
    proxy_id: Optional[int] = None
    is_enabled: bool = True
    is_public_dashboard: bool = False


class LeadUpdate(UpdateModel):
    nullable_fields = frozenset({"description", "proxy_id"})
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    description: Optional[str] = None
    poll_interval_sec: Optional[int] = Field(default=None, ge=5, le=3600)
    proxy_id: Optional[int] = None
    is_enabled: Optional[bool] = None
    is_public_dashboard: Optional[bool] = None


class AccountCreate(CamelModel):
    name: str = Field(min_length=1, max_length=120)
    exchange: str = Field(pattern="^(okx|binance|bybit|bitget)$")
    environment: str = Field(default="demo", pattern="^(demo|live)$")
    api_key: str = Field(min_length=4, max_length=256)
    secret_key: str = Field(min_length=4, max_length=256)
    passphrase: Optional[str] = Field(default=None, max_length=256)
    proxy_id: Optional[int] = None
    is_enabled: bool = True
    margin_mode: str = Field(default="cross", pattern="^(cross|isolated)$")
    position_mode: str = Field(default="hedge", pattern="^(hedge|one_way)$")

    @field_validator("exchange")
    @classmethod
    def experimental_exchange_gate(cls, value):
        # 实验适配器必须由部署者显式开启，不受请求字段控制。
        if value in ("bybit", "bitget") and os.getenv("MQ_ENABLE_EXPERIMENTAL_EXCHANGES", "").lower() != "true":
            raise ValueError("Experimental exchange disabled; set MQ_ENABLE_EXPERIMENTAL_EXCHANGES=true to opt in")
        return value


class AccountUpdate(UpdateModel):
    nullable_fields = frozenset({"passphrase", "proxy_id"})
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    environment: Optional[str] = Field(default=None, pattern="^(demo|live)$")
    api_key: Optional[str] = Field(default=None, max_length=256)
    secret_key: Optional[str] = Field(default=None, max_length=256)
    passphrase: Optional[str] = Field(default=None, max_length=256)
    proxy_id: Optional[int] = None
    is_enabled: Optional[bool] = None
    margin_mode: Optional[str] = Field(default=None, pattern="^(cross|isolated)$")
    position_mode: Optional[str] = Field(default=None, pattern="^(hedge|one_way)$")

class BindingCreate(CamelModel):
    name: str = Field(min_length=1, max_length=120)
    lead_id: int
    account_id: int
    is_enabled: bool = True
    enable_circuit_breaker: bool = True
    enable_leverage_cap: bool = False
    risk_scale: float = Field(default=1.0, gt=0.0, le=20.0)
    max_total_leverage: float = Field(default=8.0, ge=0.0, le=100.0)
    max_single_leverage: float = Field(default=4.0, ge=0.0, le=100.0)
    max_instrument_leverage: int = Field(default=20, ge=1, le=125)
    max_drawdown_pct: float = Field(default=30.0, ge=0.0, le=100.0)
    daily_max_loss_pct: float = Field(default=10.0, ge=0.0, le=100.0)
    max_slippage_pct: float = Field(default=1.0, gt=0.0, le=50.0)
    max_spread_pct: float = Field(default=0.3, ge=0.0, le=20.0)
    ioc_offset_pct: float = Field(default=0.5, ge=0.0, le=10.0)
    order_mode: str = Field(default="ioc_then_market", pattern="^(ioc_then_market|ioc_only|market_only)$")
    # 滑点超限处理方式与限价单有效期（秒）。
    slippage_action: str = Field(default="block", pattern="^(block|limit)$")
    limit_order_ttl_sec: int = Field(default=120, ge=10, le=3600)
    tp_sl_mode: str = Field(default="follow_lead", pattern="^(follow_lead|independent|hybrid_guard)$")
    enable_stop_loss: bool = False
    stop_loss_pct: float = Field(default=5.0, gt=0.0, le=95.0)
    enable_take_profit: bool = False
    take_profit_pct: float = Field(default=10.0, gt=0.0, le=500.0)
    enable_trailing_stop: bool = False
    trailing_callback_pct: float = Field(default=2.0, gt=0.0, le=50.0)
    hard_stop_loss_pct: float = Field(default=15.0, gt=0.0, le=95.0)
    symbol_whitelist: List[str] = Field(default_factory=list)
    symbol_blacklist: List[str] = Field(default_factory=list)
    symbol_map: Dict[str, str] = Field(default_factory=dict)


class BindingUpdate(UpdateModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    is_enabled: Optional[bool] = None
    enable_circuit_breaker: Optional[bool] = None
    enable_leverage_cap: Optional[bool] = None
    risk_scale: Optional[float] = Field(default=None, gt=0.0, le=20.0)
    max_total_leverage: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    max_single_leverage: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    max_instrument_leverage: Optional[int] = Field(default=None, ge=1, le=125)
    max_drawdown_pct: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    daily_max_loss_pct: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    max_slippage_pct: Optional[float] = Field(default=None, gt=0.0, le=50.0)
    max_spread_pct: Optional[float] = Field(default=None, ge=0.0, le=20.0)
    ioc_offset_pct: Optional[float] = Field(default=None, ge=0.0, le=10.0)
    order_mode: Optional[str] = Field(default=None, pattern="^(ioc_then_market|ioc_only|market_only)$")
    slippage_action: Optional[str] = Field(default=None, pattern="^(block|limit)$")
    limit_order_ttl_sec: Optional[int] = Field(default=None, ge=10, le=3600)
    tp_sl_mode: Optional[str] = Field(default=None, pattern="^(follow_lead|independent|hybrid_guard)$")
    enable_stop_loss: Optional[bool] = None
    stop_loss_pct: Optional[float] = Field(default=None, gt=0.0, le=95.0)
    enable_take_profit: Optional[bool] = None
    take_profit_pct: Optional[float] = Field(default=None, gt=0.0, le=500.0)
    enable_trailing_stop: Optional[bool] = None
    trailing_callback_pct: Optional[float] = Field(default=None, gt=0.0, le=50.0)
    hard_stop_loss_pct: Optional[float] = Field(default=None, gt=0.0, le=95.0)
    symbol_whitelist: Optional[List[str]] = None
    symbol_blacklist: Optional[List[str]] = None
    symbol_map: Optional[Dict[str, str]] = None


class KillSwitchRequest(CamelModel):
    enabled: bool


class RiskDefaults(CamelModel):
    """The same bounds as binding creation, also applied to stored policies."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    enable_circuit_breaker: StrictBool = True
    enable_leverage_cap: StrictBool = False
    max_total_leverage: float = Field(default=8.0, ge=0.0, le=100.0)
    max_single_leverage: float = Field(default=4.0, ge=0.0, le=100.0)
    max_instrument_leverage: int = Field(default=20, ge=1, le=125)
    max_drawdown_pct: float = Field(default=30.0, ge=0.0, le=100.0)
    daily_max_loss_pct: float = Field(default=10.0, ge=0.0, le=100.0)
    max_slippage_pct: float = Field(default=1.0, gt=0.0, le=50.0)
    max_spread_pct: float = Field(default=0.3, ge=0.0, le=20.0)
    ioc_offset_pct: float = Field(default=0.5, ge=0.0, le=10.0)
    order_mode: Literal["ioc_then_market", "ioc_only", "market_only"] = "ioc_then_market"
    slippage_action: Literal["block", "limit"] = "block"
    limit_order_ttl_sec: int = Field(default=120, ge=10, le=3600)
    tp_sl_mode: Literal["follow_lead", "independent", "hybrid_guard"] = "follow_lead"
    enable_stop_loss: StrictBool = False
    stop_loss_pct: float = Field(default=5.0, gt=0.0, le=95.0)
    enable_take_profit: StrictBool = False
    take_profit_pct: float = Field(default=10.0, gt=0.0, le=500.0)
    enable_trailing_stop: StrictBool = False
    trailing_callback_pct: float = Field(default=2.0, gt=0.0, le=50.0)
    hard_stop_loss_pct: float = Field(default=15.0, gt=0.0, le=95.0)


class ResilienceSettings(CamelModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    missed_order_max_age_sec: int = Field(default=900, ge=10, le=3600)
    catchup_max_price_dev_pct: float = Field(default=0.5, gt=0, le=10)
    reconcile_interval_sec: int = Field(default=60, ge=15, le=600)
    auto_close_orphan_positions: Literal["alert_only", "auto_close"] = "alert_only"
    insufficient_margin_policy: Literal["auto_scale_down", "skip_alert"] = "auto_scale_down"
    flip_close_first: StrictBool = False  # Retained for config compatibility; chronology always wins.
    clock_sync_interval_sec: int = Field(default=300, ge=30, le=3600)
    max_consecutive_failures: int = Field(default=5, ge=1, le=100)


class NotificationSettings(CamelModel):
    model_config = ConfigDict(extra="forbid")
    webhook_enabled: StrictBool = False
    webhook_url: str = ""
    telegram_enabled: StrictBool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    wecom_enabled: StrictBool = False
    wecom_webhook_url: str = ""
    dingtalk_enabled: StrictBool = False
    dingtalk_webhook_url: str = ""
    # 钉钉“加签”密钥，可选；留空表示机器人用关键词或 IP 白名单。
    dingtalk_secret: str = ""
    # Read-only hints are accepted on a GET/PUT round trip and never persisted.
    telegram_bot_token_configured: Optional[StrictBool] = Field(default=None, exclude=True)
    webhook_url_configured: Optional[StrictBool] = Field(default=None, exclude=True)
    wecom_webhook_url_configured: Optional[StrictBool] = Field(default=None, exclude=True)
    dingtalk_webhook_url_configured: Optional[StrictBool] = Field(default=None, exclude=True)
    dingtalk_secret_configured: Optional[StrictBool] = Field(default=None, exclude=True)
    outbox_enabled: StrictBool = False
    outbox_path: str = ""
    notify_on_execution: StrictBool = True
    notify_on_lead_order: StrictBool = True
    notify_on_risk_block: StrictBool = True
    notify_on_security_alert: StrictBool = True

    @field_validator("webhook_url", "wecom_webhook_url", "dingtalk_webhook_url")
    @classmethod
    def validate_webhook(cls, value):
        # GET 返回的固定掩码在 PUT 中表示保留，真正发送前仍再次验证 HTTPS。
        return value if is_mask_sentinel(value) else https_notification_url(value)

    @field_validator("outbox_path")
    @classmethod
    def validate_outbox(cls, value):
        if value:
            notification_outbox_path(value)
        return value


class SettingsUpdate(CamelModel):
    model_config = ConfigDict(extra="forbid")
    global_kill_switch: Optional[StrictBool] = None
    public_monitoring_enabled: Optional[StrictBool] = None
    notifications: Optional[NotificationSettings] = None
    risk_defaults: Optional[RiskDefaults] = None
    resilience: Optional[ResilienceSettings] = None
