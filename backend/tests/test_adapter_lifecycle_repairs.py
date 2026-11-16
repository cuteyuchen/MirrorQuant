"""拒单分类、限流隔离与长连接生命周期，HTTP 全部由 MockTransport 接管。"""
import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.services.adapters.base import BaseExchangeAdapter, ExchangeAPIError, InstrumentSpec, OrderRejected, api_error
from app.services.adapters.binance import BinanceAdapter
from app.services.adapters.bitget import BitgetAdapter
from app.services.adapters.bybit import BybitAdapter
from app.services.adapters.factory import (
    close_exchange_adapters, create_exchange_adapter, invalidate_account_adapter, invalidate_proxy_adapters,
)
from app.services.adapters.okx import OKXAdapter
from app.services.engine.router import CopyRouter, UnknownOrderOutcome
from app.services.lead_collector.binance_bapi import BinanceBapiCollector
from app.services.proxy.manager import ProxyEgressError, ProxyManager


def instrument():
    return InstrumentSpec("BTCUSDT", Decimal("1"), Decimal("0.1"), Decimal("0.1"), Decimal("0.1"))


@pytest.mark.parametrize("exchange,code,status,rejected", [
    ("binance", "-2019", 400, True), ("binance", "-1013", 400, True),
    ("binance", "-1007", 200, False), ("binance", "-2019", 503, False),
    ("okx", "51008", 200, True), ("okx", "50004", 200, False),
    ("okx", "51011", 200, False), ("okx", "51016", 200, False),
    ("binance", "unparseable", 400, False),
    ("binance", "-999999", 400, False), ("okx", "999999", 200, False),
    ("binance", "-2019", 408, False),  # 未知数字码和请求超时不能证明拒单。
    # 查单/撤单语义、官方文档查不到的码、未核实的交易所都不能当作明确拒单。
    ("binance", "-2013", 400, False), ("binance", "-2011", 400, False), ("binance", "-4112", 400, False),
    ("binance", "-4116", 400, False), ("binance", "-1001", 400, False),
    ("okx", "51603", 200, False), ("okx", "51004", 200, False), ("okx", "51119", 200, False),
    ("bybit", "110001", 200, False), ("bybit", "110072", 200, False), ("bybit", "10016", 200, False),
    ("bybit", "110007", 200, True), ("bitget", "40762", 200, False), ("bitget", "40001", 400, False),
])
def test_only_definite_business_rejections_are_terminal(exchange, code, status, rejected):
    assert isinstance(api_error(exchange, code, "synthetic", http_status=status), OrderRejected) is rejected


def test_duplicate_order_message_keeps_lookup_even_with_rejection_code():
    # 重复客户单号可能指向已受理的订单，不能直接终结。
    assert not isinstance(api_error("binance", "-2010", "Duplicate order sent."), OrderRejected)


@pytest.mark.parametrize("message", ["", "Order ID already exists", "既有订单身份冲突"])
@pytest.mark.parametrize("existing", [False, True])
async def test_okx_existing_order_id_requires_lookup_regardless_of_message(message, existing):
    # 既有订单身份不能依赖英文关键词分类；查不到仍未知，且绝不重发 POST。
    venue = OKXAdapter({})
    venue.validate_order = AsyncMock()
    venue._request = AsyncMock(side_effect=api_error("okx", "51011", message))
    venue.get_order = AsyncMock(return_value={
        "ordId": "existing", "accFillSz": "1", "avgPx": "100", "state": "filled",
    } if existing else None)
    if existing:
        result = await CopyRouter._place_order_idempotent(
            venue, "BTC-USDT-SWAP", "buy", "long", "ioc", "1", "100", "synthetic", False, "cross")
        assert result.ord_id == "existing" and result.fill_sz == 1
    else:
        with pytest.raises(UnknownOrderOutcome):
            await CopyRouter._place_order_idempotent(
                venue, "BTC-USDT-SWAP", "buy", "long", "ioc", "1", "100", "synthetic", False, "cross")
    venue._request.assert_awaited_once()
    venue.get_order.assert_awaited_once_with("BTC-USDT-SWAP", cl_ord_id="synthetic")


