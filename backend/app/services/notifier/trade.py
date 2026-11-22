"""Trade notifications use source fills and allowlisted fields; lead names are
single-line sanitized, never raw free text or error output."""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Optional

from app.core.time_util import BEIJING_TZ

# 折算基准本金；与前端仓位测算器的口径一致。
REFERENCE_CAPITAL = Decimal("1000")

# 全局显示单位：通知与页面共用同一设置。notional 只显示金额，quantity 只显示
# 数量，both 两者都显示（历史版式）。默认值与 system_configs 的 notifications
# 字典默认值保持一致。
DISPLAY_UNIT_NOTIONAL = "notional"
DISPLAY_UNIT_QUANTITY = "quantity"
DISPLAY_UNIT_BOTH = "both"
DISPLAY_UNITS = (DISPLAY_UNIT_NOTIONAL, DISPLAY_UNIT_QUANTITY, DISPLAY_UNIT_BOTH)
DEFAULT_DISPLAY_UNIT = DISPLAY_UNIT_NOTIONAL


def resolve_display_unit(value: Any) -> str:
    """Normalize a stored display-unit value; unknown or missing values fall back to the default."""
    return value if value in DISPLAY_UNITS else DEFAULT_DISPLAY_UNIT


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


def _money(value: Any, *, allow_zero: bool = False) -> str:
    # 金额只看量级，统一保留两位小数，手机上一眼能读完。
    number = _decimal(value, allow_zero=allow_zero)
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


def _limit_lines(status: str, record, limit: dict, display_unit: str = DEFAULT_DISPLAY_UNIT) -> tuple[str, list[str]]:
    """滑点超限改挂限价单的结论行与明细行；只用数字字段，不输出自由文本。"""
    px = _number(limit.get("px"))
    requested, filled = _decimal(limit.get("requested")), _decimal(limit.get("filled"), allow_zero=True)
    px_value = _decimal(limit.get("px"))
    if display_unit == DISPLAY_UNIT_NOTIONAL and px_value is not None and requested is not None and filled is not None:
        progress = f"已成交 {_money(filled * px_value, allow_zero=True)} / 委托 {_money(requested * px_value, allow_zero=True)}"
    else:
        progress = f"已成交 {_number(filled, allow_zero=True)} / 委托 {_number(requested)}"
    ttl = limit.get("ttl_sec")
    if status == "LIMIT_PLACED":
        expiry = f"{int(ttl)} 秒内未成交自动撤单" if isinstance(ttl, int) and ttl > 0 else "超时未成交自动撤单"
        if display_unit == DISPLAY_UNIT_NOTIONAL and px_value is not None and requested is not None:
            size = f"金额 {_money(requested * px_value, allow_zero=True)}"
        else:
            size = f"数量 {_number(requested)}"
        detail = [f"📌 限价 {px} ｜ {size} ｜ {expiry}"]
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


_LEAD_PLATFORMS = {"binance_bapi": "币安"}

# 拦截/跳过原因只用状态码与严格解析的数字，绝不原样输出诊断自由文本。
_BLOCK_DRIFT_RE = re.compile(
    r"^Catch-up price drift ([0-9]+(?:\.[0-9]+)?)% > max ([0-9]+(?:\.[0-9]+)?)% \(age ([0-9]+(?:\.[0-9]+)?)s\)$")
_BLOCK_STALE_RE = re.compile(
    r"^Missed order stale: age ([0-9]+(?:\.[0-9]+)?)s > max ([0-9]+(?:\.[0-9]+)?)s$")
_BLOCK_SPREAD_RE = re.compile(
    r"^Orderbook spread ([0-9]+(?:\.[0-9]+)?)% exceeds guard ([0-9]+(?:\.[0-9]+)?)%$")
_BLOCK_SLIPPAGE_RE = re.compile(
    r"^Slippage ([0-9]+(?:\.[0-9]+)?)% exceeds limit ([0-9]+(?:\.[0-9]+)?)%$")
