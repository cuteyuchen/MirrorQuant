from __future__ import annotations

import asyncio
import base64
import errno
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]
    import msvcrt
import hashlib
import hmac
import json
import logging
import os
import time
import re
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any, Dict
from urllib.parse import quote_plus

import httpx

from app.core.notification_policy import https_notification_url, notification_outbox_path
from app.core.config import get_settings

logger = logging.getLogger("mirrorquant.notifier")
_notification_http = ContextVar("notification_http", default=False)


class _NotificationHTTPFilter(logging.Filter):
    def filter(self, record):
        if _notification_http.get():
            event = record.getMessage().split(" ", 1)[0]
            record.msg = event if re.fullmatch(r"[a-z_]+\.(started|complete|failed)", event) else "Notification HTTP transport activity"
            record.args = ()
            record.stack_info = None
            record.exc_text = None
            if record.exc_info:
                record.msg += f" ({record.exc_info[0].__name__})"
                record.exc_info = None
        return True


for transport_logger in ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2",
                         "httpcore.proxy", "httpcore.socks"):
    logging.getLogger(transport_logger).addFilter(_NotificationHTTPFilter())


def _private_http_logs(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        token = _notification_http.set(True)
        try:
            return await function(*args, **kwargs)
        finally:
            _notification_http.reset(token)
    return wrapped


def dingtalk_signed_url(url: str, secret: str, now_ms: int | None = None) -> str:
    """钉钉机器人“加签”安全设置：用密钥对“时间戳 + 换行 + 密钥”做 HMAC-SHA256 后附在地址上。"""
    timestamp = int(time.time() * 1000) if now_ms is None else now_ms
    digest = hmac.new(secret.encode("utf-8"), f"{timestamp}\n{secret}".encode("utf-8"), hashlib.sha256).digest()
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}timestamp={timestamp}&sign={quote_plus(base64.b64encode(digest))}"


class NotificationService:
    """Async multi-channel dispatcher (Webhook, Telegram Bot, WeCom/DingTalk, JSONL Outbox)."""

    @staticmethod
    def _append_outbox(path: Path, record: Dict[str, Any]) -> None:
        path = notification_outbox_path(str(path))  # 写入时再次验证，不能绕过 schema。
        root = Path(get_settings().data_dir).expanduser().resolve()
        relative = path.relative_to(root)
        if fcntl is not None:
            # 逐级持有目录句柄且禁止符号链接，避免校验后父目录被替换导致越界写入。
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for part in relative.parts[:-1]:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=directory)
                    except FileExistsError:
                        pass
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                    os.close(directory)
                    directory = child
                fd = os.open(relative.name, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
            finally:
                os.close(directory)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
            return
        if root.is_symlink():
            raise OSError(errno.ELOOP, "Symlink not allowed", str(root))
        cur = root
        for part in relative.parts[:-1]:
            cur = cur / part
            if cur.exists() and cur.is_symlink():
                raise OSError(errno.ELOOP, "Symlink not allowed", str(cur))
            cur.mkdir(mode=0o700, exist_ok=True)
        if path.exists() and path.is_symlink():
            raise OSError(errno.ELOOP, "Symlink not allowed", str(path))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            while True:
                try:
                    os.lseek(f.fileno(), 0, os.SEEK_SET)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.01)
            try:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            finally:
                try:
                    os.lseek(f.fileno(), 0, os.SEEK_SET)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass

    @staticmethod
    @_private_http_logs
    async def dispatch(config: Dict[str, Any], title: str, message: str, event_type: str = "info") -> Dict[str, Any]:
        results: Dict[str, Any] = {}
        clean_msg = message.strip()
        full_text = f"[{title}]\n{clean_msg}" if title else clean_msg

        if config.get("outbox_enabled") and config.get("outbox_path"):
            try:
                outbox_file = notification_outbox_path(str(config["outbox_path"]).strip())
                await asyncio.to_thread(NotificationService._append_outbox, outbox_file, {
                    "text": clean_msg,
                    "title": title,
                    "eventType": event_type,
                    "ts": int(time.time() * 1000),
                })
                results["outbox"] = {"ok": True, "path": str(outbox_file)}
            except Exception as exc:
                results["outbox"] = {"ok": False, "error": type(exc).__name__}

        try:
            await NotificationService._dispatch_http(config, title, message, full_text, event_type, results)
        except Exception as exc:
            for channel in ("webhook", "telegram", "wecom", "dingtalk"):
                if config.get(f"{channel}_enabled"):
                    results[channel] = {"ok": False, "error": type(exc).__name__}
        return results

    @staticmethod
    async def _dispatch_http(config, title, message, full_text, event_type, results):
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0), follow_redirects=False, trust_env=False) as client:
            if config.get("webhook_enabled") and config.get("webhook_url"):
                try:
                    resp = await client.post(
                        https_notification_url(config["webhook_url"]),
                        json={"title": title, "message": message, "eventType": event_type},
                    )
                    results["webhook"] = {"ok": 200 <= resp.status_code < 300, "status": resp.status_code}
                except Exception as exc:
                    results["webhook"] = {"ok": False, "error": type(exc).__name__}

            if config.get("telegram_enabled") and config.get("telegram_bot_token") and config.get("telegram_chat_id"):
                token = config["telegram_bot_token"].strip()
                chat_id = config["telegram_chat_id"].strip()
                try:
                    resp = await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": full_text[:4096]},  # Telegram 文本上限。
                    )
                    body = resp.json()
                    results["telegram"] = {
                        "ok": 200 <= resp.status_code < 300 and isinstance(body, dict) and body.get("ok") is True,
                        "status": resp.status_code,
                    }
                except Exception as exc:
                    results["telegram"] = {"ok": False, "error": type(exc).__name__}

            if config.get("wecom_enabled") and config.get("wecom_webhook_url"):
                try:
                    resp = await client.post(
                        https_notification_url(config["wecom_webhook_url"]),
                        json={"msgtype": "text", "text": {"content": full_text}},
                    )
                    body = resp.json()
                    code = body.get("errcode") if isinstance(body, dict) else None
                    results["wecom"] = {
                        "ok": 200 <= resp.status_code < 300 and type(code) is int and code == 0,
                        "status": resp.status_code,
                    }
                    if type(code) is int:
                        # 回执码是平台的固定数字，便于在设置页直接定位失败原因。
                        results["wecom"]["errcode"] = code
                except Exception as exc:
                    results["wecom"] = {"ok": False, "error": type(exc).__name__}

            if config.get("dingtalk_enabled") and config.get("dingtalk_webhook_url"):
                try:
                    target = https_notification_url(config["dingtalk_webhook_url"])
                    secret = str(config.get("dingtalk_secret") or "").strip()
                    if secret:
                        # 配了加签密钥就每次重新签名；没配则依赖关键词或 IP 白名单。
                        target = dingtalk_signed_url(target, secret)
                    resp = await client.post(
                        target,
                        json={"msgtype": "text", "text": {"content": full_text}},
                    )
                    body = resp.json()
                    code = body.get("errcode") if isinstance(body, dict) else None
                    valid_code = type(code) is int
                    results["dingtalk"] = {
                        "ok": 200 <= resp.status_code < 300 and valid_code and code == 0,
                        "status": resp.status_code,
                    }
                    if valid_code:
                        results["dingtalk"]["errcode"] = code
                    else:
                        results["dingtalk"]["error"] = "InvalidDingTalkResponse"
                except Exception as exc:
                    results["dingtalk"] = {"ok": False, "error": type(exc).__name__}

        return results
