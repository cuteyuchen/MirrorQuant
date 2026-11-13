from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class SetupRequest(CamelModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(CamelModel):
    username: str
    password: str
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


class ProxyCreate(CamelModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=3, max_length=255)
    region: str = Field(default="AUTO", max_length=32)
    priority: int = Field(default=10, ge=1, le=1000)
    is_enabled: bool = True


class ProxyUpdate(CamelModel):
    name: Optional[str] = None
    url: Optional[str] = None
    region: Optional[str] = None
    priority: Optional[int] = Field(default=None, ge=1, le=1000)
    is_enabled: Optional[bool] = None


class LeadCreate(CamelModel):
    name: str = Field(min_length=1, max_length=120)
    platform: str = Field(default="binance_bapi", pattern="^(binance_bapi)$")
    portfolio_id: str = Field(min_length=3, max_length=100)
    description: Optional[str] = None
    poll_interval_sec: int = Field(default=15, ge=5, le=3600)
    proxy_id: Optional[int] = None
    is_enabled: bool = True
    is_public_dashboard: bool = False


class LeadUpdate(CamelModel):
    name: Optional[str] = None
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


class AccountUpdate(CamelModel):
    name: Optional[str] = None
    environment: Optional[str] = Field(default=None, pattern="^(demo|live)$")
    api_key: Optional[str] = None
    secret_key: Optional[str] = None
    passphrase: Optional[str] = None
    proxy_id: Optional[int] = None
    is_enabled: Optional[bool] = None
    margin_mode: Optional[str] = Field(default=None, pattern="^(cross|isolated)$")
    position_mode: Optional[str] = Field(default=None, pattern="^(hedge|one_way)$")


class BindingCreate(CamelModel):
    name: str = Field(min_length=1, max_length=120)
    lead_id: int
    account_id: int
    is_enabled: bool = True
    risk_scale: float = Field(default=1.0, gt=0.0, le=20.0)
    max_total_leverage: float = Field(default=8.0, gt=0.0, le=100.0)
    max_single_leverage: float = Field(default=4.0, gt=0.0, le=100.0)
    max_instrument_leverage: int = Field(default=20, ge=1, le=125)
    max_drawdown_pct: float = Field(default=30.0, gt=0.0, le=100.0)
    daily_max_loss_pct: float = Field(default=10.0, gt=0.0, le=100.0)
    max_slippage_pct: float = Field(default=1.0, gt=0.0, le=50.0)
    max_spread_pct: float = Field(default=0.3, ge=0.0, le=20.0)
    ioc_offset_pct: float = Field(default=0.5, ge=0.0, le=10.0)
    order_mode: str = Field(default="ioc_then_market", pattern="^(ioc_then_market|ioc_only|market_only)$")
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


class BindingUpdate(CamelModel):
    name: Optional[str] = None
    is_enabled: Optional[bool] = None
    risk_scale: Optional[float] = Field(default=None, gt=0.0, le=20.0)
    max_total_leverage: Optional[float] = Field(default=None, gt=0.0, le=100.0)
    max_single_leverage: Optional[float] = Field(default=None, gt=0.0, le=100.0)
    max_instrument_leverage: Optional[int] = Field(default=None, ge=1, le=125)
    max_drawdown_pct: Optional[float] = Field(default=None, gt=0.0, le=100.0)
    daily_max_loss_pct: Optional[float] = Field(default=None, gt=0.0, le=100.0)
    max_slippage_pct: Optional[float] = Field(default=None, gt=0.0, le=50.0)
    max_spread_pct: Optional[float] = Field(default=None, ge=0.0, le=20.0)
    ioc_offset_pct: Optional[float] = Field(default=None, ge=0.0, le=10.0)
    order_mode: Optional[str] = Field(default=None, pattern="^(ioc_then_market|ioc_only|market_only)$")
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
    max_total_leverage: float = Field(default=8.0, gt=0.0, le=100.0)
    max_single_leverage: float = Field(default=4.0, gt=0.0, le=100.0)
    max_instrument_leverage: int = Field(default=20, ge=1, le=125)
    max_drawdown_pct: float = Field(default=30.0, gt=0.0, le=100.0)
    daily_max_loss_pct: float = Field(default=10.0, gt=0.0, le=100.0)
    max_slippage_pct: float = Field(default=1.0, gt=0.0, le=50.0)
    max_spread_pct: float = Field(default=0.3, ge=0.0, le=20.0)
    ioc_offset_pct: float = Field(default=0.5, ge=0.0, le=10.0)
    order_mode: Literal["ioc_then_market", "ioc_only", "market_only"] = "ioc_then_market"
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
    # Read-only hints are accepted on a GET/PUT round trip and never persisted.
    telegram_bot_token_configured: Optional[StrictBool] = Field(default=None, exclude=True)
    webhook_url_configured: Optional[StrictBool] = Field(default=None, exclude=True)
    wecom_webhook_url_configured: Optional[StrictBool] = Field(default=None, exclude=True)
    dingtalk_webhook_url_configured: Optional[StrictBool] = Field(default=None, exclude=True)
    outbox_enabled: StrictBool = False
    outbox_path: str = ""
    notify_on_execution: StrictBool = True
    notify_on_lead_order: StrictBool = True
    notify_on_risk_block: StrictBool = True
    notify_on_security_alert: StrictBool = True


class SettingsUpdate(CamelModel):
    model_config = ConfigDict(extra="forbid")
    global_kill_switch: Optional[StrictBool] = None
    public_monitoring_enabled: Optional[StrictBool] = None
    notifications: Optional[NotificationSettings] = None
    risk_defaults: Optional[RiskDefaults] = None
    resilience: Optional[ResilienceSettings] = None
