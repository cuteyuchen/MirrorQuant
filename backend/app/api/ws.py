from __future__ import annotations

import asyncio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from app.core.config import get_settings
from app.core.security import decode_access_token
from app.services.engine.orchestrator import orchestrator

router = APIRouter(tags=["ws"])


@router.websocket("/ws/stream")
async def websocket_stream(ws: WebSocket):
    settings = get_settings()
    token = ws.cookies.get(settings.cookie_name) or ws.query_params.get("token")
    payload = decode_access_token(token) if token else None
    if not payload:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await orchestrator.ws_hub.connect(ws)
    try:
        await ws.send_json({"type": "connected", "user": payload.get("username")})
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=30.0)
                if msg == "ping":
                    await ws.send_json({"type": "pong"})
            except asyncio.TimeoutError:
                await ws.send_json({"type": "heartbeat"})
    except WebSocketDisconnect:
        pass
    finally:
        await orchestrator.ws_hub.disconnect(ws)
