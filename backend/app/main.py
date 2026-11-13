from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text

from app.api import accounts, auth, bindings, dashboard, leads, proxies, settings as settings_api, ws
from app.core.config import get_settings
from app.core.database import get_session_factory, init_db
from app.core.security import CredentialVault
from app.models.domain import TradingAccount, User
from app.services.engine.orchestrator import orchestrator
from app.services.notifier.outbox import notification_outbox

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Provision local keys only before a new database is created. An existing
    # database with a missing key requires restoring its original key.
    settings.resolved_master_key()
    settings.resolved_jwt_secret()
    await init_db()
    vault = CredentialVault()
    async with get_session_factory()() as db:
        try:
            for encrypted in await db.scalars(select(TradingAccount.encrypted_credentials)):
                vault.decrypt_dict(encrypted)
            for user in await db.scalars(select(User)):
                for encrypted in (user.totp_secret_enc, user.totp_pending_secret_enc):
                    if encrypted:
                        vault.decrypt_string(encrypted)
        except Exception as exc:
            raise RuntimeError("Stored credentials cannot be decrypted; restore the correct master key") from exc
    if settings.enable_background_engine:
        await orchestrator.start()
    await notification_outbox.start()
    try:
        yield
    finally:
        try:
            await orchestrator.stop()
        finally:
            await notification_outbox.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title="MirrorQuant",
        version="1.0.0",
        description="Open-source multi-lead copy-trading & quantitative monitoring platform",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def strip_mq_prefix(request, call_next):
        if request.url.path.startswith("/mq/api/"):
            request.scope["path"] = request.url.path[3:]  # strip /mq
        return await call_next(request)

    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(leads.router)
    app.include_router(accounts.router)
    app.include_router(bindings.router)
    app.include_router(proxies.router)
    app.include_router(settings_api.router)
    app.include_router(ws.router)

    @app.get("/api/health")
    async def health():
        """Process liveness only; dependency health is reported by /api/ready."""
        return {"status": "ok", "app": settings.app_name, "version": "1.0.0"}

    @app.get("/api/ready")
    async def ready():
        checks = {}
        try:
            async with get_session_factory()() as db:
                await asyncio.wait_for(db.execute(text("SELECT 1")), timeout=settings.readiness_db_timeout_sec)
            checks["database"] = {"ok": True}
        except Exception:
            # Database exceptions may expose connection credentials.
            checks["database"] = {"ok": False, "error": "Database unavailable"}
        age = orchestrator.heartbeat_age_sec()
        checks["engine"] = {
            "ok": bool(settings.enable_background_engine and orchestrator.is_running and age is not None
                       and age <= settings.readiness_heartbeat_max_age_sec),
            "enabled": settings.enable_background_engine,
            "heartbeatAgeSec": round(age, 3) if age is not None else None,
            "maxAgeSec": settings.readiness_heartbeat_max_age_sec,
        }
        is_ready = all(check["ok"] for check in checks.values())
        return JSONResponse(status_code=200 if is_ready else 503, content={
            "status": "ready" if is_ready else "degraded", "checks": checks,
        })

    static_dir = Path(__file__).resolve().parent.parent / "static"
    if static_dir.exists() and (static_dir / "index.html").exists():
        assets_dir = static_dir / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        @app.get("/{full_path:path}")
        async def spa_fallback(full_path: str):
            root = static_dir.resolve()
            candidate = (root / full_path).resolve()
            if not candidate.is_relative_to(root):
                raise HTTPException(status_code=404, detail="Not found")
            if full_path and candidate.is_file():
                return FileResponse(str(candidate))
            return FileResponse(str(root / "index.html"))

    return app


app = create_app()
