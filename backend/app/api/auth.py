from __future__ import annotations

import hmac
import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    extract_client_ip,
    get_current_user,
    get_current_user_optional,
    record_audit,
)
from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import (
    CredentialVault,
    DUMMY_PASSWORD_HASH,
    build_totp_uri,
    create_access_token,
    create_captcha_challenge,
    generate_totp_secret,
    hash_password_async,
    totp_qr_svg_data_uri,
    verify_captcha_token,
    verify_password_async,
    verify_totp_code,
)
from app.models.domain import InviteCode, LoginAttemptGuard, SystemConfig, User
from app.schemas.api import (
    ChangePasswordRequest,
    LoginRequest,
    RegisterRequest,
    SetupRequest,
    TotpDisableRequest,
    TotpSetupRequest,
    TotpVerifyRequest,
    UserOut,
)
from app.services.notifier.outbox import enqueue_notification

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _set_auth_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.cookie_name,
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        max_age=settings.access_token_expire_minutes * 60,
        path="/",
    )


def _user_dict(u: User) -> dict:
    return UserOut(
        id=u.id,
        username=u.username,
        role=u.role,
        is_active=u.is_active,
        totp_enabled=bool(getattr(u, "totp_enabled", False)),
        created_at=u.created_at,
        last_login_at=u.last_login_at,
    ).model_dump(mode="json", by_alias=True)


async def _get_or_create_guard(
    db: AsyncSession, key_type: str, key_value: str
) -> LoginAttemptGuard:
    gkey = f"{key_type}:{key_value}"
    guard = await db.scalar(select(LoginAttemptGuard).where(LoginAttemptGuard.guard_key == gkey))
    if guard is None:
        guard = LoginAttemptGuard(
            guard_key=gkey,
            key_type=key_type,
            key_value=key_value[:128],
            fail_count=0,
            alert_sent_level=0,
        )
        db.add(guard)
        await db.flush()
    return guard


def _lock_duration_for_fails(fail_count: int) -> int:
    if fail_count >= 15:
        return 7200  # 2 hours
    if fail_count >= 10:
        return 1800  # 30 minutes
    if fail_count >= 5:
        return 300   # 5 minutes
    return 0


def _pair_value(username: str, client_ip: str) -> str:
    # 哈希组合键避免用户名或 IPv6 分隔符产生冲突。
    return hashlib.sha256(f"{username}\0{client_ip}".encode()).hexdigest()


def _effective_failures(guard: Optional[LoginAttemptGuard], now: datetime) -> int:
    if guard is None:
        return 0
    last = _as_utc(guard.last_fail_at)
    if last is None or (now - last).total_seconds() >= get_settings().login_attempt_window_sec:
        return 0
    return guard.fail_count


async def _emit_security_alert(
    db: AsyncSession,
    client_ip: str,
    username: str,
    fail_count: int,
    lock_sec: int,
) -> None:
    row = await db.scalar(select(SystemConfig).where(SystemConfig.key == "notifications"))
    cfg = {}
    if row:
        try:
            cfg = json.loads(row.value_json)
        except Exception:
            cfg = {}
    if not cfg.get("notify_on_security_alert", True):
        return
    title = "[MirrorQuant 安全告警] 检测到控制台登录爆破"
    msg = (
        f"[MirrorQuant 安全告警] 检测到控制台登录爆破，来源 IP: {client_ip}，"
        f"目标账号: {username}，连续失败 {fail_count} 次，已触发 {lock_sec // 60} 分钟封锁。"
    )
    enqueue_notification(db, cfg, title=title, message=msg, event_type="security_alert")


