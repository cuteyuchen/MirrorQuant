"""管理员看板的只读投影；未知数据不补零，不访问交易所。"""
from __future__ import annotations

import json
import hashlib
import math
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import or_, select

from app.core.config import get_settings
from app.core.secret_mask import redact_error_urls
from app.core.time_util import BEIJING_TZ
from app.models.domain import (
    BindingDelivery, CopyBinding, ExecutionRecord, LeadSource, OrderIntent,
    PositionAllocation, PositionGuardState, SystemConfig, TradingAccount,
)
from app.services.engine.equity import risk_state
from app.services.engine.risk import RiskEngine
from app.services.lead_collector.freshness import lead_freshness


def number(value):
    # 拒绝 bool、NaN 与无穷，保证 JSON 中只出现有限数或 null。
    try:
        result = float(value) if value is not None and not isinstance(value, bool) else None
        return result if result is not None and math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def milliseconds(value):
    if isinstance(value, datetime):
        value = value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp() * 1000
    value = number(value)
    return int(value) if value is not None and value > 0 else None


def _json(raw, kind=dict):
    try:
        value = json.loads(raw) if raw else kind()
        return value if isinstance(value, kind) else kind()
    except (ValueError, TypeError):
        return kind()


def _evidence_confirmed(snapshot, source_valid, observed, now_ms):
    # 持仓证据有两条合法来源：带单员公开仓位的权威快照，或隐藏仓位时由完整订单历史重构的持仓。
    # 只认权威快照会让隐藏仓位带单员的跟单状态永远未知，掩盖真实的对齐/偏离信号。
    positions_meta = snapshot.get("openPositionsMeta")
    meta = snapshot.get("meta")
    authoritative = (isinstance(positions_meta, dict) and positions_meta.get("authoritative") is True
                     and positions_meta.get("complete") is True)
    reconstructed = (isinstance(positions_meta, dict) and positions_meta.get("reconstructed") is True
                     and isinstance(meta, dict) and meta.get("orderHistoryComplete") is True)
    return ((authoritative or reconstructed) and source_valid
            and observed is not None and observed <= now_ms)


def _positions_valid(value):
    # 权威标记不能替代列表自身的结构、数量与唯一性校验。
    if not isinstance(value, list):
        return False
    keys = set()
    for position in value:
        if not isinstance(position, dict):
            return False
        symbol, side = position.get("symbol"), position.get("positionSide")
        qty = number(position.get("qtyExact", position.get("qty")))
        if not isinstance(symbol, str) or not symbol or side not in ("LONG", "SHORT") or qty is None or qty <= 0:
            return False
        key = (symbol, side)
        if key in keys:
            return False
        keys.add(key)
    return True


def _risk(account):
    try:
        state = risk_state(account)
        if not isinstance(state.get("assets"), dict) or any(
            not isinstance(asset, dict) for asset in state["assets"].values()
        ):
            raise ValueError("Invalid assets")
        return state
    except (ValueError, TypeError):
        return {"legacy_unverified": True, "assets": {}}


def _account_row(account, today):
    state = _risk(account)
    asset = state["assets"].get("USDT") or {}
    equity, available = number(asset.get("equity")), number(asset.get("available"))
    if account.last_sync_at and state.get("display_usdt_confirmed"):
        equity, available = number(account.usdt_equity), number(account.avail_equity)
    baseline = number(asset.get("day_start")) if asset.get("date") == today else None
    # 今日盈亏 = 权益变化 - 当日净划转（转入记 +，转出记 -），否则充的钱会被算成盈利。
    # 流水未知（从未成功拉取，或记录不是今天的）时不拿权益差顶着，直接返回 null。
    flow = number(asset.get("today_net_flow")) if asset.get("flow_date") == today else None
    if equity is not None and baseline is not None and flow is not None:
        pnl = number(equity - baseline - flow)
    else:
        pnl = None
    # 看板账户金额沿用 USDT 口径；非 USDT 仓位不能按一比一并入浮盈。
    try:
        positions = json.loads(account.positions_json)
        positions_known = isinstance(positions, list) and all(isinstance(p, dict) for p in positions)
    except (ValueError, TypeError):
        positions, positions_known = [], False
    if not positions_known:
        positions = []
    upls = [number(p.get("upl")) for p in positions if isinstance(p, dict)]
    usdt_only = all(isinstance(p, dict) and str(p.get("symbol") or "").endswith("USDT") for p in positions)
    upl = number(sum(upls)) if positions_known and account.last_sync_at and usdt_only and all(
        value is not None for value in upls) else None
    return {
        "id": account.id, "name": account.name, "exchange": account.exchange,
        "environment": account.environment, "status": account.status, "equity": equity,
        "available": available, "unrealizedPnl": upl, "todayPnl": pnl,
        "todayPnlPct": number(pnl / baseline * 100) if pnl is not None and baseline and baseline > 0 else None,
        "lastSyncAt": milliseconds(account.last_sync_at),
        "riskLegacyUnverified": bool(state.get("legacy_unverified")),
        "ownerUserId": getattr(account, "owner_user_id", None),
    }


