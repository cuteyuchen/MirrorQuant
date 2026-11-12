from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class SetupRequest(CamelModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(CamelModel):
    username: str
    password: str


class ChangePasswordRequest(CamelModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class UserOut(CamelModel):
    id: int
    username: str
    role: str
    is_active: bool
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
    exchange: str = Field(pattern="^(okx|binance)$")
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
    max_slippage_pct: float = Field(default=1.0, gt=0.0, le=50.0)
    ioc_offset_pct: float = Field(default=0.5, ge=0.0, le=10.0)
    order_mode: str = Field(default="ioc_then_market", pattern="^(ioc_then_market|ioc_only|market_only)$")
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
    max_slippage_pct: Optional[float] = Field(default=None, gt=0.0, le=50.0)
    ioc_offset_pct: Optional[float] = Field(default=None, ge=0.0, le=10.0)
    order_mode: Optional[str] = Field(default=None, pattern="^(ioc_then_market|ioc_only|market_only)$")
    symbol_whitelist: Optional[List[str]] = None
    symbol_blacklist: Optional[List[str]] = None
    symbol_map: Optional[Dict[str, str]] = None


class KillSwitchRequest(CamelModel):
    enabled: bool


class SettingsUpdate(CamelModel):
    global_kill_switch: Optional[bool] = None
    public_monitoring_enabled: Optional[bool] = None
    notifications: Optional[Dict[str, Any]] = None
    risk_defaults: Optional[Dict[str, Any]] = None
