from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict

import httpx

logger = logging.getLogger("mirrorquant.notifier")


class NotificationService:
    """Async multi-channel dispatcher (Webhook, Telegram Bot, WeCom/DingTalk, JSONL Outbox)."""

    @staticmethod
    def _append_outbox(path: Path, record: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    @staticmethod
    async def dispatch(config: Dict[str, Any], title: str, message: str, event_type: str = "info") -> Dict[str, Any]:
        results: Dict[str, Any] = {}
        clean_msg = message.strip()
        full_text = f"[{title}]\n{clean_msg}" if title else clean_msg

        if config.get("outbox_enabled") and config.get("outbox_path"):
            outbox_file = Path(str(config["outbox_path"]).strip()).expanduser()
            try:
                await asyncio.to_thread(NotificationService._append_outbox, outbox_file, {
                    "text": clean_msg,
                    "title": title,
                    "eventType": event_type,
                    "ts": int(time.time() * 1000),
                })
                results["outbox"] = {"ok": True, "path": str(outbox_file)}
            except Exception as exc:
                results["outbox"] = {"ok": False, "error": type(exc).__name__}

        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            if config.get("webhook_enabled") and config.get("webhook_url"):
                try:
                    resp = await client.post(
                        config["webhook_url"],
                        json={"title": title, "message": message, "eventType": event_type},
                    )
                    results["webhook"] = {"ok": resp.status_code < 400, "status": resp.status_code}
                except Exception as exc:
                    results["webhook"] = {"ok": False, "error": type(exc).__name__}

            if config.get("telegram_enabled") and config.get("telegram_bot_token") and config.get("telegram_chat_id"):
                token = config["telegram_bot_token"].strip()
                chat_id = config["telegram_chat_id"].strip()
                try:
                    resp = await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat_id, "text": full_text},
                    )
                    results["telegram"] = {"ok": resp.status_code < 400, "status": resp.status_code}
                except Exception as exc:
                    results["telegram"] = {"ok": False, "error": type(exc).__name__}

            if config.get("wecom_enabled") and config.get("wecom_webhook_url"):
                try:
                    resp = await client.post(
                        config["wecom_webhook_url"],
                        json={"msgtype": "text", "text": {"content": full_text}},
                    )
                    results["wecom"] = {"ok": resp.status_code < 400, "status": resp.status_code}
                except Exception as exc:
                    results["wecom"] = {"ok": False, "error": type(exc).__name__}

            if config.get("dingtalk_enabled") and config.get("dingtalk_webhook_url"):
                try:
                    resp = await client.post(
                        config["dingtalk_webhook_url"],
                        json={"msgtype": "text", "text": {"content": full_text}},
                    )
                    results["dingtalk"] = {"ok": resp.status_code < 400, "status": resp.status_code}
                except Exception as exc:
                    results["dingtalk"] = {"ok": False, "error": type(exc).__name__}

        return results