def _instrument(binding, account, symbol):
    mapping = _json(binding.symbol_map_json)
    if symbol in mapping:
        return mapping[symbol]
    if account.exchange == "okx" and symbol.endswith(("USDT", "USDC")):
        return f"{symbol[:-4]}-{symbol[-4:]}-SWAP"
    return symbol


def _symbol(binding, inst_id):
    mapping = _json(binding.symbol_map_json)
    aliases = [symbol for symbol, mapped in mapping.items() if mapped == inst_id]
    return aliases[0] if len(aliases) == 1 else inst_id.removesuffix("-SWAP").replace("-", "")


def _mine(binding, account, source, allocation, physical, blocked_reason, uncertain_reason, lead_equity):
    scope_invalid = allocation is not None and allocation.account_id != account.id
    if scope_invalid:
        # 所有权必须同时匹配绑定与账户，不能展示其他账户的损坏账本。
        uncertain_reason = uncertain_reason or "本绑定所有权账户不匹配"
    raw_qty = None if scope_invalid else number(
        allocation.owned_qty_exact if allocation and allocation.owned_qty_exact is not None
        else allocation.owned_qty if allocation else 0)
    if raw_qty is not None and raw_qty < 0:
        raw_qty = None  # 损坏的所有权账本不能显示成有效反向持仓。
        uncertain_reason = uncertain_reason or "本绑定所有权数量无效"
    entry_cost = None if scope_invalid else number(
        allocation.entry_cost_exact if allocation and allocation.entry_cost_exact is not None
        else allocation.entry_cost if allocation else None)
    # 输入有限不代表乘除结果有限，计算溢出仍保留 null。
    entry = number(entry_cost / raw_qty) if raw_qty and entry_cost is not None and entry_cost > 0 else None
    multiplier = 1.0 if account.exchange in ("binance", "bybit", "bitget") else None
    mark = number(physical.get("markPrice", physical.get("curPx")))
    if mark is not None and mark <= 0:
        mark = None  # 零价是缺失标记，不是可用的市场价格。
    physical_qty, notional = number(physical.get("pos")), number(physical.get("notionalUsd"))
    if multiplier is None and mark and physical_qty and notional is not None and abs(notional) > 0:
        # OKX allocation 是合约张数，只有持久化名义金额能证明基币换算比例。
        denominator = number(mark * abs(physical_qty))
        multiplier = number(abs(notional) / denominator) if denominator else None
    qty = number(raw_qty * multiplier) if raw_qty is not None and multiplier is not None else (0.0 if raw_qty == 0 else None)
    side = str(source.get("positionSide") or (allocation.pos_side if allocation else "")).upper()
    pnl = (number((mark - entry) * qty * (1 if side == "LONG" else -1))
           if mark and entry and qty is not None else None)
    if raw_qty == 0 and account.last_sync_at:
        pnl = 0.0  # 已确认没有本绑定仓位，浮盈为已知零。
    currency = str(source.get("symbol") or "")[-4:]
    asset = _risk(account)["assets"].get(currency) or {}
    equity = number(asset.get("equity"))
    lead_qty = number(source.get("qtyExact", source.get("qty")))  # 持久化精确数量优先，非法精确值不回退。
    locked_ok = allocation is not None and not scope_invalid
    locked_exp = number(allocation.expected_qty_exact) if locked_ok and allocation.expected_qty_exact is not None else None
    if locked_exp is not None and locked_exp <= 0:
        locked_exp = None
    locked_lead = number(allocation.lead_qty_exact) if locked_ok and allocation.lead_qty_exact is not None else None
    if locked_lead is not None and locked_lead <= 0:
        locked_lead = None
    locked_ratio = number(allocation.cycle_ratio_exact) if locked_ok and allocation.cycle_ratio_exact is not None else None
    if locked_ratio is not None and locked_ratio <= 0:
        locked_ratio = None
    legacy_unlocked = False
    if lead_qty is None or lead_qty < 0:
        expected = None
    elif lead_qty == 0:
        expected = 0.0
    elif raw_qty == 0:
        expected = (number(lead_qty * equity / lead_equity * binding.risk_scale)
                    if equity is not None and equity >= 0 and lead_equity and lead_equity > 0 else None)
    elif locked_exp is None and locked_ratio is None:
        expected = None
        legacy_unlocked = True
    elif locked_exp is not None and locked_lead is not None:
        if abs(lead_qty - locked_lead) <= 1e-9:
            expected = locked_exp
        elif lead_qty < locked_lead:
            expected = number(locked_exp * (lead_qty / locked_lead))
        else:
            delta_lead = lead_qty - locked_lead
            inc_rate = (equity / lead_equity * binding.risk_scale
                        if equity is not None and equity >= 0 and lead_equity and lead_equity > 0
                        else locked_ratio)
            expected = number(locked_exp + delta_lead * inc_rate) if inc_rate is not None else None
    elif locked_exp is not None:
        expected = locked_exp
    else:
        expected = number(lead_qty * locked_ratio)
    state, reason = "unknown", uncertain_reason
    if blocked_reason:
        state, reason = "blocked", blocked_reason
    elif not uncertain_reason and qty is not None:
        if qty == 0:
            state, reason = "missing", "本绑定尚无已确认仓位"
        elif expected is not None:
            aligned = abs(qty - expected) <= max(abs(expected) * 0.05, 1e-8)
            state, reason = ("aligned", None) if aligned else ("partial", "数量偏离按比例应持仓量超过 5%")
        elif legacy_unlocked:
            state, reason = "unknown", "历史仓位缺少锁定比例，等待下一轮建仓"
        else:
            reason = "缺少同币种净值或带单员余额，无法计算应持仓量"
    if not account.last_sync_at and allocation is None:
        qty = None
    if qty is None and reason is None:
        reason = "缺少合约张数与基币数量的换算证据"
    position_cost = number(qty * entry) if qty is not None and entry is not None else None
    return {
        "bindingId": binding.id, "bindingName": binding.name, "accountId": account.id,
        "accountName": account.name, "environment": account.environment,
        "bindingEnabled": binding.is_enabled, "breakerTripped": binding.circuit_breaker_tripped,
        "state": state, "qty": qty, "entryPrice": entry, "pnl": pnl,
        "pnlPct": number(pnl / position_cost * 100) if pnl is not None and position_cost and position_cost > 0 else None,
        "expectedQty": expected, "reason": reason,
    }