_BLOCK_REASONS: dict[str, tuple[str, str]] = {
    "BLOCKED_CATCHUP_PRICE": ("补跟价格缺失或无效，无法核对偏离，未跟单",
                              "Catch-up reference or lead price missing or invalid; drift check impossible, order not placed"),
    "BLOCKED_CATCHUP_CONFIG": ("补跟风控参数配置无效，未跟单",
                               "Catch-up guard misconfigured; order not placed"),
    "BLOCKED_CIRCUIT_BREAKER": ("熔断已触发，暂停开仓", "Circuit breaker tripped; opening suspended"),
    "BLOCKED_KILL_SWITCH": ("急停开关已开启", "Kill switch engaged"),
    "BLOCKED_DAILY_LOSS": ("触及单日亏损上限", "Daily loss limit reached"),
    "BLOCKED_DRAWDOWN": ("触及回撤上限", "Drawdown limit reached"),
    "BLOCKED_INSUFFICIENT_MARGIN": ("保证金不足", "Insufficient margin"),
    "BLOCKED_COLLATERAL_BALANCE": ("抵押品余额不足或异常", "Collateral balance insufficient or abnormal"),
    "BLOCKED_LEVERAGE": ("杠杆超出上限", "Leverage exceeds the limit"),
    "BLOCKED_SLIPPAGE": ("滑点超出上限", "Slippage exceeded the limit"),
    "BLOCKED_SPREAD": ("盘口价差过大", "Order-book spread too wide"),
    "BLOCKED_FILTER": ("未通过订单过滤器（最小下单量或价格精度）",
                       "Order filter rejected (minimum size or price precision)"),
    "BLOCKED_RESTING_LIMIT": ("已有同方向限价单在簿等待，未重复下单",
                              "A resting limit order for the same direction already exists; no duplicate order placed"),
    "BLOCKED_SOURCE_ORDERING": ("带单员订单顺序证据不足，暂缓执行",
                                "Source ordering evidence insufficient; execution deferred"),
    "BLOCKED_ALLOCATION_DEFICIT": ("仓位分配账目不足，暂缓执行",
                                   "Position allocation deficit; execution deferred"),
    "BLOCKED_ALLOCATION_MISMATCH": ("仓位分配账目不一致，暂缓执行",
                                    "Position allocation mismatch; execution deferred"),
    "BLOCKED_ORPHAN_DETECTED": ("检测到孤儿仓位，暂缓执行", "Orphan position detected; execution deferred"),
    "BLOCKED_ORPHAN_CLOSE_EVIDENCE": ("孤儿仓位平仓证据不足，暂缓执行",
                                      "Orphan close evidence insufficient; execution deferred"),
    "BLOCKED_PROTECTION_UNAVAILABLE": ("风控保护数据不可用，暂缓执行",
                                       "Risk protection data unavailable; execution deferred"),
    "SKIPPED_BELOW_MIN": ("数量低于最小下单量", "Quantity below the minimum order size"),
    "EXIT_DUST_ABSORBED": ("退出剩余量低于最小下单量，已核销完成", "Exit remainder below the minimum order size; absorbed to complete delivery"),
    "SKIPPED_CLOSED_CYCLE": ("闭环事件已处理，无需重复执行", "Closed-cycle event already processed; nothing to execute"),
    "SKIPPED_NO_LOCAL_POS": ("本地无对应仓位", "No local position to act on"),
    "SKIPPED_UNSUPPORTED": ("不支持的品种或方向", "Unsupported instrument or side"),
    "ADOPTED_POSITION": ("现有仓位与带单员持仓一致（敞口比率差异在容差内），已认领为跟单仓位",
                         "Existing position matches the lead's within the exposure tolerance; adopted as a followed position"),
    "FAILED": ("执行失败，成交结果需核实", "Execution failed; fill result needs verification"),
    "ACCUMULATED_DEBT": ("数量不足，累积待后续补足", "Quantity insufficient; accumulated for a later top-up"),
}


