from __future__ import annotations

import base64
import fcntl
import json
import os
import secrets
import stat
from functools import lru_cache
from ipaddress import ip_network
from pathlib import Path
from typing import Annotated, Callable, List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _ensure_secret_file(path: Path, generator, may_create: Optional[Callable[[], bool]] = None) -> str:
    """Serialize creation across processes and never overwrite an existing key."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            fd = None
        if fd is not None:
            with os.fdopen(fd, "r", encoding="utf-8") as existing:
                if not stat.S_ISREG(os.fstat(existing.fileno()).st_mode):
                    raise RuntimeError("Secret path must be a regular file")
                value = existing.read().strip()
                if not value:
                    raise RuntimeError("Empty secret file; restore the correct key")
                os.fchmod(existing.fileno(), 0o600)
                return value
        if may_create is not None and not may_create():
            raise RuntimeError("Key missing for an existing database; restore the original key")
        value = generator()
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError("Secret generator returned an empty key")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(value)
            out.flush()
            os.fsync(out.fileno())
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MQ_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "MirrorQuant"
    env: str = "production"
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    data_dir: str = "./data"
    database_url: str = ""

    master_key: str = ""
    jwt_secret: str = ""
    bootstrap_token: str = ""
    cookie_name: str = "mq_access_token"
    cookie_secure: bool = False
    access_token_expire_minutes: int = 1440
    trusted_proxy_cidrs: Annotated[List[str], NoDecode] = ["127.0.0.0/8", "::1/128"]

    cors_origins: Annotated[List[str], NoDecode] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
    ]

    enable_background_engine: bool = True
    readiness_heartbeat_max_age_sec: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    readiness_db_timeout_sec: float = Field(default=2.0, gt=0, le=30, allow_inf_nan=False)
    notification_outbox_max_attempts: int = Field(default=3, ge=1, le=20)
    notification_outbox_poll_sec: float = Field(default=1.0, gt=0, le=60, allow_inf_nan=False)
    default_lead_poll_sec: int = 15
    price_tick_sec: int = 5
    pa_kline_max_staleness_multiplier: float = Field(default=3.0, gt=0, allow_inf_nan=False)
    allow_direct_egress: bool = False

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors(cls, v):
        if isinstance(v, str):
            if v.lstrip().startswith("["):
                return json.loads(v)
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    @field_validator("trusted_proxy_cidrs", mode="before")
    @classmethod
    def _parse_trusted_proxies(cls, value):
        if isinstance(value, str):
            value = json.loads(value) if value.lstrip().startswith("[") else value.split(",")
        return [str(ip_network(cidr.strip(), strict=False)) for cidr in value if cidr.strip()]

    @field_validator("bootstrap_token")
    @classmethod
    def _validate_bootstrap_token(cls, value: str) -> str:
        if value and len(value) < 32:
            raise ValueError("MQ_BOOTSTRAP_TOKEN must contain at least 32 random characters")
        return value

    def resolved_data_dir(self) -> Path:
        p = Path(self.data_dir).expanduser().resolve()
        p.mkdir(mode=0o700, parents=True, exist_ok=True)
        return p

    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        db_path = self.resolved_data_dir() / "mirrorquant.db"
        return f"sqlite+aiosqlite:///{db_path}"

    def resolved_master_key(self) -> str:
        if self.master_key:
            return self._validate_master_key(self.master_key)
        key_file = self.resolved_data_dir() / ".master_key"
        value = _ensure_secret_file(
            key_file,
            lambda: base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"),
            may_create=self._may_create_keys,
        )
        return self._validate_master_key(value)

    @staticmethod
    def _validate_master_key(value: str) -> str:
        try:
            raw = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
        except (ValueError, UnicodeError) as exc:
            raise ValueError("MQ_MASTER_KEY must encode 32 random bytes as URL-safe base64") from exc
        if len(raw) != 32:
            raise ValueError("MQ_MASTER_KEY must encode 32 random bytes as URL-safe base64")
        return value

    def _may_create_keys(self) -> bool:
        """Missing keys are only generated for an empty local installation."""
        from sqlalchemy.engine import make_url

        url = make_url(self.resolved_database_url())
        if not url.drivername.startswith("sqlite"):
            return False  # An external database must use provisioned keys.
        if not url.database or url.database == ":memory:":
            return True
        database = Path(url.database).expanduser().resolve()
        return not database.exists() or database.stat().st_size == 0

    def resolved_jwt_secret(self) -> str:
        if self.jwt_secret:
            return self.jwt_secret
        jwt_file = self.resolved_data_dir() / ".jwt_secret"
        return _ensure_secret_file(jwt_file, lambda: secrets.token_urlsafe(48), may_create=self._may_create_keys)


@lru_cache
def get_settings() -> Settings:
    return Settings()