def _guard_prices(binding, mine, guard, side):
    entry = mine["entryPrice"]
    if not entry or not mine["qty"]:
        return None, None
    tp, sl = RiskEngine.compute_tp_sl_prices(
        side.lower(), entry, binding.tp_sl_mode, binding.enable_take_profit, binding.take_profit_pct,
        binding.enable_stop_loss, binding.stop_loss_pct, binding.hard_stop_loss_pct,
    )
    if guard and binding.tp_sl_mode == "independent" and binding.enable_trailing_stop:
        if side == "LONG" and guard.peak_px > entry:
            trailing = guard.peak_px * (1 - binding.trailing_callback_pct / 100)
            sl = max(sl, trailing) if sl is not None else trailing
        elif side == "SHORT" and 0 < guard.trough_px < entry:
            trailing = guard.trough_px * (1 + binding.trailing_callback_pct / 100)
            sl = min(sl, trailing) if sl is not None else trailing
    return number(tp), number(sl)


def _common_price(prices):
    # 多个绑定价位不一致时，契约中的单一价位不能代表所有软件守护。
    return prices[0] if prices and prices[0] is not None and all(
        price is not None and math.isclose(price, prices[0], rel_tol=1e-9) for price in prices) else None


def _realized_pnl(row):
    # 显式未知标记优先；旧记录只有非零收益才能排除历史零占位歧义。
    value = number(row.get("totalPnl", row.get("pnl")))
    if row.get("pnlKnown") is True:
        return value
    if "pnlKnown" in row:
        return None
    return value if value not in (None, 0) else None


