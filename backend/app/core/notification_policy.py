"""通知出口及本地文件的信任边界。"""
from pathlib import Path
from urllib.parse import urlsplit

from app.core.config import get_settings


def https_notification_url(value: str) -> str:
    if not value:
        return value
    parsed = urlsplit(value)
    # 禁止降级 HTTP、URL 凭据及控制字符，错误信息不回显密钥。
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or any(ord(char) < 32 for char in value)):
        raise ValueError("Notification webhook must be an HTTPS URL without userinfo")
    return value


def notification_outbox_path(value: str) -> Path:
    root = Path(get_settings().data_dir).expanduser().resolve()
    supplied = Path(value).expanduser()
    path = (supplied if supplied.is_absolute() else root / supplied).resolve()
    # resolve 同时拦截 .. 和现有符号链接指向数据目录之外。
    if path == root or not path.is_relative_to(root):
        raise ValueError("Notification outbox must be a file inside the data directory")
    return path
