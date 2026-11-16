"""Trade notifications use source fills and allowlisted fields, never free-text names/errors."""
from __future__ import annotations

import math
import re
from datetime import timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Optional

# 折算基准本金；与前端仓位测算器的口径一致。
REFERENCE_CAPITAL = Decimal("1000")


def _decimal(value: Any, *, allow_zero: bool = False) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if number.is_finite() and (number > 0 or (allow_zero and number == 0)):
        return number
    return None


def _number(value: Any, *, allow_zero: bool = False) -> str:
    # 价格与数量保留原始精度，不做四舍五入。
    number = _decimal(value, allow_zero=allow_zero)
    return "未知" if number is None else format(number, "f")


def _money(value: Any) -> str:
    # 金额只看量级，统一保留两位小数，手机上一眼能读完。
    number = _decimal(value)
    return "未知" if number is None else f"{number.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,f} U"


def _symbol(value: Any) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z0-9_:/-]{1,64}", value) else "未知"


def _reference_line(notional: Any, lead_margin: Any) -> Optional[str]:
    """按带单员“本笔名义金额 / 保证金余额”的比例折算到 1000U 本金；缺任一数据就不显示，不估算。"""
    amount, margin = _decimal(notional), _decimal(lead_margin)
    if amount is None or margin is None:
        return None
    ratio = amount / margin
    # 建议杠杆沿用前端测算器：保证金占用不超过本金的 5%，限制在 2~50 倍。
    leverage = min(50, max(2, math.ceil(ratio / Decimal("0.05"))))
    return f"🧮 1000U 本金 ≈ 开 {_money(REFERENCE_CAPITAL * ratio)} ｜ 建议杠杆 {leverage}x"


def _latency_line(source: dict, record) -> Optional[str]:
    """带单成交时间到本条记录生成的间隔，便于核实跟单时效；缺时间或明显异常就不显示。"""
    try:
        lead_ms = int(source.get("endTime") or source.get("time") or 0)
        created = getattr(record, "created_at", None)
        if lead_ms <= 0 or created is None:
            return None
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)  # 数据库存的是不带时区的 UTC 时间
        seconds = int(created.timestamp() - lead_ms / 1000)
    except (TypeError, ValueError, AttributeError, OverflowError):
        return None
    if not 0 <= seconds < 86400:
        return None
    text = f"{seconds} 秒" if seconds < 120 else f"{seconds // 60} 分 {seconds % 60} 秒"
    return f"⏱ 距带单成交 {text}"


def _limit_lines(status: str, record, limit: dict) -> tuple[str, list[str]]:
    """滑点超限改挂限价单的结论行与明细行；只用数字字段，不输出自由文本。"""
    px = _number(limit.get("px"))
    requested, filled = _decimal(limit.get("requested")), _decimal(limit.get("filled"), allow_zero=True)
    progress = f"已成交 {_number(filled, allow_zero=True)} / 委托 {_number(requested)}"
    ttl = limit.get("ttl_sec")
    if status == "LIMIT_PLACED":
        expiry = f"{int(ttl)} 秒内未成交自动撤单" if isinstance(ttl, int) and ttl > 0 else "超时未成交自动撤单"
        detail = [f"📌 限价 {px} ｜ 数量 {_number(requested)} ｜ {expiry}"]
        drift = _decimal(getattr(record, "slippage_pct", None))
        if drift is not None:
            detail.append(f"📉 当前价偏离带单均价 {drift.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}%")
        return "⏳ 滑点超限，已改挂限价单（尚未成交）", detail
    if status == "LIMIT_EXPIRED":
        reason = {"timeout": "超时", "lead_exit": "带单已减仓/平仓"}.get(limit.get("reason"), "已失效")
        head = "⌛ 限价单未成交，已自动撤单" if not filled else "⌛ 限价单剩余部分已自动撤单"
        return f"{head}（{reason}）", [f"📌 限价 {px} ｜ {progress}"]
    if status == "PARTIAL":
        return "⚠️ 限价单部分成交，剩余继续挂单", [f"📌 限价 {px} ｜ {progress}"]
    # EXECUTED：订单已终结；成交量不足委托量说明剩余部分已被撤销。
    complete = requested is not None and filled is not None and filled >= requested
    return ("✅ 限价单已成交" if complete else "⚠️ 限价单部分成交，剩余已撤单"), [f"📌 限价 {px} ｜ {progress}"]