async def _record_login_failure(
    db: AsyncSession,
    request: Request,
    client_ip: str,
    username: str,
    reason: str,
    audit_action: str = "LOGIN_FAILED",
) -> tuple[int, int]:
    now = _utcnow()
    # 短写事务先取锁，计数更新不跨越口令哈希或延迟等待。
    await db.execute(update(SystemConfig).where(SystemConfig.key == "bootstrap_claimed")
                     .values(value_json=SystemConfig.value_json))
    ip_guard = await _get_or_create_guard(db, "ip", client_ip)
    user_guard = await _get_or_create_guard(db, "user", username or "__empty__")
    pair_guard = await _get_or_create_guard(db, "user_ip", _pair_value(username, client_ip))
    for guard in (ip_guard, user_guard, pair_guard):
        await db.refresh(guard)
        count = _effective_failures(guard, now)
        if count == 0:
            guard.alert_sent_level = 0
        guard.fail_count = count + 1
        guard.last_fail_at = now
        guard.last_ip = client_ip
        guard.last_username = username[:64]
    # 用户名只用于递增延迟；旧的用户名/IP 全局硬锁不再参与 admission。
    ip_guard.locked_until = user_guard.locked_until = None
    max_fails = pair_guard.fail_count
    lock_sec = _lock_duration_for_fails(max_fails)
    if lock_sec > 0:
        until = now + timedelta(seconds=lock_sec)
        pair_guard.locked_until = until

    await record_audit(
        db,
        "LOGIN_LOCKED" if lock_sec > 0 else audit_action,
        "user",
        None,
        f"{reason} (username={username}, fails={max_fails}, lockSec={lock_sec})",
        None,
        request,
    )

    if max_fails >= 15 and pair_guard.alert_sent_level < 15:
        pair_guard.alert_sent_level = 15
        await _emit_security_alert(db, client_ip, username, max_fails, lock_sec)

    await db.commit()
    return max_fails, lock_sec


@router.get("/captcha")
async def get_captcha():
    return create_captcha_challenge()


