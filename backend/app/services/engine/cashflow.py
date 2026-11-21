"""当日现金流：现货 <-> 合约钱包划转净额，用于修正“今日盈亏”。

合约权益的变化 = 交易盈亏 + 当日净划转。看板之前只算权益差，把用户
当天转进来的钱也算成了盈利。修正公式：今日盈亏 = 权益 - 日初权益 - 当日净划转。

注意：外部充值/提现只动现货钱包，不动合约权益，所以不能用充值/提现
记录；必须用期货划转记录（/fapi/v1/income 的 TRANSFER 条目），正数转入、
负数转出。SAPI /sapi/v1/futures/transfer 对本账户返回 total 0，漏掉了
收入历史里明明存在的划转，不能用它。日期一律按北京时间。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.core.time_util import BEIJING_TZ

# 划转记录 5 分钟内不重复拉：转入/转出是低频操作，同步本身很频繁。
CASH_FLOW_REFRESH_SEC = 300


def beijing_day_window_ms(today: str) -> tuple[int, int]:
    """北京时间某日的 [00:00, 次日 00:00) 对应 UTC 毫秒区间（左闭右开）。"""
    start = datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=BEIJING_TZ)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def net_flow_from_transfers(
    rows: List[Dict[str, Any]], asset: str, start_ms: int, end_ms: int,
) -> float:
    """对划转行求净额：type 1 转入记 +，type 2 转出记 -；只算指定币种与窗口内。"""
    total = 0.0
    for row in rows:
        try:
            amount = float(row["amount"])
            ts = int(row["timestamp"])
            kind = int(row["type"])
        except (KeyError, TypeError, ValueError):
            continue
        if row.get("asset") != asset or kind not in (1, 2):
            continue
        if not start_ms <= ts < end_ms or amount != amount:
            continue
        total += amount if kind == 1 else -amount
    return total


async def fetch_today_net_flow(adapter: Any, asset: str, today: str) -> float:
    """拉当日划转并求净额；拉不到（权限/网络/交易所不支持）时抛异常，调用方按未知处理。"""
    start_ms, end_ms = beijing_day_window_ms(today)
    rows = await adapter.get_futures_transfer_history(asset, start_ms, end_ms)
    if not isinstance(rows, list):
        raise ValueError("Invalid futures transfer history")
    return net_flow_from_transfers(rows, asset, start_ms, end_ms)


def stored_today_net_flow(state: Dict[str, Any], today: str) -> Optional[float]:
    """读持久化的当日净划转；日期不是今天或从未成功拉取过时返回 None（未知）。"""
    asset = (state.get("assets") or {}).get("USDT") or {}
    if asset.get("flow_date") != today:
        return None
    value = asset.get("today_net_flow")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def cash_flow_is_fresh(state: Dict[str, Any], today: str, now_ms: int) -> bool:
    """当日已有记录且在刷新间隔内时为真，可直接复用，不必再拉交易所。"""
    asset = (state.get("assets") or {}).get("USDT") or {}
    if asset.get("flow_date") != today:
        return False
    updated_at = asset.get("flow_updated_at")
    if not isinstance(updated_at, (int, float)) or isinstance(updated_at, bool):
        return False
    return 0 <= now_ms - int(updated_at) < CASH_FLOW_REFRESH_SEC * 1000