def _duration_text(seconds: float) -> tuple[str, str]:
    total = int(seconds)
    if total < 120:
        return f"{total} 秒", f"{total}s"
    if total < 3600:
        return f"{total // 60} 分 {total % 60} 秒", f"{total // 60}m {total % 60}s"
    if total < 86400:
        return f"{total // 3600} 小时 {(total % 3600) // 60} 分", f"{total // 3600}h {(total % 3600) // 60}m"
    return f"{total // 86400} 天 {(total % 86400) // 3600} 小时", f"{total // 86400}d {(total % 86400) // 3600}h"


def _block_reason(status: str, record) -> Optional[str]:
    """拦截/跳过原因的双语说明行；无已知解释的状态返回 None，不编造。"""
    if status == "BLOCKED_CATCHUP_DRIFT":
        match = _BLOCK_DRIFT_RE.fullmatch(str(getattr(record, "details", "") or ""))
        if match is not None:
            dev, maximum, age = match.groups()
            zh_age, en_age = _duration_text(float(age))
            return (f"📋 原因：补跟价格漂移 {dev}% 超过上限 {maximum}%（信号已过去 {zh_age}），未追单 / "
                    f"Catch-up price drift {dev}% exceeded the {maximum}% limit "
                    f"(signal age {en_age}); order not placed")
        return "📋 原因：补跟价格漂移超过上限，未追单 / Catch-up price drift exceeded the limit; order not placed"
    if status == "SKIPPED_STALE_MISSED_ORDER":
        match = _BLOCK_STALE_RE.fullmatch(str(getattr(record, "details", "") or ""))
        if match is not None:
            age, maximum = match.groups()
            zh_age, en_age = _duration_text(float(age))
            zh_max, en_max = _duration_text(float(maximum))
            return (f"📋 原因：欠单已过期，信号距今 {zh_age}，超过上限 {zh_max} / "
                    f"Missed order stale: signal age {en_age} exceeded the {en_max} limit")
        return "📋 原因：欠单已过期，超过追单时效上限 / Missed order stale: catch-up age limit exceeded"
    if status == "BLOCKED_SPREAD":
        match = _BLOCK_SPREAD_RE.fullmatch(str(getattr(record, "details", "") or ""))
        if match is not None:
            measured, guard = match.groups()
            return (f"📋 原因：盘口价差过大，实测 {measured}%，上限 {guard}% / "
                    f"Order-book spread too wide (measured {measured}%, guard {guard}%)")
    if status == "BLOCKED_SLIPPAGE":
        match = _BLOCK_SLIPPAGE_RE.fullmatch(str(getattr(record, "details", "") or ""))
        if match is not None:
            measured, limit = match.groups()
            return (f"📋 原因：滑点超出上限，实测 {measured}%，上限 {limit}% / "
                    f"Slippage exceeded the limit (measured {measured}%, limit {limit}%)")
    reason = _BLOCK_REASONS.get(status)
    if reason is None:
        return None
    zh, en = reason
    if status in ("BLOCKED_SLIPPAGE", "BLOCKED_SPREAD"):
        measured = _decimal(getattr(record, "slippage_pct", None))
        if measured is not None:
            shown = measured.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            zh = f"{zh}（实测 {shown}%）"
            en = f"{en} (measured {shown}%)"
    return f"📋 原因：{zh} / {en}"


def _lead_header(lead: Any, record) -> str:
    """带单员身份行：名字只取单行截断后的文本，自由文本不得伪造换行或多行内容。"""
    name = getattr(lead, "name", None) or getattr(record, "lead_name", None)
    if not isinstance(name, str):
        name = "未知带单员"
    else:
        name = re.sub(r"[\x00-\x1f\x7f]+", " ", name)
        name = re.sub(r"\s+", " ", name).strip()[:24] or "未知带单员"
    platform = getattr(lead, "platform", None)
    label = _LEAD_PLATFORMS.get(platform) if isinstance(platform, str) else None
    return f"【{name} · {label}】" if label else f"【{name}】"


