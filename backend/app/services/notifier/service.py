from __future__ import annotations

import logging
from typing import Any, Dict

import httpx

logger = logging.getLogger("mirrorquant.notifier")


class NotificationService:
    """Async multi-channel dispatcher (Webhook, Telegram Bot, WeCom/DingTalk)."""

    @staticmethod
    async def dispatch(config: Dict[str, Any], title: str, message: str, event_type: str = "info") -> Dict[str, Any]:
        results: Dict[str, Any] = {}
        full_text = f"[{title}]\n{message}"

        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            if config.get("webhook_enabled") and config.get("webhook_url"):
                try:
                    resp = await client.post(
                        config["webhook_url"],
                        json={"title": title, "message": message, "eventType": event_type},
                    )
                    results["webhook"] = {"ok": resp.status_code < 400, "status": resp.status_code}
                except Exception as exc:
                    results["webhook"] = {"ok": False, "error": str(exc)}

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
                    results["telegram"] = {"ok": False, "error": str(exc)}

            if config.get("wecom_enabled") and config.get("wecom_webhook_url"):
                try:
                    resp = await client.post(
                        config["wecom_webhook_url"],
                        json={"msgtype": "text", "text": {"content": full_text}},
                    )
                    results["wecom"] = {"ok": resp.status_code < 400, "status": resp.status_code}
                except Exception as exc:
                    results["wecom"] = {"ok": False, "error": str(exc)}

        return results
