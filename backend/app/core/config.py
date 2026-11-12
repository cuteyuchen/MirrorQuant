from __future__ import annotations

import base64
import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _ensure_secret_file(path: Path, generator) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        val = path.read_text(encoding="utf-8").strip()
        if val:
            return val
    val = generator()
    path.write_text(val, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return val


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
    cookie_name: str = "mq_access_token"
    cookie_secure: bool = False
    access_token_expire_minutes: int = 1440

    cors_origins: List[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]

    enable_background_engine: bool = True
    default_lead_poll_sec: int = 15
    price_tick_sec: int = 5

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors(cls, v):
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    def resolved_data_dir(self) -> Path:
        p = Path(self.data_dir).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        db_path = self.resolved_data_dir() / "mirrorquant.db"
        return f"sqlite+aiosqlite:///{db_path}"

    def resolved_master_key(self) -> str:
        if self.master_key:
            return self.master_key
        key_file = self.resolved_data_dir() / ".master_key"
        return _ensure_secret_file(
            key_file,
            lambda: base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"),
        )

    def resolved_jwt_secret(self) -> str:
        if self.jwt_secret:
            return self.jwt_secret
        jwt_file = self.resolved_data_dir() / ".jwt_secret"
        return _ensure_secret_file(jwt_file, lambda: secrets.token_urlsafe(48))


@lru_cache
def get_settings() -> Settings:
    return Settings()