def trade_message(record, account, source: Optional[dict] = None, lead_margin: Any = None,
                  limit: Optional[dict] = None) -> str:
    """One durable message per account outcome, with the independent lead leg first.

    Follower close/guard actions cannot establish the lead's action. A source
    delivery supplies the original symbol/side/amount even if the follower uses
    a mapped instrument, a different close size, or has no local position.
    """
    source = source or {}
    opening = False
    if "_closed" in source:
        closed = source["_closed"]
        symbol, direction = closed.get("symbol"), closed.get("side")
        action = "平仓（历史持仓记录）"
        price, qty, notional = closed.get("avgClosePrice"), None, None
        # maxQty is peak inventory, not a confirmed independent closing fill.
    elif source:
        symbol, direction, side = source.get("symbol"), source.get("positionSide"), source.get("side")
        action = _lead_action(side, direction)
        opening = _is_opening(side, direction)
        price = source.get("avgPrice")
        qty = source.get("qtyExact", source.get("qty"))
        notional = source.get("notionalExact", source.get("notional"))
    else:
        symbol, direction = record.symbol, record.position_side
        price, qty, notional = record.lead_price, record.lead_qty, record.lead_notional
        # Autonomous follower exits/alerts contain no independent lead fill.
        action = _lead_action(record.side, direction) if _number(qty) != "未知" else "未知"
        opening = action != "未知" and _is_opening(record.side, direction)
        if action == "未知":
            symbol, direction, price, qty, notional = None, None, None, None, None
    # 第一行就是结论：图标 + 动作 + 币种 + 多空。
    icon, direction_text = {"LONG": ("🟢", "做多"), "SHORT": ("🔴", "做空")}.get(direction, ("❔", "方向未知"))
    lines = [
        f"{icon} 带单 {action} ｜ {_symbol(symbol)} ｜ {direction_text}",
        f"💰 均价 {_number(price)} ｜ 数量 {_number(qty)} ｜ 金额 {_money(notional)}",
    ]
    # 折算仓位只对开仓/加仓有意义，平仓不给“建议杠杆”。
    reference = _reference_line(notional, lead_margin) if opening else None
    if reference:
        lines.append(reference)
    latency = _latency_line(source, record)
    if latency:
        lines.append(latency)
    lead = "\n".join(lines)

    exchange = {"okx": "欧易", "binance": "Binance", "bybit": "Bybit", "bitget": "Bitget"}.get(
        str(account.exchange).lower(), "交易所未知")
    environment = {"demo": "模拟盘", "simulated": "模拟盘", "live": "实盘"}.get(
        str(account.environment).lower(), "环境未知")
    tag = f"账户 #{record.account_id} / 绑定 #{record.binding_id}（{exchange} {environment}）"
    status = record.status
    confirmed = bool(record.order_id) and _number(record.my_qty) != "未知"
    limit_detail: list[str] = []
    if limit and status in ("LIMIT_PLACED", "LIMIT_EXPIRED") or (
            limit and confirmed and status in ("EXECUTED", "PARTIAL")):
        result, limit_detail = _limit_lines(status, record, limit)
    elif status == "LIMIT_PLACED":
        result = "⏳ 滑点超限，已改挂限价单（尚未成交）"
    elif status == "LIMIT_EXPIRED":
        result = "⌛ 限价单未成交，已自动撤单"
    elif status == "EXECUTED" and confirmed:
        result = "✅ 已成交"
    elif status == "EXECUTED":
        result = "⚠️ 成交未确认"
    elif status.startswith("BLOCKED"):
        result = "🛑 风控拦截"
    elif status.startswith("SKIPPED"):
        result = "⏭️ 已跳过"
    elif status == "UNFILLED":
        result = "⚪ 未成交（无新增确认成交）"
    elif status == "ACCUMULATED_DEBT":
        result = "⏳ 累积待执行（未成交）"
    elif status == "FAILED":
        result = "❌ 执行失败，成交结果需核实"
    elif status == "PARTIAL":
        result = "⚠️ 部分成交，剩余结果未确认" if confirmed else "⚠️ 成交未确认"
    else:
        result = "⚠️ 结果未知，待核实"
    # Status codes are internal constants; omit raw diagnostic text and order IDs.
    safe_status = status if re.fullmatch(r"[A-Z_]{1,64}", status) else "UNKNOWN"
    follower_symbol = _symbol(getattr(record, "inst_id", record.symbol))
    follower = f"{result}\n👤 跟单{tag}\n🏷️ {follower_symbol} ｜ {safe_status}"
    if status in ("EXECUTED", "PARTIAL", "UNFILLED"):
        follower += (f"\n📦 数量 {_number(record.my_qty, allow_zero=True)}"
                     f" ｜ 均价 {_number(record.my_price) if confirmed else '未知'}"
                     f" ｜ 金额 {_money(record.my_notional) if confirmed else '未知'}")
    if limit_detail:
        follower += "".join(f"\n{line}" for line in limit_detail)
    return f"{lead}\n\n{follower}"


def _is_opening(side: Any, direction: Any) -> bool:
    return (str(side).upper(), str(direction).upper()) in (("BUY", "LONG"), ("SELL", "SHORT"))


def _lead_action(side: Any, direction: Any) -> str:
    # A fill establishes its leg, not whether either trader already held inventory.
    return {
        ("BUY", "LONG"): "开多/加多",
        ("SELL", "SHORT"): "开空/加空",
        ("SELL", "LONG"): "平多/减多",
        ("BUY", "SHORT"): "平空/减空",
    }.get((str(side).upper(), str(direction).upper()), "未知")
