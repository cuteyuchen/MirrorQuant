import pytest

from app.services.lead_collector.binance_bapi import (
    build_order_batches,
    classify_order_actions,
    direction_label,
    expand_slim_order,
    group_orders_into_batches,
    is_open_leg,
    order_dedup_key,
    reconstruct_open_positions,
    reconstruct_trade_cycles,
    slim_order,
)


def test_order_dedup_key_invariance():
    # Same invariant fields must produce identical sha1 hash
    o1 = {
        "orderTime": 1700000000000,
        "symbol": "BTCUSDT",
        "side": "BUY",
        "positionSide": "LONG",
        "type": "MARKET",
        "executedQty": 0.5,
        "avgPrice": 65000.0,
        "orderUpdateTime": 1700000005000,  # mutated timestamp
    }
    o2 = dict(o1)
    o2["orderUpdateTime"] = 1700000099999  # changed later by exchange

    assert order_dedup_key(o1) == order_dedup_key(o2)


def test_direction_and_batching():
    assert is_open_leg("BUY", "LONG") is True
    assert is_open_leg("SELL", "LONG") is False
    assert is_open_leg("SELL", "SHORT") is True
    assert is_open_leg("BUY", "SHORT") is False

    assert direction_label("BUY", "LONG", False) == "OPEN_LONG"
    assert direction_label("SELL", "LONG", True) == "CLOSE_LONG"

    # Grouping into directional batches within 30 mins
    orders = [
        {"sym": "BTCUSDT", "side": "BUY", "ps": "LONG", "q": 0.1, "p": 60000.0, "t": 1000},
        {"sym": "BTCUSDT", "side": "BUY", "ps": "LONG", "q": 0.2, "p": 60100.0, "t": 2000},
        {"sym": "BTCUSDT", "side": "BUY", "ps": "LONG", "q": 0.3, "p": 60200.0, "t": 3000},
    ]
    batches = group_orders_into_batches(orders)
    assert len(batches) == 1
    assert round(batches[0]["qty"], 2) == 0.6
    assert batches[0]["orderCount"] == 3


def test_reconstruct_trade_cycles_from_reference_flow():
    # Scenario: 2 open legs followed by 1 full close leg (like Aoying BTC review)
    orders = [
        {"sym": "BTCUSDT", "side": "SELL", "ps": "SHORT", "q": 48.971, "p": 82555.0, "t": 10000, "pnl": 0.0},
        {"sym": "BTCUSDT", "side": "SELL", "ps": "SHORT", "q": 47.241, "p": 82571.0, "t": 20000, "pnl": 0.0},
        {"sym": "BTCUSDT", "side": "BUY", "ps": "SHORT", "q": 96.212, "p": 81934.0, "t": 80000, "pnl": 60000.0},
    ]
    cycles = reconstruct_trade_cycles(orders)
    assert len(cycles) == 1
    c = cycles[0]
    assert c["status"] == "CLOSED"
    assert c["entryCount"] == 2
    assert c["exitCount"] == 1
    assert c["scaleInCount"] == 1
    assert round(c["totalEntryQty"], 3) == 96.212
    assert round(c["totalExitQty"], 3) == 96.212
    assert c["realizedPnl"] == 60000.0