@pytest.mark.parametrize("kind,payload,code", [
    (BinanceAdapter, {"code": "-2019", "msg": "insufficient margin"}, "-2019"),
    (OKXAdapter, {"code": "1", "data": [{"sCode": "51008", "sMsg": "insufficient margin"}]}, "51008"),
    (BybitAdapter, {"retCode": 110004, "retMsg": "insufficient margin"}, "110004"),
    (BitgetAdapter, {"code": "40762", "msg": "rejected"}, "40762"),
])
@pytest.mark.parametrize("status", [200, 302, 400, 503])
async def test_transport_preserves_business_code_but_5xx_stays_ambiguous(monkeypatch, kind, payload, code, status):
    # 同一业务码不能覆盖 HTTP 5xx 的受理歧义，且 POST 一律只发送一次。
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(status, json=payload)
    monkeypatch.setattr(ProxyManager, "build_client", lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    with pytest.raises(ExchangeAPIError) as error:
        await kind({})._request("POST", "/synthetic-order")
    assert error.value.code == code
    # Bitget 的拒单码未能对照官方文档核实，因此任何状态下都保持“结果未知”。
    assert isinstance(error.value, OrderRejected) is (status in (200, 400) and kind is not BitgetAdapter)
    assert len(calls) == 1


@pytest.mark.parametrize("kind", [BinanceAdapter, OKXAdapter, BybitAdapter, BitgetAdapter])
async def test_unparseable_4xx_is_not_a_definite_rejection(monkeypatch, kind):
    # 网关返回 HTML 时无法解析真实受理结果，必须留下查单分支。
    monkeypatch.setattr(ProxyManager, "build_client", lambda **_: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(400, text="<html>upstream unavailable</html>"))))
    with pytest.raises(json.JSONDecodeError):
        await kind({})._request("POST", "/synthetic-order")


async def test_binance_success_code_200_still_returns_response(monkeypatch):
    # 币安部分成功接口使用正数 200，不能误报为业务失败。
    payload = {"code": 200, "msg": "success"}
    monkeypatch.setattr(ProxyManager, "build_client", lambda **_: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))))
    assert await BinanceAdapter({})._request("POST", "/synthetic-setting") == payload


async def test_invalid_local_order_type_does_not_fetch_market_data():
    # 本地参数拒绝不得被行情超时掩盖，更不能尝试实际提交。
    venue = BinanceAdapter({})
    venue.get_instruments, venue.get_orderbook = AsyncMock(), AsyncMock()
    with pytest.raises(OrderRejected, match="Unsupported order type"):
        await venue.validate_order("BTCUSDT", "unsupported", "1")
    venue.get_instruments.assert_not_awaited()
    venue.get_orderbook.assert_not_awaited()


async def test_binance_local_validation_rejects_without_post_or_lookup():
    venue = BinanceAdapter({})
    venue.get_instruments = AsyncMock(return_value={"BTCUSDT": instrument()})
    venue._request, venue.get_order = AsyncMock(), AsyncMock()
    with pytest.raises(OrderRejected, match="LOCAL_VALIDATION"):
        await CopyRouter._place_order_idempotent(venue, "BTCUSDT", "buy", "long", "ioc",
                                                "0.01", "100", "synthetic", False, "cross")
    venue._request.assert_not_awaited()
    venue.get_order.assert_not_awaited()


async def test_binance_requests_result_response_and_maps_self_match_expiry():
    venue = BinanceAdapter({})
    venue.validate_order = AsyncMock()
    venue._request = AsyncMock(return_value={"orderId": 1, "clientOrderId": "synthetic", "status": "EXPIRED_IN_MATCH"})
    result = await venue.place_order("BTCUSDT", "buy", "long", "ioc", "1", "100", "synthetic")
    assert venue._request.await_args.kwargs["params"]["newOrderRespType"] == "RESULT"
    assert CopyRouter._state(result.state) == "expired"


