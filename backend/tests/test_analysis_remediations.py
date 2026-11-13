from unittest.mock import Mock

import pytest

from app.core.config import get_settings
from app.services.analysis.pa_analyzer import PAAnalyzer, calc_rsi
from app.services.proxy.manager import ProxyManager


NOW = 1_800_000_000.0


def candles(interval_seconds=3600, age_seconds=3600, falling=False):
    return [
        {
            "ot": int((NOW - age_seconds - (119 - i) * interval_seconds) * 1000),
            "ct": int((NOW - age_seconds - (118 - i) * interval_seconds) * 1000 - 1),
            "o": float(200 - i if falling else 100),
            "h": float(201 - i if falling else 101),
            "l": float(199 - i if falling else 99),
            "c": float(200 - i if falling else 100),
            "v": 1.0,
        }
        for i in range(120)
    ]


def analyze(analyzer, c1h, c4h, **kwargs):
    return analyzer.analyze_from_candles("BTCUSDT", "LONG", 100.0, c1h, c4h, **kwargs)


@pytest.fixture(autouse=True)
def analysis_clock(monkeypatch):
    monkeypatch.setattr("app.services.analysis.pa_analyzer.time.time", lambda: NOW)
    monkeypatch.setattr(get_settings(), "pa_kline_max_staleness_multiplier", 3.0)


@pytest.mark.parametrize("values, expected", [
    ([100.0] * 30, 50.0),
    (list(range(30)), 100.0),
    (list(reversed(range(30))), 0.0),
    ([100.0] * 5, None),
])
def test_rsi_extremes_and_flat_series(values, expected):
    assert calc_rsi(values) == expected


def test_zero_rsi_is_preserved_in_both_summaries():
    result = analyze(PAAnalyzer(), candles(falling=True), candles(14400, 14400, falling=True))
    assert result["rsi1h"] == result["rsi4h"] == 0.0
    assert "RSI 0/0" in result["summary"]
    assert "RSI 0/0" in result["summaryZh"]


def test_fresh_analysis_reports_source_timestamp_not_wall_clock():
    result = analyze(PAAnalyzer(), candles(), candles(14400, 14400))
    assert result["stale"] is False
    assert result["asOf"] == result["analyzedAt"] == int((NOW - 14400) * 1000)


@pytest.mark.parametrize("interval", ["1h", "4h"])
def test_either_stale_timeframe_marks_analysis_stale(interval):
    c1h = candles(age_seconds=10801 if interval == "1h" else 3600)
    c4h = candles(14400, 43201 if interval == "4h" else 14400)
    result = analyze(PAAnalyzer(), c1h, c4h, current_price=200.0)
    assert result["stale"] is True
    assert result["asOf"] == min(c1h[-1]["ot"], c4h[-1]["ot"])
    assert result["priceVsEntryPct"] == 0.0  # Do not mix a new ticker into stale candles.


def test_staleness_bound_is_configurable(monkeypatch):
    monkeypatch.setattr(get_settings(), "pa_kline_max_staleness_multiplier", 0.5)
    assert analyze(PAAnalyzer(), candles(), candles(14400, 14400))["stale"] is True


def test_missing_source_timestamp_never_reports_fresh_analysis():
    c1h = candles()
    c1h[-1].pop("ot")
    result = analyze(PAAnalyzer(), c1h, candles(14400, 14400))
    assert result["stale"] is True
    assert result["asOf"] is result["analyzedAt"] is None


def test_future_close_time_does_not_advance_source_timestamp():
    c1h = candles(age_seconds=5)
    c4h = candles(14400, 5)
    assert c1h[-1]["ct"] > NOW * 1000
    result = analyze(PAAnalyzer(), c1h, c4h)
    assert result["stale"] is False
    assert result["asOf"] == int((NOW - 5) * 1000)


class KlineClient:
    def __init__(self, rows=None):
        self.rows = rows
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def get(self, _):
        self.calls += 1
        raw = [[c[k] for k in ("ot", "o", "h", "l", "c", "v", "ct")] for c in self.rows or []]
        return Mock(status_code=200 if self.rows else 503, json=lambda: raw)


@pytest.mark.asyncio
async def test_fresh_cached_klines_avoid_network(monkeypatch):
    analyzer = PAAnalyzer()
    rows = candles()
    analyzer._kline_cache[("BTCUSDT", "1h")] = {"at": NOW - 1, "rows": rows}
    build = Mock(side_effect=AssertionError("Fresh cache should not refetch"))
    monkeypatch.setattr(ProxyManager, "build_client", build)
    assert await analyzer.fetch_klines("BTCUSDT") is rows
    build.assert_not_called()


@pytest.mark.asyncio
async def test_stale_data_refetches_even_with_recent_cache_write(monkeypatch):
    analyzer = PAAnalyzer()
    analyzer._kline_cache[("BTCUSDT", "1h")] = {"at": NOW - 1, "rows": candles(age_seconds=20000)}
    client = KlineClient(candles())
    monkeypatch.setattr(ProxyManager, "build_client", lambda **_: client)
    rows = await analyzer.fetch_klines("BTCUSDT")
    assert client.calls == 1
    assert rows[-1]["ot"] == int((NOW - 3600) * 1000)


@pytest.mark.asyncio
async def test_failed_refetch_exposes_stale_cache_timestamp(monkeypatch):
    analyzer = PAAnalyzer()
    c1h, c4h = candles(age_seconds=20000), candles(14400, 50000)
    analyzer._kline_cache[("BTCUSDT", "1h")] = {"at": NOW - 1, "rows": c1h}
    analyzer._kline_cache[("BTCUSDT", "4h")] = {"at": NOW - 1, "rows": c4h}
    client = KlineClient()
    monkeypatch.setattr(ProxyManager, "build_client", lambda **_: client)
    result = await analyzer.analyze_position("BTCUSDT", "LONG", 100.0)
    assert client.calls == 8
    assert result["stale"] is True
    assert result["asOf"] == result["analyzedAt"] == c4h[-1]["ot"]


@pytest.mark.asyncio
async def test_successful_fetch_of_old_data_is_still_stale(monkeypatch):
    client = KlineClient(candles(age_seconds=50000))
    monkeypatch.setattr(ProxyManager, "build_client", lambda **_: client)
    result = await PAAnalyzer().analyze_position("BTCUSDT", "LONG", 100.0)
    assert result["stale"] is True
    assert result["asOf"] == result["analyzedAt"] == int((NOW - 50000) * 1000)
