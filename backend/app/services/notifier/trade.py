"""Trade notifications use source fills and allowlisted fields, never free-text names/errors."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Optional


def _number(value: Any, *, allow_zero: bool = False) -> str:
    if value is None or isinstance(value, bool):
        return "未知"
    try:
        number = Decimal(str(value))
        if number.is_finite() and (number > 0 or (allow_zero and number == 0)):
            return format(number, "f")
    except (InvalidOperation, ValueError):
        pass
    return "未知"


def _symbol(value: Any) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z0-9_:/-]{1,64}", value) else "未知"


def trade_message(record, account, source: Optional[dict] = None) -> str:
    """One durable message per account outcome, with the independent lead leg first.

    Follower close/guard actions cannot establish the lead's action. A source
    delivery supplies the original symbol/side/amount even if the follower uses
    a mapped instrument, a different close size, or has no local position.
    """
    source = source or {}
    if "_closed" in source:
        closed = source["_closed"]
        symbol, direction = closed.get("symbol"), closed.get("side")
        action = "平仓（历史持仓记录）"
        price, qty, notional = closed.get("avgClosePrice"), None, None
        # maxQty is peak inventory, not a confirmed independent closing fill.
    elif source:
        symbol, direction, side = source.get("symbol"), source.get("positionSide"), source.get("side")
        action = _lead_action(side, direction)
        price = source.get("avgPrice")
        qty = source.get("qtyExact", source.get("qty"))
        notional = source.get("notionalExact", source.get("notional"))
    else:
        symbol, direction = record.symbol, record.position_side
        price, qty, notional = record.lead_price, record.lead_qty, record.lead_notional
        # Autonomous follower exits/alerts contain no independent lead fill.
        action = _lead_action(record.side, direction) if _number(qty) != "未知" else "未知"
        if action == "未知":
            symbol, direction, price, qty, notional = None, None, None, None, None
    direction = direction if direction in ("LONG", "SHORT") else "未知"
    lead = (f"📣 带单动作: {action} | {_symbol(symbol)} | 方向 {direction}\n"
            f"带单成交均价: {_number(price)} | 数量: {_number(qty)} | 名义金额: {_number(notional)}")

    exchange = {"okx": "欧易", "binance": "Binance", "bybit": "Bybit", "bitget": "Bitget"}.get(
        str(account.exchange).lower(), "交易所未知")
    environment = {"demo": "模拟盘", "simulated": "模拟盘", "live": "实盘"}.get(
        str(account.environment).lower(), "环境未知")
    tag = f"跟单账户 #{record.account_id} / 绑定 #{record.binding_id} ({exchange} {environment})"
    status = record.status
    confirmed = bool(record.order_id) and _number(record.my_qty) != "未知"
    if status == "EXECUTED" and confirmed:
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
    follower = f"{result} {tag} | {follower_symbol} | {safe_status}"
    if status in ("EXECUTED", "PARTIAL", "UNFILLED"):
        follower += (f"\n跟单确认数量: {_number(record.my_qty, allow_zero=True)}"
                     f" | 成交均价: {_number(record.my_price) if confirmed else '未知'}"
                     f" | 名义金额: {_number(record.my_notional) if confirmed else '未知'}")
    return f"{lead}\n\n{follower}"


def _lead_action(side: Any, direction: Any) -> str:
    # A fill establishes its leg, not whether either trader already held inventory.
    return {
        ("BUY", "LONG"): "买入开多/加多",
        ("SELL", "SHORT"): "卖出开空/加空",
        ("SELL", "LONG"): "卖出平多/减多",
        ("BUY", "SHORT"): "买入平空/减空",
    }.get((str(side).upper(), str(direction).upper()), "未知")