def test_classify_oneway_orders_and_cycles_when_flat():
    # One-Way (positionSide="BOTH") ETHUSDT orders when current positions are flat ([])
    orders = [
        {"orderId": "1", "sym": "ETHUSDT", "side": "SELL", "ps": "BOTH", "q": 100.0, "p": 1700.0, "t": 1000, "ot": 1000, "pnl": 0.0, "pnlKnown": True},
        {"orderId": "2", "sym": "ETHUSDT", "side": "SELL", "ps": "BOTH", "q": 70.0, "p": 1680.0, "t": 2000, "ot": 2000, "pnl": 0.0, "pnlKnown": True},
        {"orderId": "3", "sym": "ETHUSDT", "side": "BUY", "ps": "BOTH", "q": 60.0, "p": 1720.0, "t": 3000, "ot": 3000, "pnl": -1680.0, "pnlKnown": True},
        {"orderId": "4", "sym": "ETHUSDT", "side": "BUY", "ps": "BOTH", "q": 110.0, "p": 1740.0, "t": 4000, "ot": 4000, "pnl": -5280.0, "pnlKnown": True},
    ]
    cls = classify_order_actions(orders, open_positions=[], positions_confirmed=True)
    keys = [order_dedup_key(expand_slim_order(o)) for o in orders]
    assert [cls[k] for k in keys] == ["OPEN_SHORT", "ADD_SHORT", "REDUCE_SHORT", "CLOSE_SHORT"]

    batches = group_orders_into_batches(orders, gap_ms=100, classifications=cls)
    assert [b["dir"] for b in batches] == ["OPEN_SHORT", "ADD_SHORT", "REDUCE_SHORT", "CLOSE_SHORT"]

    cycles = reconstruct_trade_cycles(orders, classifications=cls)
    assert len(cycles) == 1
    assert cycles[0]["positionSide"] == "SHORT"
    assert cycles[0]["status"] == "CLOSED"
    assert cycles[0]["entryCount"] == 2
    assert cycles[0]["exitCount"] == 2


def test_classify_oneway_gapped_history_respects_zero_pnl_open_cue():
    # Window with pre-existing position before an opening gap + confirmed flat now
    orders = [
        {"orderId": "1", "sym": "ETHUSDT", "side": "SELL", "ps": "BOTH", "q": 50.0, "p": 1653.19, "t": 1000, "ot": 1000, "pnl": 0.0, "pnlKnown": True},
        {"orderId": "2", "sym": "ETHUSDT", "side": "BUY", "ps": "BOTH", "q": 92.803, "p": 1608.65, "t": 2000, "ot": 2000, "pnl": 7520.80, "pnlKnown": True},
        {"orderId": "3", "sym": "ETHUSDT", "side": "SELL", "ps": "BOTH", "q": 172.0, "p": 1632.83, "t": 3000, "ot": 3000, "pnl": 0.0, "pnlKnown": True},
        {"orderId": "4", "sym": "ETHUSDT", "side": "SELL", "ps": "BOTH", "q": 40.0, "p": 1670.23, "t": 4000, "ot": 4000, "pnl": 0.0, "pnlKnown": True},
        {"orderId": "5", "sym": "ETHUSDT", "side": "BUY", "ps": "BOTH", "q": 400.421, "p": 1714.35, "t": 5000, "ot": 5000, "pnl": -20432.45, "pnlKnown": True},
        {"orderId": "6", "sym": "ETHUSDT", "side": "SELL", "ps": "BOTH", "q": 100.0, "p": 1699.01, "t": 6000, "ot": 6000, "pnl": 0.0, "pnlKnown": True},
        {"orderId": "7", "sym": "ETHUSDT", "side": "SELL", "ps": "BOTH", "q": 70.0, "p": 1678.45, "t": 7000, "ot": 7000, "pnl": 0.0, "pnlKnown": True},
        {"orderId": "8", "sym": "ETHUSDT", "side": "BUY", "ps": "BOTH", "q": 60.163, "p": 1725.39, "t": 8000, "ot": 8000, "pnl": -2096.47, "pnlKnown": True},
        {"orderId": "9", "sym": "ETHUSDT", "side": "BUY", "ps": "BOTH", "q": 42.836, "p": 1742.15, "t": 9000, "ot": 9000, "pnl": -2210.51, "pnlKnown": True},
        {"orderId": "10", "sym": "ETHUSDT", "side": "BUY", "ps": "BOTH", "q": 34.84, "p": 1748.81, "t": 10000, "ot": 10000, "pnl": -2029.92, "pnlKnown": True},
    ]
    cls = classify_order_actions(orders, open_positions=[], positions_confirmed=True)
    keys = [order_dedup_key(expand_slim_order(o)) for o in orders]
    # All SELLs are SHORT entries (OPEN_SHORT or ADD_SHORT), all BUYs are SHORT exits (REDUCE_SHORT or CLOSE_SHORT)
    assert all(cls[keys[i]].endswith("_SHORT") for i in range(len(orders)))
    assert cls[keys[5]] == "OPEN_SHORT"
    assert cls[keys[6]] == "ADD_SHORT"
    assert cls[keys[7]] == "REDUCE_SHORT"
    assert cls[keys[8]] == "REDUCE_SHORT"
    assert cls[keys[9]] == "CLOSE_SHORT"


