from __future__ import annotations

import asyncio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import get_session_factory
from app.core.security import decode_access_token
from app.models.domain import User
from app.services.engine.orchestrator import orchestrator

router = APIRouter(tags=["ws"])


@router.websocket("/ws/stream")
async def websocket_stream(ws: WebSocket):
    settings = get_settings()
    origin = ws.headers.get("origin")
    if not origin or origin not in settings.cors_origins:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    token = ws.cookies.get(settings.cookie_name)
    payload = decode_access_token(token) if token else None
    if not payload or payload.get("typ") != "mq_access" or not payload.get("sub"):
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    async def authorized() -> bool:
        current = decode_access_token(token)
        if not current:
            return False
        async with get_session_factory()() as db:
            user = await db.scalar(select(User).where(User.id == int(current["sub"]), User.is_active.is_(True)))
            return user is not None and current.get("ver") == user.session_version

    if not await authorized():
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await orchestrator.ws_hub.connect(ws, validator=authorized)
    try:
        await ws.send_json({"type": "connected", "user": payload.get("username")})
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=5.0)
                if not await authorized():
                    await ws.close(code=status.WS_1008_POLICY_VIOLATION)
                    break
                if msg == "ping":
                    await ws.send_json({"type": "pong"})
            except asyncio.TimeoutError:
                if not await authorized():
                    await ws.close(code=status.WS_1008_POLICY_VIOLATION)
                    break
                await ws.send_json({"type": "heartbeat"})
    except WebSocketDisconnect:
        pass
    finally:
        await orchestrator.ws_hub.disconnect(ws)