def _precise_action(side: Any, direction: Any, position: Optional[dict]) -> Optional[str]:
    """用仓位变化区分开/加仓与减/平仓，并写明多空：开多/加多/开空/加空/减多/平多/减空/平空。

    缺仓位上下文或方向不明时返回 None，走模糊口径，不猜测。
    """
    if not isinstance(position, dict):
        return None
    previous = _decimal(position.get("previous_qty"), allow_zero=True)
    current = _decimal(position.get("current_qty"), allow_zero=True)
    if previous is None or current is None:
        return None
    side_key = str(direction).upper()
    if side_key not in ("LONG", "SHORT"):
        return None
    long_side = side_key == "LONG"
    if _is_opening(side, direction):
        if previous <= 0:
            return "开多" if long_side else "开空"
        return "加多" if long_side else "加空"
    if current <= 0:
        return "平多" if long_side else "平空"
    return "减多" if long_side else "减空"


def _position_line(position: Optional[dict], display_unit: str = DEFAULT_DISPLAY_UNIT,
                   price: Any = None) -> Optional[str]:
    if not isinstance(position, dict):
        return None
    previous = _decimal(position.get("previous_qty"), allow_zero=True)
    current = _decimal(position.get("current_qty"), allow_zero=True)
    if previous is None or current is None:
        return None
    px = _decimal(price)
    if display_unit == DISPLAY_UNIT_NOTIONAL and px is not None:
        # 原/现仓位只有数量：按本笔带单均价折算为金额（近似值，与本笔均价同一口径）。
        # 零仓位折算为 0.00 U，不是未知。
        return f"【原仓位】 {_money(previous * px, allow_zero=True)} → 【现仓位】 {_money(current * px, allow_zero=True)}"
    return f"【原仓位】 {format(previous, 'f')} → 【现仓位】 {format(current, 'f')}"


def _notify_time_line(record) -> Optional[str]:
    """本条通知的生成时间（北京时间）；记录本身没有时间时不显示。"""
    try:
        created = getattr(record, "created_at", None)
        if created is None:
            return None
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)  # 数据库存的是不带时区的 UTC 时间
        moment = created.astimezone(BEIJING_TZ)
    except (TypeError, ValueError, AttributeError, OverflowError, OSError):
        return None
    if not 2020 <= moment.year <= 2100:
        return None
    return f"🕐 通知时间 {moment.strftime('%Y/%m/%d %H:%M:%S')}（北京时间）"


def _event_time_line(source: dict) -> Optional[str]:
    """带单成交的北京时间；事件本身过旧时也如实显示，供判断消息时效。"""
    try:
        event_ms = int(source.get("endTime") or source.get("time") or 0)
        if event_ms <= 0:
            return None
        moment = datetime.fromtimestamp(event_ms / 1000, tz=BEIJING_TZ)
    except (TypeError, ValueError, AttributeError, OverflowError, OSError):
        return None
    if not 2020 <= moment.year <= 2100:
        return None
    return f"🕐 带单时间 {moment.strftime('%Y/%m/%d %H:%M:%S')}（北京时间）"


def _snapshot_line(position: Optional[dict], lead_margin: Any) -> Optional[str]:
    if not isinstance(position, dict):
        return None
    count = position.get("open_count")
    if type(count) is not int or count < 0:
        return None
    line = f"【持仓快照】 当前持仓 {count} 个"
    if _decimal(lead_margin) is not None:
        line += f" ｜ 账户保证金 {_money(lead_margin)}"
    return line


