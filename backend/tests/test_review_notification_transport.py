import json
import logging

import httpcore
import httpx
import pytest
from httpcore._backends.base import AsyncNetworkBackend, AsyncNetworkStream

from app.services.notifier.service import NotificationService


SECRETS = ("FAKE_PATH_SECRET", "FAKE_QUERY_SECRET", "FAKE_CHAT_ADDRESS", "private-notification.invalid",
           "api.telegram.org", "FAKE_LOCATION_SECRET")


def configuration(channel):
    config = {f"{channel}_enabled": True}
    if channel == "telegram":
        config.update(telegram_bot_token=SECRETS[0], telegram_chat_id=SECRETS[2])
    else:
        config["webhook_url" if channel == "webhook" else f"{channel}_webhook_url"] = (
            f"https://{SECRETS[3]}/{SECRETS[0]}?token={SECRETS[1]}")
    return config


class SimulatedStream(AsyncNetworkStream):
    def __init__(self, failure):
        self.failure = failure

    async def read(self, max_bytes, timeout=None):
        if self.failure:
            raise httpcore.ReadTimeout(" ".join(SECRETS))
        body = b'{"errcode":0,"ok":true}'  # 同时满足企业机器人和 Telegram 的业务成功协议。
        return (b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode()
                + b"\r\nLocation: https://private-notification.invalid/FAKE_LOCATION_SECRET\r\n\r\n" + body)

    async def write(self, buffer, timeout=None):
        pass

    async def aclose(self):
        pass

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return self

    def get_extra_info(self, info):
        return None


class SimulatedBackend(AsyncNetworkBackend):
    def __init__(self, failure=False):
        self.failure = failure

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        return SimulatedStream(self.failure)


@pytest.mark.parametrize("channel", ["webhook", "telegram", "dingtalk", "wecom"])
@pytest.mark.parametrize("failure", [False, True])
async def test_actual_httpcore_debug_is_private_and_unrelated_logs_survive(monkeypatch, caplog, channel, failure):
    real_client = httpx.AsyncClient
    transport = httpx.AsyncHTTPTransport()
    transport._pool = httpcore.AsyncConnectionPool(network_backend=SimulatedBackend(failure))
    monkeypatch.setattr("app.services.notifier.service.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs))
    with caplog.at_level(logging.DEBUG):
        result = await NotificationService.dispatch(configuration(channel), "test", "test")
        logging.getLogger("httpcore.connection").debug("Unrelated transport address public.invalid remains visible")
        async with real_client(transport=httpx.MockTransport(lambda request: httpx.Response(204))) as client:
            await client.get("https://public.invalid/visible")
    assert result[channel]["ok"] is not failure
    assert any(record.name.startswith("httpcore.") and "receive_response_headers" in record.getMessage()
               for record in caplog.records)
    for secret in SECRETS:
        assert secret not in caplog.text
        assert secret not in json.dumps(result)
    assert "Unrelated transport address public.invalid remains visible" in caplog.text
    assert "https://public.invalid/visible" in caplog.text


@pytest.mark.parametrize("channel", ["webhook", "telegram", "dingtalk", "wecom"])
@pytest.mark.parametrize("phase", ["constructor", "entry", "exit"])
async def test_client_lifecycle_exceptions_never_escape_with_secrets(monkeypatch, caplog, channel, phase):
    def fail():
        logging.getLogger("httpcore.connection").debug("client.failed exception=%r", RuntimeError(" ".join(SECRETS)))
        raise RuntimeError(" ".join(SECRETS))

    class Client:
        def __init__(self, **kwargs):
            if phase == "constructor":
                fail()

        async def __aenter__(self):
            if phase == "entry":
                fail()
            return self

        async def __aexit__(self, *args):
            if phase == "exit":
                fail()

        async def post(self, *args, **kwargs):
            return httpx.Response(200, json={"errcode": 0, "ok": True})

    monkeypatch.setattr("app.services.notifier.service.httpx.AsyncClient", Client)
    with caplog.at_level(logging.DEBUG):
        try:
            result = await NotificationService.dispatch(configuration(channel), "test", "test")
        except Exception as exc:
            pytest.fail(f"Unsanitized client lifecycle exception escaped: {exc}")
    assert result[channel] == {"ok": False, "error": "RuntimeError"}
    for secret in SECRETS:
        assert secret not in caplog.text and secret not in json.dumps(result)
