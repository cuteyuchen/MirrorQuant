from app.services.lead_collector.binance_bapi import (
    direction_label,
    expand_slim_order,
    group_orders_into_batches,
    is_open_leg,
    order_dedup_key,
    reconstruct_trade_cycles,
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