def _events(lead, snapshot, deliveries, records, day_start, now):
    store = _json(lead.orders_store_json)
    display_fallback = not store and bool(snapshot.get("orders"))
    if not store:
        # 兼容只有持久化展示快照的旧记录；没有稳定来源键时不猜测执行记录关联。
        for raw in snapshot.get("orders") or []:
            if not isinstance(raw, dict):
                continue
            key = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()[:24]
            store[key] = {"t": raw.get("time"), "ot": raw.get("orderTime"), "sym": raw.get("symbol"),
                          "ps": raw.get("positionSide"), "side": raw.get("side"), "q": raw.get("qty"),
                          "p": raw.get("avgPrice"), "pnl": raw.get("pnl"),
                          **({"pnlKnown": raw["pnlKnown"]} if "pnlKnown" in raw else {})}
    display_fallback = display_fallback and bool(store)
    orders = {}
    delivery_keys = {}
    fallback_bindings = defaultdict(set)
    for key, raw in store.items():
        if not isinstance(raw, dict):
            continue
        orders[key] = {
            "id": f"order:{key}", "time": milliseconds(raw.get("t") or raw.get("orderUpdateTime") or raw.get("ot")),
            "symbol": raw.get("sym") or raw.get("symbol"), "side": raw.get("ps") or raw.get("positionSide"),
            "tradeSide": raw.get("side"), "qty": number(raw.get("q", raw.get("executedQty"))),
            "price": number(raw.get("p", raw.get("avgPrice"))),
            # 旧 slim 数据把未知 PnL 存成零，不能反推成真实零收益。
            "realizedPnl": _realized_pnl(raw),
        }
    for delivery in deliveries:
        payload = _json(delivery.payload_json)
        key = payload.get("_source_key") or delivery.source_event_id
        delivery_keys[delivery.source_event_id] = key
        if "_closed" in payload or key in store or display_fallback:
            continue
        qty, price, pnl = number(payload.get("qtyExact", payload.get("qty"))), number(payload.get("avgPrice")), _realized_pnl(payload)
        if key in orders:
            # 跨绑定重复来源可去重；同绑定的多次增量不能猜测原始成交量和累计收益。
            event = orders[key]
            if delivery.binding_id in fallback_bindings[key] or (event["qty"], event["price"], event["realizedPnl"]) != (qty, price, pnl):
                event["qty"] = event["price"] = event["realizedPnl"] = None
            fallback_bindings[key].add(delivery.binding_id)
            continue
        orders[key] = {"id": f"order:{key}", "time": delivery.source_sequence,
            "symbol": payload.get("symbol"), "side": payload.get("positionSide"),
            "tradeSide": payload.get("side"), "qty": qty,
            "price": price, "realizedPnl": pnl}
        fallback_bindings[key].add(delivery.binding_id)
        # 同一来源在多个绑定出现不重复计数；不合并无法证明的增量成交。
    observed_positions = snapshot.get("openPositions")
    meta = snapshot.get("openPositionsMeta") or {}
    # 隐藏仓位带单员的持仓由完整订单历史重构，永远拿不到权威/完整标记；只认权威快照会让
    # 今日统计与动态永远空白。与 _evidence_confirmed 同一口径：重构证据 + 完整订单历史即确认。
    evidence = ((meta.get("authoritative") is True and meta.get("complete") is True) or
                (meta.get("reconstructed") is True and
                 (snapshot.get("meta") or {}).get("orderHistoryComplete") is True))
    known = _positions_valid(observed_positions) and evidence
    known &= (snapshot.get("meta") or {}).get("orderHistoryComplete") is True
    observed_at = milliseconds(meta.get("asOf"))
    known &= (observed_at is not None and 0 <= now - observed_at <= get_settings().lead_snapshot_stale_sec * 1000
              and lead.status != "error")  # 旧观察不能证明直到当前时刻的日内统计。
    # 损坏的符号或方向不能构造有效持仓键。
    remaining = {(p.get("symbol"), p.get("positionSide")): number(p.get("qtyExact", p.get("qty")))
                 for p in observed_positions or [] if isinstance(p, dict)
                 and isinstance(p.get("symbol"), str) and p.get("positionSide") in ("LONG", "SHORT")} if known else {}
    signed_remaining: dict[str, float | None] = {}
    if known:
        for (p_sym, p_side), p_qty in remaining.items():
            if p_qty is not None:
                signed_remaining[p_sym] = (signed_remaining.get(p_sym) or 0.0) + (p_qty if p_side == "LONG" else -p_qty)
    closed = [c for c in _json(lead.closed_cache_json, list) if isinstance(c, dict)]
    closed += [c for c in snapshot.get("closed") or [] if isinstance(c, dict)]
    # 同一闭仓周期的刷新只保留最新观察，避免日内次数被重复累计。
    cycles = {(str(c.get("id")), c.get("symbol"), c.get("side")): c for c in closed
              if isinstance(c.get("symbol"), str) and c.get("side") in ("LONG", "SHORT")}
    execution_by_key = defaultdict(list)
    for record in records:
        key = delivery_keys.get(record.delivery_event_id)
        if key is not None:
            execution_by_key[key].append({
                "bindingId": record.binding_id, "accountName": record.account_name,
                "status": record.status, "qty": number(record.my_qty), "price": number(record.my_price) or None,
                "slippagePct": number(record.slippage_pct) if record.my_qty > 0 else None,
                "spreadPct": number(record.spread_pct) if record.my_qty > 0 else None,
                "details": redact_error_urls(record.details),
            })
    result = []
    event_actions_by_id: dict[str, str] = {}
    sorted_orders = sorted(orders.items(), key=lambda item: (item[1]["time"] or 0, item[0]), reverse=True)
    for key, event in sorted_orders:
        timestamp, qty, side = event["time"], event["qty"], event["side"]
        if (timestamp is None or timestamp > now or not qty or qty < 0
                or not isinstance(event["symbol"], str) or event["tradeSide"] not in ("BUY", "SELL")):
            continue
        if side in ("LONG", "SHORT"):
            leg = (event["symbol"], side)
            is_open = (event["tradeSide"], side) in (("BUY", "LONG"), ("SELL", "SHORT"))
            # 观察时间之后的成交不能用更早的仓位倒推开仓或平仓。
            after = remaining.get(leg, 0.0 if known else None) if observed_at and timestamp <= observed_at else None
            action = None
            out_side = side
            if after is not None:
                before = after - qty if is_open else after + qty
                if before >= -1e-8:
                    action = ("OPEN" if before <= 1e-8 else "ADD") if is_open else ("CLOSE" if after <= 1e-8 else "REDUCE")
                    remaining[leg] = max(0.0, before)
                else:
                    remaining[leg] = None
            if action is None:
                for cycle in cycles.values():
                    if (cycle.get("symbol"), cycle.get("side")) != leg:
                        continue
                    opened, ended = milliseconds(cycle.get("opened")), milliseconds(cycle.get("closed"))
                    if opened and ended and opened <= timestamp <= ended:
                        if is_open:
                            action = "OPEN" if timestamp == opened else "ADD"
                        else:
                            action = "CLOSE" if timestamp == ended else "REDUCE"
                        break
        elif side in ("BOTH", "", None):
            sym = event["symbol"]
            trade_side = event["tradeSide"]
            after_signed = (
                signed_remaining[sym]
                if sym in signed_remaining
                else (0.0 if known else None)
            ) if observed_at and timestamp <= observed_at else None
            action = None
            out_side = None
            if after_signed is not None:
                delta = qty if trade_side == "BUY" else -qty
                before_signed = after_signed - delta
                realized_val = event["realizedPnl"]
                if (
                    ((before_signed > 1e-8 and after_signed < -1e-8) or (before_signed < -1e-8 and after_signed > 1e-8))
                    and realized_val == 0
                ):
                    action = "OPEN"
                    out_side = "SHORT" if trade_side == "SELL" else "LONG"
                    signed_remaining[sym] = None
                else:
                    if before_signed > 1e-8 or after_signed > 1e-8:
                        out_side = "LONG"
                        action = ("OPEN" if before_signed <= 1e-8 else "ADD") if trade_side == "BUY" else ("CLOSE" if after_signed <= 1e-8 else "REDUCE")
                    elif before_signed < -1e-8 or after_signed < -1e-8:
                        out_side = "SHORT"
                        action = ("OPEN" if before_signed >= -1e-8 else "ADD") if trade_side == "SELL" else ("CLOSE" if after_signed >= -1e-8 else "REDUCE")
                    if action:
                        signed_remaining[sym] = before_signed
            if action is None:
                for cycle in cycles.values():
                    if cycle.get("symbol") != sym or cycle.get("side") not in ("LONG", "SHORT"):
                        continue
                    opened, ended = milliseconds(cycle.get("opened")), milliseconds(cycle.get("closed"))
                    if opened and ended and opened <= timestamp <= ended:
                        c_side = cycle["side"]
                        c_open = (trade_side, c_side) in (("BUY", "LONG"), ("SELL", "SHORT"))
                        out_side = c_side
                        action = ("OPEN" if timestamp == opened else "ADD") if c_open else ("CLOSE" if timestamp == ended else "REDUCE")
                        break
        else:
            continue

        if action:
            event_actions_by_id[event["id"]] = action
        if action and out_side and timestamp >= day_start:
            result.append({
                "id": event["id"], "time": event["time"], "symbol": event["symbol"],
                "side": out_side, "qty": event["qty"], "price": event["price"], "realizedPnl": event["realizedPnl"],
                "action": action, "mine": execution_by_key[key],
            })
    for cycle in cycles.values():
        timestamp = milliseconds(cycle.get("closed"))
        side, symbol = cycle.get("side"), cycle.get("symbol")
        if not timestamp or not day_start <= timestamp <= now or side not in ("LONG", "SHORT"):
            continue
        opened = milliseconds(cycle.get("opened"))
        if any(e["symbol"] == symbol and e["side"] == side and e["action"] == "CLOSE"
               and (opened or timestamp) <= e["time"] <= timestamp for e in result):
            continue
        # 闭仓汇总没有最后一笔数量，不能用历史最大持仓量伪造本次成交量。
        event_id = "closed:" + str(cycle.get("id"))
        result.append({"id": event_id, "time": timestamp, "symbol": symbol, "side": side, "action": "CLOSE",
                       "qty": None, "price": number(cycle.get("avgClosePrice")), "realizedPnl": _realized_pnl(cycle),
                       "mine": execution_by_key[event_id]})
    day_orders = [o for o in orders.values() if o["time"] and day_start <= o["time"] <= now]
    exits = [
        o for o in day_orders
        if (o["tradeSide"], o["side"]) in (("SELL", "LONG"), ("BUY", "SHORT"))
        or (o["side"] in ("BOTH", "", None) and event_actions_by_id.get(o["id"]) in ("CLOSE", "REDUCE"))
    ]
    # 只有订单历史覆盖今日且逐笔 PnL 都已知，才给出日内已实现收益。
    covered = any(o["time"] and o["time"] < day_start for o in orders.values())
    realized = number(sum(o["realizedPnl"] for o in exits)) if covered and known and all(
        o["realizedPnl"] is not None for o in exits) else None
    result.sort(key=lambda event: (event["time"], event["id"]), reverse=True)
    # 只要今日有无法分类的来源事件，次数也是未知，不能显示伪造的零。
    classified = {event["id"] for event in result}
    classifications_complete = known and all(order["id"] in classified for order in day_orders)
    return result, realized, classifications_complete


