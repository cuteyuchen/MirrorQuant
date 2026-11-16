"""Only synthetic URLs/tokens, intercepted offline; no notification is sent."""
import logging
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.notifier.service import NotificationService


async def test_http_client_log_does_not_expose_notification_destination(monkeypatch, caplog):
    real_client = httpx.AsyncClient
    monkeypatch.setattr("app.services.notifier.service.httpx.AsyncClient", lambda **kwargs:
        real_client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"errcode": 0})), **kwargs))
    with caplog.at_level(logging.INFO, logger="httpx"):
        result = await NotificationService.dispatch({"dingtalk_enabled": True,
            "dingtalk_webhook_url": "https://simulation.invalid/robot/send?access_token=SYNTHETIC_HTTP_TOKEN"}, "test", "test")
    assert result["dingtalk"]["ok"] is True
    assert "SYNTHETIC_HTTP_TOKEN" not in caplog.text
    assert "https://simulation.invalid/robot" not in caplog.text


@pytest.mark.parametrize("channel", ["webhook", "telegram", "wecom", "dingtalk"])
@pytest.mark.parametrize("outcome", ["success", "http_error", "exception"])
async def test_all_channel_destinations_private_without_changing_delivery(monkeypatch, caplog, channel, outcome):
    path_secret = "SYNTHETIC_PATH_SECRET"
    query_secret = "SYNTHETIC_QUERY_SECRET"
    destination = f"https://simulation.invalid/{path_secret}/send?token={query_secret}"
    config = {f"{channel}_enabled": True}
    if channel == "telegram":
        config.update(telegram_bot_token=path_secret, telegram_chat_id="synthetic-chat")
    else:
        config["webhook_url" if channel == "webhook" else f"{channel}_webhook_url"] = destination
    requests = []

    def deliver(request):
        requests.append(request)
        if outcome == "exception":
            try:
                raise httpx.ReadTimeout(f"Synthetic timeout at {request.url}", request=request)
            except httpx.ReadTimeout:
                logging.getLogger("httpx").exception("Synthetic client failure at %s", request.url)
                raise
        # HTTP 成功还必须包含相应渠道的业务确认。
        return httpx.Response(200 if outcome == "success" else 503, json={"errcode": 0, "ok": True})

    real_client = httpx.AsyncClient
    monkeypatch.setattr("app.services.notifier.service.httpx.AsyncClient", lambda **kwargs:
        real_client(transport=httpx.MockTransport(deliver), **kwargs))
    with caplog.at_level(logging.INFO):
        result = await NotificationService.dispatch(config, "synthetic-title", "synthetic-message")
        logging.getLogger("mirrorquant.engine").info("Unrelated application info remains visible")
        async with real_client(transport=httpx.MockTransport(lambda request: httpx.Response(204))) as client:
            await client.get("https://public.invalid/visible")
    assert len(requests) == 1
    assert requests[0].method == "POST"
    payload = json.loads(requests[0].content)
    if channel == "webhook":
        assert payload == {"title": "synthetic-title", "message": "synthetic-message", "eventType": "info"}
    elif channel == "telegram":
        assert payload == {"chat_id": "synthetic-chat", "text": "[synthetic-title]\nsynthetic-message"}
    else:
        assert payload == {"msgtype": "text", "text": {"content": "[synthetic-title]\nsynthetic-message"}}
    assert result[channel]["ok"] is (outcome == "success")
    if outcome == "exception":
        assert result[channel]["error"] == "ReadTimeout"
    else:
        assert result[channel]["status"] == (200 if outcome == "success" else 503)
    assert path_secret not in caplog.text
    assert query_secret not in caplog.text
    assert "simulation.invalid" not in caplog.text
    assert "api.telegram.org/bot" not in caplog.text
    assert "Unrelated application info remains visible" in caplog.text
    assert "https://public.invalid/visible" in caplog.text


async def test_dingtalk_business_failure_is_private_and_not_acknowledged(monkeypatch, caplog):
    real_client = httpx.AsyncClient
    monkeypatch.setattr("app.services.notifier.service.httpx.AsyncClient", lambda **kwargs:
        real_client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "errcode": 310000, "errmsg": "SYNTHETIC_RESPONSE_SECRET"})), **kwargs))
    with caplog.at_level(logging.INFO):
        result = await NotificationService.dispatch({"dingtalk_enabled": True,
            "dingtalk_webhook_url": "https://simulation.invalid/SYNTHETIC_PATH?key=SYNTHETIC_QUERY"}, "test", "test")
    assert result["dingtalk"] == {"ok": False, "status": 200, "errcode": 310000}
    for secret in ("SYNTHETIC_PATH", "SYNTHETIC_QUERY", "SYNTHETIC_RESPONSE_SECRET"):
        assert secret not in caplog.text