def test_classify_hedge_orders_when_flat():
    orders = [
        {"orderId": "a", "sym": "BTCUSDT", "side": "BUY", "ps": "LONG", "q": 1.0, "p": 50000, "t": 1000, "ot": 1000, "pnl": 0.0, "pnlKnown": True},
        {"orderId": "b", "sym": "BTCUSDT", "side": "BUY", "ps": "LONG", "q": 0.5, "p": 51000, "t": 2000, "ot": 2000, "pnl": 0.0, "pnlKnown": True},
        {"orderId": "c", "sym": "BTCUSDT", "side": "SELL", "ps": "LONG", "q": 0.5, "p": 52000, "t": 3000, "ot": 3000, "pnl": 500.0, "pnlKnown": True},
        {"orderId": "d", "sym": "BTCUSDT", "side": "SELL", "ps": "LONG", "q": 1.0, "p": 53000, "t": 4000, "ot": 4000, "pnl": 2000.0, "pnlKnown": True},
    ]
    cls = classify_order_actions(orders, open_positions=[], positions_confirmed=True)
    keys = [order_dedup_key(expand_slim_order(o)) for o in orders]
    assert [cls[k] for k in keys] == ["OPEN_LONG", "ADD_LONG", "REDUCE_LONG", "CLOSE_LONG"]