@pytest.mark.parametrize("path", ["/api/v5/trade/order", "/api/v5/trade/order-algo"])
@pytest.mark.parametrize("top_code", ["1", "2"])
async def test_okx_promotes_nested_business_error(monkeypatch, path, top_code):
    def respond(request):
        return httpx.Response(200, json={"code": top_code, "msg": "batch failure",
                                         "data": [{"sCode": "51008", "sMsg": "insufficient margin"}]})
    monkeypatch.setattr(ProxyManager, "build_client", lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    with pytest.raises(OrderRejected) as error:
        await OKXAdapter({})._request("POST", path, body={})
    assert error.value.code == "51008" and error.value.msg == "insufficient margin"


async def test_rate_cooldown_isolated_by_account_route_and_public_private():
    first = BinanceAdapter({"api_key": "account-a"}, proxy_url="http://proxy-a.invalid")
    same = BinanceAdapter({"api_key": "account-a"}, proxy_url="http://proxy-a.invalid")
    other = BinanceAdapter({"api_key": "account-b"}, proxy_url="http://proxy-a.invalid")
    other_route = BinanceAdapter({"api_key": "account-a"}, proxy_url="http://proxy-b.invalid")
    response = httpx.Response(429, headers={"Retry-After": "30"})
    first._note_rate_limit(response, auth=True)
    with pytest.raises(OrderRejected, match="cooldown"):
        same._check_rate_limit(True)
    first._check_rate_limit(False)
    other._check_rate_limit(True)
    other_route._check_rate_limit(True)
    # 其他账户的冷却不能阻挡本账户 reduce-only 下单。
    other.validate_order = AsyncMock()
    other._request = AsyncMock(return_value={"orderId": 1, "status": "FILLED", "executedQty": "1", "avgPrice": "100"})
    await other.place_order("BTCUSDT", "sell", "long", "market", "1", reduce_only=True)
    other._request.assert_awaited_once()
    first._note_rate_limit(response, auth=False)
    with pytest.raises(OrderRejected):
        other._check_rate_limit(False)  # 同一出口的公共额度可共享，但不污染私有额度。
    other._check_rate_limit(True)


async def test_adapter_pool_credentials_proxy_and_shutdown_invalidation(monkeypatch):
    constructed = []
    def client(**kwargs):
        result = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})))
        constructed.append(result)
        return result
    monkeypatch.setattr(ProxyManager, "build_client", client)
    first = create_exchange_adapter("binance", {"api_key": "a"}, proxy_url="http://proxy.invalid", account_id=1)
    assert first is create_exchange_adapter("binance", {"api_key": "a"}, proxy_url="http://proxy.invalid", account_id=1)
    await first._request("GET", "/one")
    await first._request("GET", "/two")
    assert len(constructed) == 1
    changed = create_exchange_adapter("binance", {"api_key": "b"}, proxy_url="http://proxy.invalid", account_id=1)
    assert changed is not first
    await changed._request("GET", "/changed")
    await invalidate_account_adapter(1)
    assert constructed[-1].is_closed
    replacement = create_exchange_adapter("binance", {"api_key": "b"}, proxy_url="http://proxy.invalid", account_id=1)
    assert replacement is not changed
    await replacement._request("GET", "/replacement")
    await invalidate_proxy_adapters("http://proxy.invalid")
    assert constructed[-1].is_closed
    await close_exchange_adapters()
    assert all(client.is_closed for client in constructed)