def _lead_section(record, source: dict, lead_margin: Any = None, lead: Any = None,
                  position: Optional[dict] = None, display_unit: str = DEFAULT_DISPLAY_UNIT) -> str:
    """The lead leg of a trade message: identity, action, price, position, snapshot.

    Follower close/guard actions cannot establish the lead's action. A source
    delivery supplies the original symbol/side/amount even if the follower uses
    a mapped instrument, a different close size, or has no local position.
    """
    source = source or {}
    opening = False
    adopted = getattr(record, "status", None) == "ADOPTED_POSITION"
    if adopted:
        # 认领没有独立的带单成交事件：动作与数字直接取记录上的带单员仓位口径。
        symbol, direction = record.symbol, record.position_side
        price, qty, notional = record.lead_price, record.lead_qty, record.lead_notional
        action = "认领仓位"
    elif "_closed" in source:
        closed = source["_closed"]
        symbol, direction = closed.get("symbol"), closed.get("side")
        closed_side = str(direction).upper()
        if closed_side == "LONG":
            action = "平多（历史持仓记录）"
        elif closed_side == "SHORT":
            action = "平空（历史持仓记录）"
        else:
            action = "平仓（历史持仓记录）"
        price, qty, notional = closed.get("avgClosePrice"), None, None
        # maxQty is peak inventory, not a confirmed independent closing fill.
    elif source:
        symbol, direction, side = source.get("symbol"), source.get("positionSide"), source.get("side")
        precise = _precise_action(side, direction, position)
        action = precise if precise is not None else _lead_action(side, direction)
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
        elif position is not None:
            precise = _precise_action(record.side, direction, position)
            if precise is not None:
                action = precise
    # 第一行是带单员身份，第二行就是结论：图标 + 动作 + 币种 + 多空。
    icon, direction_text = {"LONG": ("🟢", "做多"), "SHORT": ("🔴", "做空")}.get(direction, ("❔", "方向未知"))
    if adopted:
        icon = "📥"
    size_line = f"💰 本次均价 {_number(price)}"
    if display_unit != DISPLAY_UNIT_NOTIONAL:
        size_line += f" ｜ 数量 {_number(qty)}"
    if display_unit != DISPLAY_UNIT_QUANTITY:
        size_line += f" ｜ 金额 {_money(notional)}"
    lines = [
        _lead_header(lead, record),
        f"{icon} {action} ｜ {_symbol(symbol)} 永续 ｜ {direction_text}",
        size_line,
    ]
    position_line = _position_line(position, display_unit, price)
    if position_line:
        lines.append(position_line)
    # 折算仓位只对开仓/加仓有意义，平仓不给“建议杠杆”。
    reference = _reference_line(notional, lead_margin) if opening else None
    if reference:
        lines.append(reference)
    latency = _latency_line(source, record)
    if latency:
        lines.append(latency)
    event_time = _event_time_line(source)
    if event_time:
        lines.append(event_time)
    notify_time = _notify_time_line(record)
    if notify_time:
        lines.append(notify_time)
    snapshot = _snapshot_line(position, lead_margin)
    if snapshot:
        lines.append(snapshot)
    return "\n".join(lines)


