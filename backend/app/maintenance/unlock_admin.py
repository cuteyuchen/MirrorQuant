"""带外清理管理员登录限制，不修改口令、二次验证或会话。"""
from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import or_, select, update

from app.core.database import get_session_factory, reset_db_engine
from app.models.domain import AuditLog, LoginAttemptGuard, User


async def unlock_admin(username: str, ip: str | None = None) -> int:
    async with get_session_factory()() as db:
        user = await db.scalar(select(User).where(User.username == username, User.role == "admin"))
        if user is None:
            raise ValueError("Admin not found")
        # 新旧版本的组合键和用户名延迟均可恢复；可选限定受影响 IP。
        selected = or_(
            LoginAttemptGuard.guard_key == f"user:{username}",
            (LoginAttemptGuard.last_username == username) &
            (LoginAttemptGuard.last_ip == ip if ip else LoginAttemptGuard.last_ip.is_not(None)),
        )
        result = await db.execute(update(LoginAttemptGuard).where(selected).values(
            fail_count=0, locked_until=None, last_fail_at=None, alert_sent_level=0))
        db.add(AuditLog(username="maintenance", action="UNLOCK_ADMIN_LOGIN", resource_type="user",
                        resource_id=str(user.id), details="Out-of-band login guard reset", ip_address=ip))
        await db.commit()
        return result.rowcount


async def _run(args) -> None:
    try:
        count = await unlock_admin(args.username, args.ip)
        print(f"Unlocked {count} login guard(s); credentials unchanged.")
    finally:
        await reset_db_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True)
    parser.add_argument("--ip", help="Only clear this IP's pair locks and the username delay")
    args = parser.parse_args()
    if not 1 <= len(args.username) <= 64:
        parser.error("username length must be 1..64")
    try:
        asyncio.run(_run(args))
    except Exception as exc:
        # 数据库异常可能含连接信息，仅输出类型。
        parser.exit(1, f"Unlock failed ({type(exc).__name__}); database or admin unavailable.\n")


if __name__ == "__main__":
    main()