@pytest.mark.parametrize("kind,payload", [
    (BinanceAdapter, {}),
    (OKXAdapter, {"code": "0", "data": []}),
    (BybitAdapter, {"retCode": 0, "result": {}}),
    (BitgetAdapter, {"code": "00000", "data": {}}),
])
@pytest.mark.parametrize("first_auth", [True, False])
async def test_request_auth_isolated_when_account_connections_are_reused(monkeypatch, kind, payload, first_auth):
    # 两个账户共用出口，但认证字段只能存在于本次私有请求。
    fields = {
        BinanceAdapter: ("x-mbx-apikey", None, None, None),
        OKXAdapter: ("ok-access-key", "ok-access-sign", "ok-access-timestamp", "ok-access-passphrase"),
        BybitAdapter: ("x-bapi-api-key", "x-bapi-sign", "x-bapi-timestamp", None),
        BitgetAdapter: ("access-key", "access-sign", "access-timestamp", "access-passphrase"),
    }
    key_field, sign_field, time_field, phrase_field = fields[kind]
    auth_fields = {field for names in fields.values() for field in names if field}
    auth_fields.update(("authorization", "signature", "timestamp", "recvwindow",
                        "x-bapi-recv-window", "x-bapi-sign-type"))
    clients, requests, pools = [], [], {}
    clock = [1800000000.0]

    def client(**kwargs):
        assert kwargs["proxy_url"] == "http://proxy.invalid"
        # 保留构造默认头，否则把签名写入连接池的回归会被 Mock 掩盖。
        headers = {**ProxyManager.DEFAULT_HEADERS, **(kwargs.get("headers") or {})}
        index = len(clients)
        def respond(request):
            requests.append((index, request))
            return httpx.Response(200, json=payload)
        result = httpx.AsyncClient(headers=headers, transport=httpx.MockTransport(respond),
                                   trust_env=False, follow_redirects=False)
        clients.append(result)
        return result

    monkeypatch.setattr(ProxyManager, "build_client", client)
    monkeypatch.setattr("time.time", lambda: clock[0])
    venues = [kind({"api_key": f"synthetic-{name}", "secret_key": f"synthetic-secret-{name}",
                    "passphrase": f"synthetic-phrase-{name}"}, proxy_url="http://proxy.invalid")
              for name in ("a", "b")]
    steps = [(venues[0], "GET"), (venues[1], "GET"), (venues[0], "GET"), (venues[1], "GET"),
             (venues[0], "POST"), (venues[1], "POST")]
    for auth in (first_auth, not first_auth):
        for venue, method in steps:
            clock[0] += 1
            now_ms = int(clock[0] * 1000)
            # 每次提供新字典，且立即确认调用方数据没有被注入认证字段。
            params = {"symbol": "BTCUSDT"}
            body = {"symbol": "BTCUSDT", "side": "BUY"}
            original_params, original_body = params.copy(), body.copy()
            kwargs = {"params": params, "auth": auth, "retries": 1}
            if method == "POST" and kind is not BinanceAdapter:
                kwargs["body"] = body
            await venue._request(method, "/synthetic-headers", **kwargs)
            assert params == original_params and body == original_body
            selected = venue._http_clients[auth]
            assert selected is pools.setdefault((venue, auth), selected)
            client_index, request = requests[-1]
            assert clients[client_index] is selected and request.method == method
            # 不能把残留查询字段重新签名后也当成正确请求。
            if kind is not BinanceAdapter or not auth:
                assert request.url.params == httpx.QueryParams(original_params)
            if method == "POST" and kind is not BinanceAdapter:
                assert json.loads(request.content) == original_body
            assert all(auth_fields.isdisjoint(item.headers) for item in clients)
            if not auth:
                assert auth_fields.isdisjoint(request.headers)
                assert {"signature", "timestamp", "recvWindow"}.isdisjoint(request.url.params)
                continue
            expected_fields = {field for field in fields[kind] if field}
            if kind is BybitAdapter:
                expected_fields.update(("x-bapi-recv-window", "x-bapi-sign-type"))
            assert set(request.headers).intersection(auth_fields) == expected_fields
            assert request.headers[key_field] == venue.api_key
            if kind is BinanceAdapter:
                # 币安的时间戳与签名在 URL 查询中，不是请求头。
                expected_query = venue._sign_query({**original_params, "timestamp": now_ms, "recvWindow": 5000})
                assert request.url.query.decode() == expected_query
            else:
                timestamp = (datetime.fromtimestamp(clock[0], timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
                             if kind is OKXAdapter else str(now_ms))
                assert request.headers[time_field] == timestamp
                body_str = json.dumps(original_body, separators=(",", ":")) if method == "POST" else ""
                if kind is BybitAdapter:
                    assert request.headers["x-bapi-recv-window"] == "5000"
                    assert request.headers["x-bapi-sign-type"] == "2"
                    signed_payload = request.url.query.decode() if method == "GET" else body_str
                    signature = venue._sign(timestamp, "5000", signed_payload)
                else:
                    signature = venue._sign(timestamp, method, request.url.raw_path.decode(), body_str)
                assert request.headers[sign_field] == signature
                if phrase_field:
                    assert request.headers[phrase_field] == venue.passphrase
    assert len(requests) == 12 and len(clients) == len(set(pools.values())) == 4
    await close_exchange_adapters()
    assert all(item.is_closed for item in clients)


async def test_contract_metadata_cache_shared_across_accounts():
    first, second = BinanceAdapter({"api_key": "a"}), BinanceAdapter({"api_key": "b"})
    first._request = AsyncMock(return_value={"symbols": []})
    second._request = AsyncMock(side_effect=AssertionError("cache miss"))
    assert await first.get_instruments() == {}
    assert await second.get_instruments() == {}
    first._request.assert_awaited_once()
    second._request.assert_not_awaited()
    second._request = AsyncMock(return_value={"symbols": []})
    await second.get_instruments(force_refresh=True)
    second._request.assert_awaited_once()


async def test_client_cleanup_attempts_every_client_and_redacts_failures(caplog):
    # 清理错误不能泄露连接异常中携带的凭据，也不能中断其他池的释放。
    broken = Mock(aclose=AsyncMock(side_effect=RuntimeError("private-cleanup-credential")))
    healthy = Mock(aclose=AsyncMock())
    venue = BinanceAdapter({})
    venue._http_clients = {True: broken, False: healthy}
    BaseExchangeAdapter._live_clients.update((broken, healthy))
    await venue.aclose()
    broken.aclose.assert_awaited_once()
    healthy.aclose.assert_awaited_once()
    assert venue._http_clients == {} and not BaseExchangeAdapter._live_clients
    assert "HTTP client cleanup failed (RuntimeError)" in caplog.text
    assert "private-cleanup-credential" not in caplog.text


async def test_bapi_shutdown_attempts_all_clients_even_when_one_close_fails(caplog):
    # 公共采集池遵循相同的关闭规则，无任何实际网络请求。
    broken = Mock(aclose=AsyncMock(side_effect=RuntimeError("private-cleanup-credential")))
    healthy = Mock(aclose=AsyncMock())
    loop = asyncio.get_running_loop()
    BinanceBapiCollector._http_clients.update({(loop, "broken"): broken, (loop, "healthy"): healthy})
    await BinanceBapiCollector.close_clients()
    broken.aclose.assert_awaited_once()
    healthy.aclose.assert_awaited_once()
    assert BinanceBapiCollector._http_clients == {}
    assert "private-cleanup-credential" not in caplog.text


async def test_credential_rotation_reports_background_close_errors(caplog):
    # 后台释放旧适配器失败也要取回异常，不产生无人处理的任务异常。
    first = create_exchange_adapter("binance", {"api_key": "a"}, account_id=1)
    first.aclose = AsyncMock(side_effect=RuntimeError("private-cleanup-credential"))
    create_exchange_adapter("binance", {"api_key": "b"}, account_id=1)
    await close_exchange_adapters()
    first.aclose.assert_awaited_once()
    assert "Adapter cleanup failed (RuntimeError)" in caplog.text
    assert "private-cleanup-credential" not in caplog.text


async def test_bapi_clients_reuse_connections_but_keep_request_headers_separate(monkeypatch):
    clients, requests = [], []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"code": "000000", "data": {}})
    def client(**kwargs):
        result = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        clients.append(result)
        return result
    monkeypatch.setattr(ProxyManager, "build_client", client)
    collector = BinanceBapiCollector()
    for portfolio in ("first", "second"):
        await collector._http_json("https://example.invalid/history", portfolio_id=portfolio,
                                   proxy_url="http://proxy.invalid", retries=1)
    assert len(clients) == 1 and len(requests) == 2
    assert "first" in requests[0].headers["referer"] and "second" in requests[1].headers["referer"]
    await BinanceBapiCollector.close_clients()
    assert clients[0].is_closed