def _follower_section(record, account, limit: Optional[dict] = None,
                      display_unit: str = DEFAULT_DISPLAY_UNIT) -> str:
    """One bound account's outcome: result, account tag, reason, fill."""
    exchange = {"okx": "欧易", "binance": "Binance", "bybit": "Bybit", "bitget": "Bitget"}.get(
        str(account.exchange).lower(), "交易所未知")
    environment = {"demo": "模拟盘", "simulated": "模拟盘", "live": "实盘"}.get(
        str(account.environment).lower(), "环境未知")
    tag = f"账户 #{record.account_id} / 绑定 #{record.binding_id}（{exchange} {environment}）"
    status = record.status
    confirmed = _number(record.my_qty) != "未知" and (bool(record.order_id) or status == "ADOPTED_POSITION")
    limit_detail: list[str] = []
    if limit and status in ("LIMIT_PLACED", "LIMIT_EXPIRED") or (
            limit and confirmed and status in ("EXECUTED", "PARTIAL")):
        result, limit_detail = _limit_lines(status, record, limit, display_unit)
    elif status == "LIMIT_PLACED":
        result = "⏳ 滑点超限，已改挂限价单（尚未成交）"
    elif status == "LIMIT_EXPIRED":
        result = "⌛ 限价单未成交，已自动撤单"
    elif status == "EXECUTED" and confirmed:
        result = "✅ 已成交"
    elif status == "EXECUTED":
        result = "⚠️ 成交未确认"
    elif status == "ADOPTED_POSITION":
        result = "📥 已认领现有仓位为跟单仓位"
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
    reason_line = _block_reason(status, record)
    if reason_line:
        follower += f"\n{reason_line}"
    if status in ("EXECUTED", "PARTIAL", "UNFILLED", "ADOPTED_POSITION"):
        parts = []
        if display_unit != DISPLAY_UNIT_NOTIONAL:
            parts.append(f"数量 {_number(record.my_qty, allow_zero=True)}")
        parts.append(f"均价 {_number(record.my_price) if confirmed else '未知'}")
        if display_unit != DISPLAY_UNIT_QUANTITY:
            parts.append(f"金额 {_money(record.my_notional) if confirmed else '未知'}")
        follower += "\n📦 " + " ｜ ".join(parts)
    if limit_detail:
        follower += "".join(f"\n{line}" for line in limit_detail)
    return follower


def trade_sections(record, account, source: Optional[dict] = None, lead_margin: Any = None,
                   limit: Optional[dict] = None, lead: Any = None,
                   position: Optional[dict] = None,
                   display_unit: str = DEFAULT_DISPLAY_UNIT) -> tuple[str, Optional[str]]:
    """Render the lead leg and one account's outcome separately for merged delivery."""
    source = source or {}
    display_unit = resolve_display_unit(display_unit)
    lead_part = _lead_section(record, source, lead_margin, lead, position, display_unit)
    follower_part = (
        _follower_section(record, account, limit, display_unit)
        if record is not None and account is not None
        else None
    )
    return lead_part, follower_part


def lead_only_message(source: Any = None, lead_margin: Any = None, lead: Any = None,
                      position: Optional[dict] = None,
                      display_unit: str = DEFAULT_DISPLAY_UNIT) -> str:
    """Render only the lead trader's activity with no follower or order-following info.

    Accepts either (source, lead_margin, lead=lead, ...) or (lead, source, lead_margin, ...).
    """
    if isinstance(source, (dict, type(None))) and not isinstance(lead_margin, dict):
        pass  # called as (source, lead_margin, lead=..., ...)
    elif isinstance(lead_margin, dict):
        # called as (lead, source, lead_margin, ...)
        lead, source, lead_margin = source, lead_margin, lead
    display_unit = resolve_display_unit(display_unit)
    return _lead_section(None, source, lead_margin=lead_margin, lead=lead, position=position, display_unit=display_unit)


def trade_message(record, account, source: Optional[dict] = None, lead_margin: Any = None,
                  limit: Optional[dict] = None, lead: Any = None,
                  position: Optional[dict] = None,
                  display_unit: str = DEFAULT_DISPLAY_UNIT) -> str:
    """One durable message per account outcome, with the independent lead leg first."""
    lead_text, follower = trade_sections(record, account, source, lead_margin, limit, lead, position, display_unit)
    if follower is None:
        return lead_text
    return f"{lead_text}\n\n{follower}"


def merged_trade_message(lead: str, followers: list) -> str:
    """One message per lead event: the lead leg once, then every bound account's outcome below."""
    valid_followers = [f for f in followers if isinstance(f, str) and f.strip()]
    if not valid_followers:
        return lead
    return "\n\n".join([lead, *valid_followers])


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