def test_oneway_both_open_short_reduce_short_and_cross_zero_reversals():
    # 1) BOTH SELL 开空 + 加空，再 BUY 减空：减空按比例消减成本，持仓均价保持不变
    raw_short = [
        {"orderId": "1", "symbol": "ETHUSDT", "side": "SELL", "positionSide": "BOTH", "type": "MARKET", "executedQty": "10", "avgPrice": "1700", "orderTime": 1000, "orderUpdateTime": 1000, "totalPnl": "0", "pnlKnown": True},
        {"orderId": "2", "symbol": "ETHUSDT", "side": "SELL", "positionSide": "BOTH", "type": "MARKET", "executedQty": "10", "avgPrice": "1800", "orderTime": 2000, "orderUpdateTime": 2000, "totalPnl": "0", "pnlKnown": True},
        {"orderId": "3", "symbol": "ETHUSDT", "side": "BUY", "positionSide": "BOTH", "type": "MARKET", "executedQty": "5", "avgPrice": "1720", "orderTime": 3000, "orderUpdateTime": 3000, "totalPnl": "150", "pnlKnown": True},
    ]
    store = {r["h"]: r for r in (slim_order(o) for o in raw_short)}
    positions = reconstruct_open_positions(store)
    assert len(positions) == 1
    assert positions[0]["symbol"] == "ETHUSDT"
    assert positions[0]["positionSide"] == "SHORT"
    assert positions[0]["qty"] == pytest.approx(15.0)
    assert positions[0]["avgEntry"] == pytest.approx(1750.0)

    batches_short = build_order_batches(raw_short, gap_ms=100)
    assert [b["dir"] for b in batches_short] == ["OPEN_SHORT", "ADD_SHORT", "REDUCE_SHORT"]
    assert [b["positionSide"] for b in batches_short] == ["SHORT", "SHORT", "SHORT"]
    assert [b["isClose"] for b in batches_short] == [False, False, True]

    # 2) BOTH BUY 平空后反手开多（带已实现盈亏，不得截断为 0）
    flip_buy = {
        "orderId": "4", "symbol": "ETHUSDT", "side": "BUY", "positionSide": "BOTH", "type": "MARKET",
        "executedQty": "25", "avgPrice": "1760", "orderTime": 4000, "orderUpdateTime": 4000,
        "totalPnl": "-150", "pnlKnown": True,
    }
    rec4 = slim_order(flip_buy)
    store[rec4["h"]] = rec4
    pos_after_buy_flip = reconstruct_open_positions(store)
    assert len(pos_after_buy_flip) == 1
    assert pos_after_buy_flip[0]["positionSide"] == "LONG"
    assert pos_after_buy_flip[0]["qty"] == pytest.approx(10.0)
    assert pos_after_buy_flip[0]["avgEntry"] == pytest.approx(1760.0)

    batches_flip_buy = build_order_batches([*raw_short, flip_buy], gap_ms=100)
    assert [b["dir"] for b in batches_flip_buy] == [
        "OPEN_SHORT", "ADD_SHORT", "REDUCE_SHORT", "CLOSE_SHORT", "OPEN_LONG",
    ]
    assert [b["qty"] for b in batches_flip_buy[-2:]] == [15.0, 10.0]
    assert [b["isClose"] for b in batches_flip_buy[-2:]] == [True, False]

    # 3) BOTH SELL 平多后反手开空（跨零轴拆两段）
    flip_sell = {
        "orderId": "5", "symbol": "ETHUSDT", "side": "SELL", "positionSide": "BOTH", "type": "MARKET",
        "executedQty": "18", "avgPrice": "1810", "orderTime": 5000, "orderUpdateTime": 5000,
        "totalPnl": "500", "pnlKnown": True,
    }
    rec5 = slim_order(flip_sell)
    store[rec5["h"]] = rec5
    pos_after_sell_flip = reconstruct_open_positions(store)
    assert len(pos_after_sell_flip) == 1
    assert pos_after_sell_flip[0]["positionSide"] == "SHORT"
    assert pos_after_sell_flip[0]["qty"] == pytest.approx(8.0)
    assert pos_after_sell_flip[0]["avgEntry"] == pytest.approx(1810.0)

    batches_flip_sell = build_order_batches([*raw_short, flip_buy, flip_sell], gap_ms=100)
    assert [b["dir"] for b in batches_flip_sell] == [
        "OPEN_SHORT", "ADD_SHORT", "REDUCE_SHORT", "CLOSE_SHORT", "OPEN_LONG", "CLOSE_LONG", "OPEN_SHORT",
    ]
    assert [b["qty"] for b in batches_flip_sell[-2:]] == [10.0, 8.0]
    assert [b["isClose"] for b in batches_flip_sell[-2:]] == [True, False]

    cycles = reconstruct_trade_cycles([*raw_short, flip_buy, flip_sell])
    assert [(c["positionSide"], c["status"], c["totalEntryQty"], c["totalExitQty"], c.get("netRemainingQty")) for c in cycles] == [
        ("SHORT", "OPEN", 8.0, 0.0, 8.0),
        ("LONG", "CLOSED", 10.0, 10.0, 0.0),
        ("SHORT", "CLOSED", 20.0, 20.0, 0.0),
    ]


def test_oneway_closed_rows_stale_filter_matches_reconstructed_long_short():
    # closed_rows 记录带 SHORT，不会用原始 BOTH 字符串比较，且按重建方向正确匹配/隔离
    open_short = slim_order({
        "orderId": "s1", "symbol": "ETHUSDT", "side": "SELL", "positionSide": "BOTH", "type": "MARKET",
        "executedQty": "5", "avgPrice": "1800", "orderTime": 2000, "orderUpdateTime": 2000,
    })
    store = {open_short["h"]: open_short}
    # 相反方向 LONG 的闭仓历史不能误杀重建出的 SHORT 持仓
    assert len(reconstruct_open_positions(store, closed_rows=[
        {"symbol": "ETHUSDT", "side": "LONG", "opened": 1000, "closed": 3000},
    ])) == 1
    # 同方向 SHORT 且覆盖 openTime 的闭仓历史才判定为已闭仓旧单
    assert reconstruct_open_positions(store, closed_rows=[
        {"symbol": "ETHUSDT", "side": "SHORT", "opened": 1000, "closed": 3000},
    ]) == []