async def test_bapi_proxy_invalidation_is_targeted_and_fapi_reuses_pool(monkeypatch):
    # 只关闭被修改的出口，公共行情回补也复用连接。
    clients = []
    def client(**kwargs):
        result = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"code": "000000", "data": {}})))
        clients.append(result)
        return result
    monkeypatch.setattr(ProxyManager, "build_client", client)
    collector = BinanceBapiCollector()
    await collector._http_json("https://example.invalid/history", proxy_url="http://proxy-a.invalid", retries=1)
    await collector._fapi_get("/fapi/v1/time", proxy_url="http://proxy-a.invalid", retries=1)
    await collector._http_json("https://example.invalid/history", proxy_url="http://proxy-b.invalid", retries=1)
    await collector._http_json("https://example.invalid/history", proxy_url=None, retries=1)
    assert len(clients) == 3
    await collector.close_proxy_clients("http://proxy-a.invalid")
    assert clients[0].is_closed and not clients[1].is_closed and not clients[2].is_closed
    await collector.close_proxy_clients(None)
    assert clients[2].is_closed and not clients[1].is_closed
    await collector.close_clients()
    assert all(item.is_closed for item in clients)


@pytest.mark.parametrize("url", ["", " "])
def test_assigned_empty_proxy_cannot_turn_into_direct_egress(url):
    # 损坏的代理配置也必须失败关闭，不把空 URL 解释成直连。
    node = SimpleNamespace(id=1, is_enabled=True, is_healthy=True, url=url)
    with pytest.raises(ProxyEgressError, match="Assigned proxy"):
        ProxyManager.select_proxy_url([node], preferred_proxy_id=1, exchange="okx")