async def build_board(db, *, now=None, owner_user_id=None):
    """Build the copy board snapshot.

    owner_user_id scopes the board to one user's accounts/bindings (and their
    allocations, intents, deliveries and execution records). Admins pass None
    and see everything; leads themselves are visible to all users.
    """
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(BEIJING_TZ)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    now_ms, start_ms = milliseconds(now), milliseconds(start)
    leads = list((await db.scalars(select(LeadSource).where(
        LeadSource.deleted_at.is_(None)).order_by(LeadSource.id))).all())
    account_stmt = select(TradingAccount).where(TradingAccount.deleted_at.is_(None))
    binding_stmt = select(CopyBinding).where(CopyBinding.deleted_at.is_(None))
    if owner_user_id is not None:
        account_stmt = account_stmt.where(TradingAccount.owner_user_id == owner_user_id)
        binding_stmt = binding_stmt.where(CopyBinding.owner_user_id == owner_user_id)
    accounts = {a.id: a for a in (await db.scalars(account_stmt.order_by(TradingAccount.id))).all()}
    bindings = list((await db.scalars(binding_stmt.order_by(CopyBinding.id))).all())
    allocations = {(a.binding_id, a.inst_id, a.pos_side.upper()): a for a in
                   (await db.scalars(select(PositionAllocation))).all()}
    guards = {(g.binding_id, g.inst_id, g.pos_side.upper()): g for g in
              (await db.scalars(select(PositionGuardState))).all()}
    intents = list((await db.scalars(select(OrderIntent).where(OrderIntent.state.in_(("UNKNOWN", "SUBMITTING"))))).all())
    deliveries = list((await db.scalars(select(BindingDelivery).where(or_(
        BindingDelivery.source_sequence >= start_ms, BindingDelivery.exit_progress == "REVIEW_REQUIRED",
        BindingDelivery.state == "REVIEW_REQUIRED",
    )))).all())
    records = list((await db.scalars(select(ExecutionRecord).where(
        ExecutionRecord.created_at >= start.astimezone(timezone.utc),
    ).order_by(ExecutionRecord.created_at.desc(), ExecutionRecord.id.desc()))).all())
    if owner_user_id is not None:
        binding_ids = {b.id for b in bindings}
        account_ids = set(accounts)
        allocations = {k: v for k, v in allocations.items() if v.binding_id in binding_ids}
        guards = {k: v for k, v in guards.items() if v.binding_id in binding_ids}
        intents = [i for i in intents if i.account_id in account_ids]
        deliveries = [d for d in deliveries if d.binding_id in binding_ids]
        records = [r for r in records if r.account_id in account_ids]
    latest = {}
    for record in records:
        latest.setdefault((record.binding_id, record.inst_id, record.position_side), record)
    kill_row = await db.get(SystemConfig, "global_kill_switch")
    kill = bool(_json(kill_row.value_json, bool)) if kill_row else False
    alerts = []

    def alert(level, code, message, binding=None, lead_id=None, account_id=None):
        alerts.append({"level": level, "code": code, "message": redact_error_urls(message),
                       "bindingId": binding.id if binding else None,
                       "leadId": binding.lead_id if binding else lead_id,
                       "accountId": binding.account_id if binding else account_id})

    if kill:
        alert("critical", "GLOBAL_KILL_SWITCH", "全局急停已开启，新开仓暂停，保护性平仓仍可执行")
    binding_map = {b.id: b for b in bindings}
    unresolved = {(i.binding_id, i.inst_id, i.pos_side.upper()) for i in intents}
    review = set()
    for delivery in deliveries:
        if delivery.exit_progress == "REVIEW_REQUIRED" or delivery.state == "REVIEW_REQUIRED":
            review.add(delivery.binding_id)
    for binding in bindings:
        if binding.circuit_breaker_tripped:
            alert("critical", "BINDING_BREAKER", binding.circuit_breaker_reason or "绑定已熔断", binding)
        if binding.id in review:
            alert("critical", "DELIVERY_REVIEW_REQUIRED", "投递证据存在冲突，需要人工审查", binding)
    for intent in intents:
        alert("critical", "ORDER_INTENT_UNRESOLVED", f"订单意图 {intent.id} 处于 {intent.state}",
              binding_map.get(intent.binding_id), account_id=intent.account_id)
    for account in accounts.values():
        if account.status == "error" or account.last_error:
            alert("warning", "ACCOUNT_SYNC_FAILED", account.last_error or "账户同步失败", account_id=account.id)
        if _risk(account).get("legacy_unverified"):
            alert("critical", "RISK_LEGACY_UNVERIFIED", "账户历史风险基线币种未经确认", account_id=account.id)
    lead_rows = []
    for lead in leads:
        snapshot = _json(lead.snapshot_json)
        # 历史 JSON 字段形状损坏时保留未知状态，不让单个带单员拖垮整个看板。
        for field in ("meta", "profile", "openPositionsMeta"):
            if not isinstance(snapshot.get(field), dict):
                snapshot[field] = {}
        for field in ("orders", "closed"):
            if not isinstance(snapshot.get(field), list):
                snapshot[field] = []
        if not isinstance(snapshot.get("openPositions"), list):
            snapshot["openPositions"] = None
        fresh = lead_freshness(lead, snapshot, now_ms=now_ms)
        observed = milliseconds((snapshot.get("openPositionsMeta") or {}).get("asOf"))
        if observed and observed <= now_ms:
            age = (now_ms - observed) / 1000
            fresh["snapshotAgeSec"] = max(fresh["snapshotAgeSec"] or 0, age)
            fresh["stale"] |= age > get_settings().lead_snapshot_stale_sec
        elif observed is not None:
            fresh["stale"] = True  # 未来观察时间不能作为新鲜证据。
        if fresh["stale"]:
            alert("warning", "LEAD_SNAPSHOT_STALE", "带单员快照已过期或时间未知", lead_id=lead.id)
        if lead.status == "error" or lead.last_error:
            alert("warning", "LEAD_SYNC_FAILED", lead.last_error or "带单员同步失败", lead_id=lead.id)
        bound = [b for b in bindings if b.lead_id == lead.id and b.account_id in accounts]
        source_positions = [p for p in snapshot.get("openPositions") or [] if isinstance(p, dict)]
        source_valid = _positions_valid(snapshot.get("openPositions"))  # 同一列表只校验一次，供所有绑定复用。
        positions, orphans, used = [], [], set()
        lead_equity = number((snapshot.get("profile") or {}).get("marginBalance"))
        if lead_equity is None and lead.last_sync_at:
            lead_equity = number(lead.margin_balance)

        def mine_for(binding, source, allocation, inst_id):
            account = accounts[binding.account_id]
            physical = next((p for p in _json(account.positions_json, list) if isinstance(p, dict) and
                             p.get("instId") == inst_id and str(p.get("posSide")).upper() == source["positionSide"]), {})
            reason = ("全局急停已开启" if kill else "绑定已禁用" if not binding.is_enabled else
                      "账户已禁用" if not account.is_enabled else
                      binding.circuit_breaker_reason or "绑定已熔断" if binding.circuit_breaker_tripped else
                      "投递需要人工审查" if binding.id in review else
                      "历史风险基线未经确认" if _risk(account).get("legacy_unverified") else None)
            symbol_check = RiskEngine.check_symbol_filter(source["symbol"], _json(binding.symbol_whitelist_json, list),
                                                         _json(binding.symbol_blacklist_json, list))
            if not reason and not symbol_check.allowed:
                reason = symbol_check.reason
            recent = latest.get((binding.id, inst_id, source["positionSide"]))
            if not reason and recent and recent.status.startswith("BLOCKED") and recent.status not in (
                "BLOCKED_KILL_SWITCH", "BLOCKED_CIRCUIT_BREAKER",
            ):
                reason = redact_error_urls(recent.details) or recent.status
            uncertain = ("存在未决订单意图" if (binding.id, inst_id, source["positionSide"]) in unresolved else
                         "带单员快照过期" if fresh["stale"] else
                         "带单员持仓证据未确认" if not _evidence_confirmed(snapshot, source_valid, observed, now_ms) else
                         "账户尚未成功同步" if not account.last_sync_at else
                         "账户同步失败，仓位数据可能已过期" if account.status == "error" else None)
            if not uncertain and recent and recent.status == "DEFERRED_SOURCE_UNPROVEN":
                # 出场延期不是正常对齐证据；后续确认记录会覆盖这条原因。
                uncertain = redact_error_urls(recent.details) or "带单员出场证据不足，等待下一轮观察"
            return _mine(binding, account, source, allocation, physical, reason, uncertain, lead_equity)

        for source in source_positions:
            symbol, side = source.get("symbol"), source.get("positionSide")
            if not isinstance(symbol, str) or not symbol or side not in ("LONG", "SHORT"):
                continue
            opened = milliseconds(source.get("openTime"))
            if opened and opened > now_ms:
                opened = None
            mine, prices = [], []
            for binding in bound:
                inst_id = _instrument(binding, accounts[binding.account_id], symbol)
                key = (binding.id, inst_id, side)
                used.add(key)
                allocation = allocations.get(key)
                item = mine_for(binding, source, allocation, inst_id)
                mine.append(item)
                if allocation and number(allocation.owned_qty_exact or allocation.owned_qty):
                    prices.append(_guard_prices(binding, item, guards.get(key), side))
            qty, entry, mark = number(source.get("qtyExact", source.get("qty"))), number(source.get("avgEntry")), number(source.get("markPrice"))
            # 缺失价格常被旧采集器存为零，不能当成已知成交价或行情。
            entry = entry if entry is not None and entry > 0 else None
            mark = mark if mark is not None and mark > 0 else None
            qty = qty if qty is not None and qty > 0 else None
            positions.append({
                "key": f"{symbol}_{side}", "symbol": symbol, "side": side, "qty": qty,
                "entryPrice": entry, "markPrice": mark, "pnl": number(source.get("uPnL")),
                "pnlPct": number(source.get("uPnLPct")), "leverage": number(source.get("leverage")),
                "notional": number(source.get("markValue", source.get("notional"))),
                "openedAt": opened, "openedToday": start_ms <= opened <= now_ms if opened is not None else None,
                "takeProfitPrice": _common_price([price[0] for price in prices]),
                "stopLossPrice": _common_price([price[1] for price in prices]), "mine": mine,
            })
        # 持仓列表未知时不能把所有本地仓位误报为孤儿仓。
        source_known = (_evidence_confirmed(snapshot, source_valid, observed, now_ms)
                        and not fresh["stale"])
        if source_known:
            for key, allocation in allocations.items():
                binding = binding_map.get(allocation.binding_id)
                if key in used or binding not in bound or not number(allocation.owned_qty_exact or allocation.owned_qty):
                    continue
                if allocation.account_id != binding.account_id:
                    continue  # 其他账户的损坏归属不能当成本绑定的孤儿仓。
                symbol, side = _symbol(binding, allocation.inst_id), allocation.pos_side.upper()
                source = {"symbol": symbol, "positionSide": side, "qty": 0}
                item = mine_for(binding, source, allocation, allocation.inst_id)
                orphans.append({**item, "symbol": symbol, "side": side})
        bound_ids = {b.id for b in bound}
        events, realized, classifications_complete = _events(
            lead, snapshot, [d for d in deliveries if d.binding_id in bound_ids],
            [r for r in records if r.lead_id == lead.id], start_ms, now_ms,
        )
        counts_known = (classifications_complete and not fresh["stale"] and
                        (snapshot.get("meta") or {}).get("orderHistoryComplete") is True)
        position_show = (snapshot.get("profile") or {}).get("positionShow", lead.position_show)
        positions_meta = snapshot.get("openPositionsMeta") or {}
        positions_confirmed = bool(
            _evidence_confirmed(snapshot, source_valid, observed, now_ms)
            or (source_valid and positions_meta.get("emptyVerified") is True)
        )
        lead_rows.append({
            "id": lead.id, "name": lead.name, "avatarUrl": lead.avatar_url, "status": lead.status,
            "enabled": lead.is_enabled, "stale": fresh["stale"], "snapshotAgeSec": fresh["snapshotAgeSec"],
            "positionShow": position_show if isinstance(position_show, bool) else None,
            "positionsConfirmed": positions_confirmed,
            "boundCount": len(bound), "activeBoundCount": sum(1 for b in bound if b.is_enabled),
            "todayOpenCount": sum(event["action"] == "OPEN" for event in events) if counts_known else None,
            "todayCloseCount": sum(event["action"] == "CLOSE" for event in events) if counts_known else None,
            "todayRealizedPnl": realized,
            "positions": positions if source_valid or positions else None,
            "orphans": orphans if source_known else None, "todayEvents": events[:100],
        })
    return {
        "serverTime": now_ms, "dayStart": start_ms, "globalKillSwitch": kill, "alerts": alerts,
        "accounts": [_account_row(account, local.strftime("%Y-%m-%d")) for account in accounts.values()],
        "leads": lead_rows,
    }