@router.get("/status")
async def auth_status(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    count = await db.scalar(select(func.count(User.id))) or 0
    client_ip = extract_client_ip(request)
    ip_guard = await db.scalar(
        select(LoginAttemptGuard).where(LoginAttemptGuard.guard_key == f"ip:{client_ip}")
    )
    now = _utcnow()
    lock_rem = 0
    captcha_required = False
    if ip_guard:
        # 未提供用户名的状态接口不能把某个组合键的锁扩散给整个 IP。
        captcha_required = _effective_failures(ip_guard, now) >= 3
    return {
        "initialized": count > 0,
        "authenticated": current_user is not None,
        "user": _user_dict(current_user) if current_user else None,
        "captchaRequired": captcha_required,
        "lockRemainingSec": lock_rem,
    }


@router.post("/setup", status_code=status.HTTP_201_CREATED)
async def setup_admin(
    payload: SetupRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    bootstrap_token: str = Header(default="", alias="X-MQ-Bootstrap-Token"),
):
    expected = get_settings().bootstrap_token
    if not expected or not hmac.compare_digest(bootstrap_token.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=403, detail="Bootstrap authorization required")
    # Claim before reading: SQLite must acquire its write lock before the
    # transaction observes setup state; all workers contend on the same row.
    claim = await db.execute(
        update(SystemConfig)
        .where(SystemConfig.key == "bootstrap_claimed", SystemConfig.value_json == "false")
        .values(value_json="true")
    )
    if claim.rowcount != 1:
        raise HTTPException(status_code=409, detail="System is already initialized")
    count = await db.scalar(select(func.count(User.id))) or 0
    if count > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="System is already initialized",
        )
    user = User(
        username=payload.username.strip(),
        password_hash=await hash_password_async(payload.password),
        role="admin",
        is_active=True,
        last_login_at=_utcnow(),
    )
    db.add(user)
    await db.flush()
    await record_audit(db, "SETUP_ADMIN", "user", str(user.id), f"Initialized admin {user.username}", user, request)
    await db.commit()
    await db.refresh(user)

    token = create_access_token(user.id, user.username, user.role, session_version=user.session_version)
    _set_auth_cookie(response, token)
    return {"user": _user_dict(user), "token": token}


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Invite-only registration. No open sign-up: a valid invite code is required.

    The code is consumed atomically in the same transaction as the user
    creation, so two concurrent registrations cannot both spend the last use.
    """
    code_text = payload.invite_code.strip()
    code = await db.scalar(select(InviteCode).where(InviteCode.code == code_text))
    now = _utcnow()
    if code is None:
        raise HTTPException(status_code=400, detail="Invalid invite code")
    if code.is_revoked:
        raise HTTPException(status_code=400, detail="Invite code has been revoked")
    if code.expires_at is not None and _as_utc(code.expires_at) <= now:
        raise HTTPException(status_code=400, detail="Invite code has expired")
    if code.used_count >= code.max_uses:
        raise HTTPException(status_code=400, detail="Invite code has been fully used")

    username = payload.username.strip()
    existing = await db.scalar(select(User).where(User.username == username))
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already taken")

    user = User(
        username=username,
        password_hash=await hash_password_async(payload.password),
        role="user",
        is_active=True,
        last_login_at=now,
    )
    db.add(user)
    await db.flush()
    # 原子消耗：条件更新只在仍有余量时 +1；并发抢完最后一格时回滚本次注册。
    consumed = await db.execute(
        update(InviteCode)
        .where(InviteCode.id == code.id, InviteCode.used_count < InviteCode.max_uses)
        .values(used_count=InviteCode.used_count + 1)
    )
    if consumed.rowcount != 1:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Invite code has been fully used")
    await record_audit(db, "REGISTER", "user", str(user.id),
                       f"Registered {username} with invite code {code.id}", user, request)
    await db.commit()
    await db.refresh(user)

    token = create_access_token(user.id, user.username, user.role, session_version=user.session_version)
    _set_auth_cookie(response, token)
    return {"user": _user_dict(user), "token": token}


@router.post("/login")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    client_ip = extract_client_ip(request)
    username = payload.username.strip()
    now = _utcnow()

    ip_guard = await db.scalar(
        select(LoginAttemptGuard).where(LoginAttemptGuard.guard_key == f"ip:{client_ip}")
    )
    user_guard = await db.scalar(
        select(LoginAttemptGuard).where(LoginAttemptGuard.guard_key == f"user:{username or '__empty__'}")
    )
    pair_guard = await db.scalar(select(LoginAttemptGuard).where(
        LoginAttemptGuard.guard_key == f"user_ip:{_pair_value(username, client_ip)}"))

    # 只有同一用户名与 IP 的组合能触发硬锁。
    active_locks = [
        dt
        for dt in (
            _as_utc(pair_guard.locked_until) if pair_guard else None,
        )
        if dt and dt > now
    ]
    if active_locks:
        until = max(active_locks)
        rem = max(1, int((until - now).total_seconds()))
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(rem)},
            content={
                "detail": f"Too many failed login attempts. Try again in {rem}s.",
                "code": "ACCOUNT_LOCKED",
                "lockRemainingSec": rem,
                "captchaRequired": True,
            },
        )

    # 2. Enforce captcha after 3 consecutive failures
    prior_fails = _effective_failures(pair_guard, now)
    username_fails = _effective_failures(user_guard, now)
    if username_fails:
        # 延迟有上限，不占全局执行锁，也不持续锁死正常管理员。
        await asyncio.sleep(min(5.0, 0.1 * 2 ** min(username_fails - 1, 6)))
    if prior_fails >= 3 or payload.captcha_token is not None or payload.captcha_answer is not None:
        if not payload.captcha_token or payload.captcha_answer is None or not str(payload.captcha_answer).strip():
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "detail": "Captcha verification required",
                    "code": "CAPTCHA_REQUIRED",
                    "captchaRequired": True,
                    "failCount": prior_fails,
                },
            )
        if not verify_captcha_token(payload.captcha_token, payload.captcha_answer):
            fails, lock_sec = await _record_login_failure(
                db, request, client_ip, username, "Invalid captcha", audit_action="LOGIN_CAPTCHA_FAILED"
            )
            if lock_sec > 0:
                return JSONResponse(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    headers={"Retry-After": str(lock_sec)},
                    content={
                        "detail": f"Too many failed login attempts. Locked for {lock_sec}s.",
                        "code": "ACCOUNT_LOCKED",
                        "lockRemainingSec": lock_sec,
                        "captchaRequired": True,
                        "failCount": fails,
                    },
                )
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "detail": "Invalid or expired captcha",
                    "code": "INVALID_CAPTCHA",
                    "captchaRequired": True,
                    "failCount": fails,
                },
            )

    # 3. Verify credentials
    # 已删除用户按账号不存在处理，不透露是已删。
    user = await db.scalar(select(User).where(User.username == username, User.deleted_at.is_(None)))
    valid_password = await verify_password_async(
        payload.password, user.password_hash if user else DUMMY_PASSWORD_HASH)
    if not user or not valid_password:
        fails, lock_sec = await _record_login_failure(
            db, request, client_ip, username, "Invalid credentials"
        )
        if lock_sec > 0:
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                headers={"Retry-After": str(lock_sec)},
                content={
                    "detail": f"Too many failed login attempts. Locked for {lock_sec}s.",
                    "code": "ACCOUNT_LOCKED",
                    "lockRemainingSec": lock_sec,
                    "captchaRequired": True,
                    "failCount": fails,
                },
            )
        resp_content = {
            "detail": "Invalid username or password",
            "code": "INVALID_CREDENTIALS",
            "captchaRequired": fails >= 3,
            "failCount": fails,
        }
        if fails >= 3:
            ch = create_captcha_challenge()
            resp_content["captchaRequired"] = True
            resp_content["captchaSvg"] = ch.get("svg")
            resp_content["captchaToken"] = ch.get("captchaToken")

        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content=resp_content,
        )

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account disabled")

    # 4. Enforce TOTP 2FA if enabled
    if user.totp_enabled and not user.totp_secret_enc:
        raise HTTPException(status_code=403, detail="2FA state corrupted; administrator recovery required")
    if user.totp_enabled:
        if not payload.totp_code or not payload.totp_code.strip():
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={
                    "detail": "Two-factor authentication code required",
                    "code": "TOTP_REQUIRED",
                    "totpRequired": True,
                    "captchaRequired": prior_fails >= 3,
                },
            )
        vault = CredentialVault()
        try:
            totp_secret = vault.decrypt_string(user.totp_secret_enc)
        except Exception as exc:
            raise HTTPException(status_code=403, detail="2FA state corrupted; administrator recovery required") from exc
        if not verify_totp_code(totp_secret, payload.totp_code):
            fails, lock_sec = await _record_login_failure(
                db, request, client_ip, username, "Invalid 2FA code", audit_action="LOGIN_2FA_FAILED"
            )
            if lock_sec > 0:
                return JSONResponse(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    headers={"Retry-After": str(lock_sec)},
                    content={
                        "detail": f"Too many failed attempts. Locked for {lock_sec}s.",
                        "code": "ACCOUNT_LOCKED",
                        "lockRemainingSec": lock_sec,
                        "captchaRequired": True,
                        "totpRequired": True,
                        "failCount": fails,
                    },
                )
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={
                    "detail": "Invalid 2FA verification code",
                    "code": "INVALID_TOTP",
                    "totpRequired": True,
                    "captchaRequired": fails >= 3,
                    "failCount": fails,
                },
            )

    # 5. Reset failure counters on clean login
    for guard in (ip_guard, user_guard, pair_guard):
        if guard:
            guard.fail_count = 0
            guard.locked_until = None
            guard.alert_sent_level = 0

    user.last_login_at = now
    await record_audit(db, "LOGIN", "user", str(user.id), "Admin logged in", user, request)
    await db.commit()

    token = create_access_token(user.id, user.username, user.role, session_version=user.session_version)
    _set_auth_cookie(response, token)
    return {"user": _user_dict(user), "token": token}


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    settings = get_settings()
    response.delete_cookie(key=settings.cookie_name, path="/")
    if current_user:
        await _revoke_sessions(db, current_user)
        await record_audit(db, "LOGOUT", "user", str(current_user.id), "Logged out", current_user, request)
        await db.commit()
    return {"ok": True}


@router.get("/me")
async def me(current_user: User = Depends(get_current_user)):
    return {"user": _user_dict(current_user)}


@router.post("/change-password")
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not await verify_password_async(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password incorrect")
    current_user.password_hash = await hash_password_async(payload.new_password)
    await _revoke_sessions(db, current_user)
    await record_audit(db, "CHANGE_PASSWORD", "user", str(current_user.id), "Password rotated", current_user, request)
    await db.commit()
    return {"ok": True}


@router.post("/2fa/setup")
async def setup_totp(
    payload: TotpSetupRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.totp_enabled:
        raise HTTPException(status_code=400, detail="Existing 2FA must be disabled with current code before re-enrolling")
    if not await verify_password_async(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="Current password incorrect")
    secret = generate_totp_secret()
    vault = CredentialVault()
    pending = vault.encrypt_dict({"secret": secret, "expires_at": int((_utcnow() + timedelta(minutes=10)).timestamp())})
    result = await db.execute(
        update(User)
        .where(User.id == current_user.id, User.totp_enabled.is_(False), User.session_version == current_user.session_version)
        .values(totp_pending_secret_enc=pending)
    )
    if result.rowcount != 1:
        raise HTTPException(status_code=409, detail="2FA state changed; sign in again")
    otpauth_uri = build_totp_uri(secret, current_user.username)
    qr_svg = totp_qr_svg_data_uri(otpauth_uri)
    await record_audit(db, "SETUP_2FA_INIT", "user", str(current_user.id), "Generated pending TOTP secret", current_user, request)
    await db.commit()
    return {
        "secret": secret,
        "otpauthUri": otpauth_uri,
        "qrSvgDataUri": qr_svg,
    }


@router.post("/2fa/enable")
async def enable_totp(
    payload: TotpVerifyRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.totp_enabled:
        raise HTTPException(status_code=400, detail="Existing 2FA must be disabled with current code before re-enrolling")
    if not current_user.totp_pending_secret_enc:
        raise HTTPException(status_code=400, detail="No pending 2FA setup found; generate QR first")
    vault = CredentialVault()
    pending_encrypted = current_user.totp_pending_secret_enc
    try:
        pending = vault.decrypt_dict(pending_encrypted)
        if pending["expires_at"] <= int(_utcnow().timestamp()):
            raise ValueError("Expired enrollment")
        pending_secret = pending["secret"]
    except Exception as exc:
        raise HTTPException(status_code=400, detail="2FA enrollment expired or invalid; generate QR again") from exc
    if not verify_totp_code(pending_secret, payload.code):
        raise HTTPException(status_code=400, detail="Invalid 6-digit verification code")
    result = await db.execute(
        update(User)
        .where(User.id == current_user.id, User.totp_enabled.is_(False), User.totp_pending_secret_enc == pending_encrypted,
               User.session_version == current_user.session_version)
        .values(totp_secret_enc=vault.encrypt_string(pending_secret), totp_pending_secret_enc=None,
                totp_enabled=True, session_version=User.session_version + 1)
    )
    if result.rowcount != 1:
        raise HTTPException(status_code=409, detail="2FA state changed; sign in again")
    await record_audit(db, "ENABLE_2FA", "user", str(current_user.id), "TOTP 2FA enabled", current_user, request)
    await db.commit()
    await db.refresh(current_user)
    return {"ok": True, "user": _user_dict(current_user)}


@router.post("/2fa/disable")
async def disable_totp(
    payload: TotpDisableRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not await verify_password_async(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="Current password incorrect")
    if current_user.totp_enabled:
        if not current_user.totp_secret_enc or not payload.code or not payload.code.strip():
            raise HTTPException(status_code=400, detail="Current 2FA verification code required")
        vault = CredentialVault()
        try:
            sec = vault.decrypt_string(current_user.totp_secret_enc)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="2FA state corrupted; administrator recovery required") from exc
        if not verify_totp_code(sec, payload.code.strip()):
            raise HTTPException(status_code=400, detail="Invalid 2FA verification code")
    result = await db.execute(
        update(User)
        .where(User.id == current_user.id, User.session_version == current_user.session_version,
               User.totp_enabled == current_user.totp_enabled, User.totp_secret_enc == current_user.totp_secret_enc)
        .values(totp_enabled=False, totp_secret_enc=None, totp_pending_secret_enc=None,
                session_version=User.session_version + 1)
    )
    if result.rowcount != 1:
        raise HTTPException(status_code=409, detail="2FA state changed; sign in again")
    await record_audit(db, "DISABLE_2FA", "user", str(current_user.id), "TOTP 2FA disabled", current_user, request)
    await db.commit()
    await db.refresh(current_user)
    return {"ok": True, "user": _user_dict(current_user)}


async def _revoke_sessions(db: AsyncSession, user: User) -> None:
    """Increment in SQL so concurrent security changes cannot lose revocations."""
    await db.execute(
        update(User).where(User.id == user.id).values(session_version=User.session_version + 1)
    )
