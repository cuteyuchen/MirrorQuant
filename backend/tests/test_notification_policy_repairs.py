"""通知业务确认、长文本和本地路径边界。"""
import json
import os
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core.config import get_settings
from app.core.notification_policy import notification_outbox_path
from app.core.secret_mask import MASK_SENTINEL
from app.schemas.api import NotificationSettings
from app.services.notifier.outbox import NotificationOutboxDrainer
from app.services.notifier.service import NotificationService


@pytest.mark.parametrize("channel", ["wecom", "dingtalk", "telegram"])
@pytest.mark.parametrize("valid", [False, True])
async def test_channel_body_must_confirm_success(monkeypatch, channel, valid):
    real_client = httpx.AsyncClient
    captured = []
    def respond(request):
        captured.append(request)
        return httpx.Response(200, json={"ok": valid, "errcode": 0 if valid else 400})
    monkeypatch.setattr("app.services.notifier.service.httpx.AsyncClient",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    config = {f"{channel}_enabled": True, f"{channel}_webhook_url": "https://example.invalid/webhook",
              "telegram_bot_token": "synthetic-token", "telegram_chat_id": "synthetic-chat"}
    result = await NotificationService.dispatch(config, "test", "x" * 5000)
    assert result[channel]["ok"] is valid
    if channel == "telegram":
        assert len(json.loads(captured[0].content)["text"]) == 4096


@pytest.mark.parametrize("channel,body", [
    ("wecom", {}), ("wecom", {"errcode": False}), ("wecom", {"errcode": "0"}),
    ("dingtalk", {}), ("dingtalk", {"errcode": False}), ("dingtalk", {"errcode": "0"}),
    ("telegram", {}), ("telegram", {"ok": 1}),
])
async def test_missing_or_wrong_type_business_acknowledgement_is_failure(monkeypatch, channel, body):
    # HTTP 200、假值或类型转换不能绕过机器人渠道的明确业务确认。
    real_client = httpx.AsyncClient
    monkeypatch.setattr("app.services.notifier.service.httpx.AsyncClient",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(
                            lambda request: httpx.Response(200, json=body)), **kwargs))
    config = {f"{channel}_enabled": True, f"{channel}_webhook_url": "https://example.invalid/webhook",
              "telegram_bot_token": "synthetic-token", "telegram_chat_id": "synthetic-chat"}
    assert (await NotificationService.dispatch(config, "test", "message"))[channel]["ok"] is False


@pytest.mark.parametrize("url", ["http://example.invalid/hook", "file:///tmp/hook", "https://user:pass@example.invalid/hook"])
def test_non_https_or_credential_bearing_webhook_rejected(url):
    with pytest.raises(ValueError, match="HTTPS"):
        NotificationSettings(webhook_url=url)
    assert NotificationSettings(webhook_url=MASK_SENTINEL).webhook_url == MASK_SENTINEL


@pytest.mark.parametrize("field,good,bad", [
    ("dingtalk_webhook_url", "https://oapi.dingtalk.com/robot/send?access_token=x",
     "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x"),
    ("wecom_webhook_url", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x",
     "https://oapi.dingtalk.com/robot/send?access_token=x"),
])
def test_channel_webhook_must_match_platform_host(field, good, bad):
    # 钉钉 URL 存进企业微信字段会在发送时静默走错渠道，保存时按域名拦下。
    assert getattr(NotificationSettings(**{field: good}), field) == good
    with pytest.raises(ValueError, match="must use host"):
        NotificationSettings(**{field: bad})
    assert getattr(NotificationSettings(**{field: MASK_SENTINEL}), field) == MASK_SENTINEL
    assert getattr(NotificationSettings(**{field: ""}), field) == ""


def test_outbox_must_remain_inside_data_directory(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr(get_settings(), "data_dir", str(root))
    assert notification_outbox_path("alerts/events.jsonl") == root / "alerts" / "events.jsonl"
    for bad in ("../outside.jsonl", str(tmp_path / "outside.jsonl"), str(root)):
        with pytest.raises(ValueError, match="inside the data directory"):
            notification_outbox_path(bad)
    (root / "escape").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        notification_outbox_path("escape/outside.jsonl")


def test_outbox_rejects_parent_symlink_swap_after_validation(tmp_path, monkeypatch):
    # 在路径校验后模拟目录被换成外部链接，不能越界创建或追加文件。
    root, outside = tmp_path / "data", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "alerts").mkdir()
    monkeypatch.setattr(get_settings(), "data_dir", str(root))
    real_open = os.open
    def swapped_open(path, flags, *args, **kwargs):
        if path == "alerts":
            (root / "alerts").rmdir()
            (root / "alerts").symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr("app.services.notifier.service.os.open", swapped_open)
    with pytest.raises(OSError):
        NotificationService._append_outbox(root / "alerts" / "events.jsonl", {"text": "test"})
    assert not (outside / "events.jsonl").exists()


async def test_default_retry_horizon_covers_minutes_and_stop_keeps_pending_backoff(monkeypatch):
    drainer = NotificationOutboxDrainer()
    assert drainer.max_attempts >= 8
    assert sum(min(300, 10 * 2 ** i) for i in range(drainer.max_attempts - 1)) >= 900
    drain = AsyncMock()
    monkeypatch.setattr(drainer, "drain_once", drain)
    await drainer.stop()
    drain.assert_not_awaited()  # 停机不能抹掉数分钟退避策略。