@pytest.mark.parametrize("rollback_fails", [False, True])
async def test_binance_partial_tp_sl_rolls_back_only_new_tp(rollback_fails, caplog):
    venue = BinanceAdapter({})
    effects = [{"algoId": "new-tp"}, OrderRejected("binance", "-2019", "SL rejected")]
    effects.append(RuntimeError("rollback failed") if rollback_fails else {})
    venue._request = AsyncMock(side_effect=effects)
    with pytest.raises(RuntimeError, match="rollback failed" if rollback_fails else "SL rejected"):
        await venue.place_tp_sl("BTCUSDT", "long", "1", tp_px="110", sl_px="90")
    last = venue._request.await_args
    assert last.args == ("DELETE", "/fapi/v1/algoOrder") and last.kwargs["params"]["algoId"] == "new-tp"
    if rollback_fails:
        assert "TP rollback failed" in caplog.text


async def test_binance_cancellation_failure_is_reported_and_logged(caplog):
    venue = BinanceAdapter({})
    venue._request = AsyncMock(side_effect=RuntimeError("synthetic unavailable"))
    with pytest.raises(RuntimeError, match="unavailable"):
        await venue.cancel_tp_sl("BTCUSDT", "long")
    assert "Protective cancellation failed" in caplog.text


async def test_binance_malformed_protective_inventory_is_not_reported_as_empty(caplog):
    # 无法核验库存属于失败，不能静默报告没有保护单。
    venue = BinanceAdapter({})
    venue._request = AsyncMock(return_value={})
    with pytest.raises(ValueError, match="Incomplete"):
        await venue.cancel_tp_sl("BTCUSDT", "long")
    assert "Protective cancellation failed" in caplog.text
