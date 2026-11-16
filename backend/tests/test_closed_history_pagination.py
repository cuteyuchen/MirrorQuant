"""闭仓分页连续性回归；残缺页面不能推进已确认游标。"""
from unittest.mock import AsyncMock

import pytest

from app.services.lead_collector.binance_bapi import BinanceBapiCollector, normalize_holdings, reconstruct_open_positions, slim_closed_position


def history(count):
    return [{"positionId": str(i), "symbol": "BTCUSDT", "side": "LONG",
             "opened": 1000 + i * 100, "closed": 1050 + i * 100} for i in reversed(range(count))]


def pages(rows, total=None):
    return [{"list": rows[i:i + 100], "total": len(rows) if total is None else total}
            for i in range(0, len(rows), 100)]


async def test_first_sync_reads_all_pages_instead_of_stopping_at_one_hundred():
    collector = BinanceBapiCollector()
    rows = history(230)
    collector.fetch_position_history = AsyncMock(side_effect=pages(rows))
    collected, complete, total, cursor = await collector.collect_closed_history("lead", [])
    assert collected == rows and complete is True and total == 230
    assert collector.fetch_position_history.await_count == 3
    assert [call.kwargs["page_number"] for call in collector.fetch_position_history.await_args_list] == [1, 2, 3]
    assert cursor[0] == rows[0]["positionId"]


async def test_active_lead_completes_when_history_connects_to_last_seen_position():
    collector = BinanceBapiCollector()
    rows = history(300)
    previous = slim_closed_position(rows[150])
    collector.fetch_position_history = AsyncMock(side_effect=pages(rows, total=9000))
    collected, complete, total, cursor = await collector.collect_closed_history("lead", [previous])
    assert complete is True and total == 9000 and len(collected) == 200
    assert collector.fetch_position_history.await_count == 2 and cursor[0] == rows[0]["positionId"]


async def test_cap_never_advances_existing_anchor_or_claims_continuity():
    collector = BinanceBapiCollector()
    rows = history(400)
    old = rows[-1]
    anchor = [old["positionId"], old["symbol"], old["side"], old["opened"], old["closed"]]
    collector.fetch_position_history = AsyncMock(side_effect=pages(rows))
    collected, complete, _, cursor = await collector.collect_closed_history(
        "lead", [], {"meta": {"closedHistoryCursor": anchor}}, max_pages=2)
    assert complete is False and cursor == anchor and len(collected) == 200
    # 本轮残片已写缓存也不等于追平旧游标，下一轮仍需追到同一个锚点。
    collector.fetch_position_history = AsyncMock(side_effect=pages(rows))
    _, complete, _, cursor = await collector.collect_closed_history(
        "lead", [slim_closed_position(row) for row in collected],
        {"meta": {"closedHistoryCursor": cursor}}, max_pages=2)
    assert not complete and cursor == anchor


async def test_initial_cap_with_no_anchor_cannot_promote_partial_cache_next_round():
    # 第一次触顶没有已确认锚点，缓存首屏不构成下一轮连续性证明。
    collector = BinanceBapiCollector()
    rows = history(400)
    collector.fetch_position_history = AsyncMock(side_effect=pages(rows))
    collected, complete, _, cursor = await collector.collect_closed_history("lead", [], max_pages=2)
    assert complete is False and cursor is None
    collector.fetch_position_history = AsyncMock(side_effect=pages(rows))
    _, complete, _, cursor = await collector.collect_closed_history(
        "lead", [slim_closed_position(row) for row in collected],
        {"meta": {"closedHistoryCursor": None}}, max_pages=2)
    assert complete is False and cursor is None


async def test_missing_total_can_complete_only_when_prior_anchor_is_reached():
    # 没有 total 时可以靠旧锚点衔接，不能凭短页宣称全历史已确认。
    collector = BinanceBapiCollector()
    rows = history(150)
    responses = [{"list": rows[:100]}, {"list": rows[100:]}]
    collector.fetch_position_history = AsyncMock(side_effect=responses)
    assert (await collector.collect_closed_history("lead", []))[1] is False
    collector.fetch_position_history = AsyncMock(side_effect=responses)
    _, complete, total, _ = await collector.collect_closed_history("lead", [slim_closed_position(rows[120])])
    assert complete is True and total is None


async def test_snapshot_marks_order_history_complete_only_after_continuity_check():
    # 空短页完成实际历史采集，其他网络入口均禁止调用。
    collector = BinanceBapiCollector()
    collector.fetch_detail = AsyncMock(return_value={"positionShow": False, "marginBalance": "1000"})
    collector.fetch_order_history = AsyncMock(return_value=[])
    collector._http_json = AsyncMock(side_effect=AssertionError("unexpected external call"))
    snapshot = await collector.sync_snapshot("lead", {}, [], closed_history=[], with_analysis=False)
    assert snapshot["meta"]["orderHistoryComplete"] is True
    collector.fetch_order_history.assert_awaited_once()
    collector._http_json.assert_not_awaited()


@pytest.mark.parametrize("issue", ["failed_page", "changed_total", "repeated_page", "bad_order"])
async def test_paging_failures_do_not_claim_complete_history(issue):
    collector = BinanceBapiCollector()
    rows = history(250)
    responses = pages(rows)
    if issue == "failed_page":
        responses[1] = RuntimeError("page unavailable")
    elif issue == "changed_total":
        responses[1]["total"] = 251
    elif issue == "repeated_page":
        responses[1] = responses[0]
    else:
        responses[1]["list"] = [rows[0], rows[1], {**rows[2], "positionId": "out-of-order"}]
    collector.fetch_position_history = AsyncMock(side_effect=responses)
    if issue == "failed_page":
        with pytest.raises(RuntimeError, match="page unavailable"):
            await collector.collect_closed_history("lead", [])
    else:
        assert (await collector.collect_closed_history("lead", []))[1] is False


@pytest.mark.parametrize("symbol,base", [("BTCUSDT", "BTC"), ("BTCUSDC", "BTC"), ("USDTUSDC", "USDT")])
def test_base_asset_only_removes_usdt_or_usdc_suffix(symbol, base):
    direct = normalize_holdings([{"symbol": symbol, "positionAmount": "1", "positionSide": "LONG", "entryPrice": "100"}])
    assert direct[0]["baseAsset"] == base
    reconstructed = reconstruct_open_positions({"one": {"h": "one", "sym": symbol, "ps": "LONG",
                                                       "side": "BUY", "q": "1", "p": "100", "ot": 1000}})
    assert reconstructed[0]["baseAsset"] == base