def test_jinmu_55_both_orders_reconstructs_ethusdt_short_110_072_avg_1736_45():
    # 模拟金木超级大隐藏带单员 55 笔 positionSide=BOTH 订单：前 48 笔包含多轮开平与反手且全部归零，
    # 最后 7 笔构成 247.911 空仓（加权均价约 1736.45）+ 3 笔 BUY 减空共 137.839，最终剩余 SHORT 110.072 @ ~1736.45
    orders = []
    t = 1_795_000_000_000
    oid = 1

    def add_order(side: str, qty: str, price: str, pnl: str = "0"):
        nonlocal t, oid
        t += 1000
        orders.append({
            "orderId": f"jm-{oid}",
            "symbol": "ETHUSDT",
            "side": side,
            "positionSide": "BOTH",
            "type": "MARKET",
            "executedQty": qty,
            "avgPrice": price,
            "orderTime": t,
            "orderUpdateTime": t,
            "totalPnl": pnl,
            "pnlKnown": True,
        })
        oid += 1

    # 48 笔历史成交（包含跨零轴反手）：每轮 6 笔 × 8 轮 = 48 笔，每轮净归零
    for r in range(8):
        add_order("SELL", "50", "1700.00")
        add_order("SELL", "30", "1710.00")
        add_order("BUY", "40", "1690.00", "550")
        add_order("BUY", "60", "1680.00", "1100")   # 跨零轴：平余下40空并反手开20多
        add_order("SELL", "50", "1715.00", "700")   # 跨零轴：平20多并反手开30空
        add_order("BUY", "30", "1695.00", "600")    # 精准归零

    # 第 49~52 笔：4 笔 SELL 开/加空共 247.911，加权均价 ≈ 1736.45027
    add_order("SELL", "70", "1720.00")
    add_order("SELL", "65", "1735.00")
    add_order("SELL", "60", "1745.00")
    add_order("SELL", "52.911", "1750.30")

    # 第 53~55 笔：3 笔 BUY 减空共 137.839 -> 净剩 SHORT 110.072，均价仍保持 ~1736.45
    add_order("BUY", "60.163", "1725.39", "665.80")
    add_order("BUY", "42.836", "1742.15", "-244.15")
    add_order("BUY", "34.84", "1748.81", "-430.60")

    assert len(orders) == 55
    store = {rec["h"]: rec for rec in (slim_order(o) for o in orders)}
    assert len(store) == 55

    positions = reconstruct_open_positions(store)
    assert len(positions) == 1
    pos = positions[0]
    assert pos["symbol"] == "ETHUSDT"
    assert pos["positionSide"] == "SHORT"
    assert pos["qtyExact"] == "110.072"
    assert pos["qty"] == pytest.approx(110.072, abs=1e-6)
    assert pos["avgEntry"] == pytest.approx(1736.45, abs=0.01)

    batches = build_order_batches(orders, gap_ms=100)
    assert all(b["positionSide"] in ("LONG", "SHORT") for b in batches)
    assert all(b["dir"].startswith(("OPEN_", "ADD_", "REDUCE_", "CLOSE_")) for b in batches)
    assert [b["dir"] for b in batches[-7:]] == [
        "OPEN_SHORT", "ADD_SHORT", "ADD_SHORT", "ADD_SHORT",
        "REDUCE_SHORT", "REDUCE_SHORT", "REDUCE_SHORT",
    ]

    cycles = reconstruct_trade_cycles(orders)
    open_cycles = [c for c in cycles if c["status"] == "OPEN"]
    assert len(open_cycles) == 1
    assert open_cycles[0]["positionSide"] == "SHORT"
    assert open_cycles[0]["netRemainingQty"] == pytest.approx(110.072, abs=1e-6)
