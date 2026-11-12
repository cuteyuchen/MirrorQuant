from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

from app.services.proxy.manager import ProxyManager

FAPI_PRIMARY = "https://www.binance.com"
FAPI_FALLBACK = "https://fapi.binance.com"
KLINE_TTL = {"1h": 60.0, "4h": 180.0}


def smart_px(v: Optional[float]) -> str:
    if v is None:
        return "--"
    a = abs(v)
    if a >= 1000:
        return f"{v:.1f}"
    if a >= 100:
        return f"{v:.2f}"
    if a >= 1:
        return f"{v:.3f}"
    if a >= 0.01:
        return f"{v:.4f}"
    return f"{v:.6f}"


def round_px(v: Optional[float]) -> Optional[float]:
    if v is None:
        return None
    a = abs(v)
    if a >= 1000:
        return round(v, 2)
    if a >= 100:
        return round(v, 3)
    if a >= 1:
        return round(v, 4)
    return round(v, 6)


def calc_ema_series(values: Sequence[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    out = [float(values[0])]
    for x in values[1:]:
        out.append(float(x) * k + out[-1] * (1.0 - k))
    return out


def calc_atr(candles: Sequence[Dict[str, float]], period: int = 14) -> Optional[float]:
    if len(candles) < period + 2:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def calc_rsi(closes: Sequence[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 2:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))


def find_unmitigated_fvgs(
    candles: Sequence[Dict[str, float]], atr_val: Optional[float]
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    if not candles or not atr_val or len(candles) < 10:
        return None, None
    closed = candles[:-1]
    start = max(2, len(closed) - 72)
    min_gap = 0.25 * atr_val
    bull_fvgs = []
    bear_fvgs = []
    for i in range(start, len(closed)):
        c0, c2 = closed[i - 2], closed[i]
        if c2["l"] - c0["h"] >= min_gap:
            low, high = c0["h"], c2["l"]
            if not any(closed[j]["l"] <= low for j in range(i + 1, len(closed))):
                bull_fvgs.append({"low": low, "high": high, "mid": (low + high) / 2.0, "idx": i})
        if c0["l"] - c2["h"] >= min_gap:
            low, high = c2["h"], c0["l"]
            if not any(closed[j]["h"] >= high for j in range(i + 1, len(closed))):
                bear_fvgs.append({"low": low, "high": high, "mid": (low + high) / 2.0, "idx": i})
    cur = candles[-1]["c"]
    bull_below = [f for f in bull_fvgs if f["mid"] <= cur * 1.005]
    bear_above = [f for f in bear_fvgs if f["mid"] >= cur * 0.995]
    nb = bull_below[-1] if bull_below else (bull_fvgs[-1] if bull_fvgs else None)
    na = bear_above[-1] if bear_above else (bear_fvgs[-1] if bear_fvgs else None)
    return nb, na


def _trend_label(px: float, ema21: float, ema55: float) -> Tuple[str, str, str]:
    if px > ema21 and ema21 > ema55:
        return "BULL", "Bullish Stack", "多头排列"
    if px < ema21 and ema21 < ema55:
        return "BEAR", "Bearish Stack", "空头排列"
    if px > ema21 and px > ema55:
        return "BULL_BIAS", "Bullish Bias", "偏多结构"
    if px < ema21 and px < ema55:
        return "BEAR_BIAS", "Bearish Bias", "偏空结构"
    return "RANGE", "Range / Choppy", "震荡整理"


def analyze_scaling(
    entry_orders: Optional[Sequence[Dict[str, Any]]],
    position_side: str,
    avg_entry: float,
) -> Tuple[str, Optional[str], str, Optional[str]]:
    if not entry_orders or len(entry_orders) <= 1:
        return "Single Entry", None, "单笔建仓", None
    ordered = sorted(entry_orders, key=lambda x: x.get("t", 0))
    prices = [float(x["p"]) for x in ordered if x.get("p")]
    if len(prices) <= 1:
        return "Single Entry", None, "单笔建仓", None
    p_first, p_last = prices[0], prices[-1]
    p_min, p_max = min(prices), max(prices)
    span_ms = (ordered[-1].get("t", 0) or 0) - (ordered[0].get("t", 0) or 0)
    spread_pct = (p_max - p_min) / p_min * 100.0 if p_min > 0 else 0.0

    if spread_pct < 0.25 and span_ms <= 30 * 60 * 1000:
        return (
            "TWAP / Split Cluster",
            f"{len(prices)} fills between {smart_px(p_min)}~{smart_px(p_max)} ({spread_pct:.2f}%)",
            "TWAP / 密集拆单",
            f"{len(prices)} 笔集中成交于 {smart_px(p_min)}~{smart_px(p_max)}（价差 {spread_pct:.2f}%）",
        )
    if position_side == "SHORT":
        if p_last > p_first * 1.008:
            return (
                "Scale-In Higher (Averaging Up Short)",
                f"{smart_px(p_first)} -> {smart_px(p_last)}, avg {smart_px(avg_entry)}",
                "逢高分批加空（拉高均价）",
                f"{smart_px(p_first)} → {smart_px(p_last)}，均价 {smart_px(avg_entry)}",
            )
        if p_last < p_first * 0.992:
            return (
                "Momentum Pyramid Short",
                f"{smart_px(p_first)} -> {smart_px(p_last)}, avg {smart_px(avg_entry)}",
                "顺势滚仓追空",
                f"{smart_px(p_first)} → {smart_px(p_last)}，均价 {smart_px(avg_entry)}",
            )
    else:
        if p_last < p_first * 0.992:
            return (
                "Scale-In Lower (Averaging Down Long)",
                f"{smart_px(p_first)} -> {smart_px(p_last)}, avg {smart_px(avg_entry)}",
                "逢低分批加多（摊低均价）",
                f"{smart_px(p_first)} → {smart_px(p_last)}，均价 {smart_px(avg_entry)}",
            )
        if p_last > p_first * 1.008:
            return (
                "Momentum Pyramid Long",
                f"{smart_px(p_first)} -> {smart_px(p_last)}, avg {smart_px(avg_entry)}",
                "顺势滚仓追多",
                f"{smart_px(p_first)} → {smart_px(p_last)}，均价 {smart_px(avg_entry)}",
            )
    return (
        "Range Scaling",
        f"{len(prices)} orders across {smart_px(p_min)}~{smart_px(p_max)}",
        "区间分批建仓",
        f"{len(prices)} 笔分散于 {smart_px(p_min)}~{smart_px(p_max)}（均价 {smart_px(avg_entry)}）",
    )


class PAAnalyzer:
    """Async Price-Action & SMC quantitative structure analyzer."""

    def __init__(self):
        self._kline_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}

    async def fetch_klines(
        self,
        symbol: str,
        interval: str = "1h",
        limit: int = 120,
        proxy_url: Optional[str] = None,
    ) -> List[Dict[str, float]]:
        key = (symbol, interval)
        ttl = KLINE_TTL.get(interval, 60.0)
        now = time.time()
        cached = self._kline_cache.get(key)
        if cached and (now - cached["at"]) < ttl and len(cached["rows"]) >= 30:
            return cached["rows"]

        path = f"/fapi/v1/klines?symbol={quote(symbol)}&interval={interval}&limit={limit}"
        urls = [f"{FAPI_PRIMARY}{path}", f"{FAPI_FALLBACK}{path}"]
        async with ProxyManager.build_client(proxy_url=proxy_url, timeout=12.0) as client:
            for attempt in range(4):
                target = urls[attempt % len(urls)] if attempt >= 2 else urls[0]
                try:
                    resp = await client.get(target)
                    if resp.status_code != 200:
                        continue
                    raw = resp.json()
                    rows = [
                        {
                            "ot": int(k[0]),
                            "o": float(k[1]),
                            "h": float(k[2]),
                            "l": float(k[3]),
                            "c": float(k[4]),
                            "v": float(k[5]),
                            "ct": int(k[6]),
                        }
                        for k in raw
                    ]
                    if rows:
                        self._kline_cache[key] = {"at": now, "rows": rows}
                        return rows
                except Exception:
                    await asyncio.sleep(0.25 * (attempt + 1))

        return cached["rows"] if cached else []

    def analyze_from_candles(
        self,
        symbol: str,
        position_side: str,
        avg_entry: float,
        c1h: List[Dict[str, float]],
        c4h: List[Dict[str, float]],
        current_price: Optional[float] = None,
        entry_orders: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        ps = str(position_side or "").upper()
        if ps not in ("LONG", "SHORT") or not avg_entry or avg_entry <= 0:
            return {"ok": False, "reason": "invalid_input"}
        if len(c1h) < 60 or len(c4h) < 56:
            return {"ok": False, "reason": "kline_unavailable"}

        px = float(current_price) if current_price else c1h[-1]["c"]
        closes_1h_live = [c["c"] for c in c1h[:-1]] + [px]
        closes_4h_live = [c["c"] for c in c4h[:-1]] + [px]

        ema21_1h = calc_ema_series(closes_1h_live, 21)[-1]
        ema55_1h = calc_ema_series(closes_1h_live, 55)[-1]
        ema21_4h = calc_ema_series(closes_4h_live, 21)[-1]
        ema55_4h = calc_ema_series(closes_4h_live, 55)[-1]

        atr_1h = calc_atr(c1h[:-1], 14) or (px * 0.01)
        rsi_1h = calc_rsi(closes_1h_live, 14)
        rsi_4h = calc_rsi(closes_4h_live, 14)

        win24 = c1h[-24:]
        win72 = c1h[-72:]
        high24 = max(max(c["h"] for c in win24), px)
        low24 = min(min(c["l"] for c in win24), px)
        high72 = max(max(c["h"] for c in win72), px)
        low72 = min(min(c["l"] for c in win72), px)
        eq72 = (high72 + low72) / 2.0

        bull_fvg, bear_fvg = find_unmitigated_fvgs(c1h, atr_1h)
        t1_code, t1_label, t1_zh = _trend_label(px, ema21_1h, ema55_1h)
        t4_code, t4_label, t4_zh = _trend_label(px, ema21_4h, ema55_4h)

        span72 = max(high72 - low72, 1e-9)
        entry_pct = max(0.0, min(100.0, (avg_entry - low72) / span72 * 100.0))
        cur_pct = max(0.0, min(100.0, (px - low72) / span72 * 100.0))
        raw_diff_pct = (px - avg_entry) / avg_entry * 100.0
        edge_pct = -raw_diff_pct if ps == "SHORT" else raw_diff_pct
        entry_vs_ema21_atr = (avg_entry - ema21_1h) / atr_1h

        aligned_1h = (t1_code in ("BEAR", "BEAR_BIAS")) if ps == "SHORT" else (t1_code in ("BULL", "BULL_BIAS"))
        aligned_4h = (t4_code in ("BEAR", "BEAR_BIAS")) if ps == "SHORT" else (t4_code in ("BULL", "BULL_BIAS"))

        if aligned_1h and aligned_4h:
            trend_align, trend_align_zh, trend_score = "1H+4H Aligned", "双周期强共振", 2
        elif aligned_1h:
            trend_align, trend_align_zh, trend_score = "1H Aligned / 4H Counter", "1H 顺势 / 4H 逆势", 1
        elif aligned_4h:
            trend_align, trend_align_zh, trend_score = "4H Aligned / 1H Pullback", "4H 顺势 / 1H 回调", 0
        else:
            trend_align, trend_align_zh, trend_score = "Counter-Trend Both TF", "双周期逆势", -2

        if ps == "SHORT":
            if entry_pct >= 68:
                entry_zone, entry_zone_zh, loc_score = "Premium Zone Short", "溢价区做空", 2
            elif entry_pct >= 42:
                entry_zone, entry_zone_zh, loc_score = "Equilibrium Short", "均衡区做空", 1
            else:
                entry_zone, entry_zone_zh, loc_score = "Discount Chase Short", "折价区追空", -1

            res_cands = [high24, ema21_1h, ema55_1h] + ([bear_fvg["high"]] if bear_fvg else [])
            above_res = [x for x in res_cands if x > px * 0.999]
            key_res = min(above_res) if above_res else max(high24, ema21_1h)
            invalidation = max(high24, avg_entry + 1.2 * atr_1h) + 0.35 * atr_1h
            if high24 > px:
                invalidation = min(invalidation, max(high24, avg_entry) + 0.5 * atr_1h)
            sup_cands = [low24, low72] + ([bull_fvg["high"]] if bull_fvg else [])
            below_sup = [x for x in sup_cands if x < px * 1.001]
            key_sup = max(below_sup) if below_sup else low72
            risk_dist = max(invalidation - avg_entry, 0.8 * atr_1h)
            target1 = min(low24, avg_entry - 1.5 * risk_dist)
            if target1 >= px * 0.995:
                target1 = px - 1.5 * atr_1h
            target2 = min(low72, target1 - 1.2 * atr_1h)
            rr_entry = (avg_entry - target1) / max(invalidation - avg_entry, 1e-9)
            rr_cur = (px - target1) / max(invalidation - px, 1e-9) if invalidation > px else 0.0
        else:
            if entry_pct <= 32:
                entry_zone, entry_zone_zh, loc_score = "Discount Zone Long", "折价区做多", 2
            elif entry_pct <= 58:
                entry_zone, entry_zone_zh, loc_score = "Equilibrium Long", "均衡区做多", 1
            else:
                entry_zone, entry_zone_zh, loc_score = "Premium Chase Long", "溢价区追多", -1

            sup_cands = [low24, ema21_1h, ema55_1h] + ([bull_fvg["low"]] if bull_fvg else [])
            below_sup = [x for x in sup_cands if x < px * 1.001]
            key_sup = max(below_sup) if below_sup else min(low24, ema21_1h)
            invalidation = min(low24, avg_entry - 1.2 * atr_1h) - 0.35 * atr_1h
            if low24 < px:
                invalidation = max(invalidation, min(low24, avg_entry) - 0.5 * atr_1h)
            res_cands = [high24, high72] + ([bear_fvg["low"]] if bear_fvg else [])
            above_res = [x for x in res_cands if x > px * 0.999]
            key_res = min(above_res) if above_res else high72
            risk_dist = max(avg_entry - invalidation, 0.8 * atr_1h)
            target1 = max(high24, avg_entry + 1.5 * risk_dist)
            if target1 <= px * 1.005:
                target1 = px + 1.5 * atr_1h
            target2 = max(high72, target1 + 1.2 * atr_1h)
            rr_entry = (target1 - avg_entry) / max(avg_entry - invalidation, 1e-9)
            rr_cur = (target1 - px) / max(px - invalidation, 1e-9) if px > invalidation else 0.0

        total_score = trend_score + loc_score + (1 if edge_pct >= 0.8 else (-1 if edge_pct <= -1.5 else 0))
        if total_score >= 3:
            verdict, verdict_zh, v_level = "Strong Alignment", "顺势占优", "good"
        elif total_score >= 1:
            verdict, verdict_zh, v_level = "Favorable Structure", "结构良好", "mid"
        elif total_score >= -1:
            verdict, verdict_zh, v_level = "Neutral / Tactical", "中性博弈", "warn"
        else:
            verdict, verdict_zh, v_level = "Counter-Trend Risk", "逆势高险", "bad"

        scaling_style, scaling_desc, scaling_style_zh, scaling_desc_zh = analyze_scaling(entry_orders, ps, avg_entry)

        summary = (
            f"Entry {smart_px(avg_entry)} at {entry_pct:.0f}% of 72H range ({entry_zone}, "
            f"{entry_vs_ema21_atr:+.1f} ATR vs 1H EMA21); {trend_align} "
            f"(1H {t1_label} / 4H {t4_label}, RSI {rsi_1h or 50:.0f}/{rsi_4h or 50:.0f}). "
            f"Invalidation {smart_px(invalidation)}, T1 {smart_px(target1)}, T2 {smart_px(target2)}."
        )
        edge_txt_zh = f"当前浮盈 {edge_pct:.2f}%" if edge_pct >= 0 else f"当前浮亏 {abs(edge_pct):.2f}%"
        fvg_zh_parts = []
        if bear_fvg:
            fvg_zh_parts.append(f"上方 1H 空头 FVG 阻力带 {smart_px(bear_fvg['low'])}~{smart_px(bear_fvg['high'])}")
        if bull_fvg:
            fvg_zh_parts.append(f"下方 1H 多头 FVG 支撑带 {smart_px(bull_fvg['low'])}~{smart_px(bull_fvg['high'])}")
        fvg_zh = ("；" + "，".join(fvg_zh_parts)) if fvg_zh_parts else ""
        scale_note_zh = f"；{scaling_desc_zh}" if scaling_desc_zh else ""
        summary_zh = (
            f"均价 {smart_px(avg_entry)} 处于近 72H 区间 {entry_pct:.0f}% 分位（{entry_zone_zh}，"
            f"相对 1H EMA21 偏离 {entry_vs_ema21_atr:+.1f} ATR）{scale_note_zh}；{edge_txt_zh}。"
            f"{trend_align_zh}（1H {t1_zh} / 4H {t4_zh}，RSI {rsi_1h or 50:.0f}/{rsi_4h or 50:.0f}）{fvg_zh}；"
            f"客观防守失效参考 {smart_px(invalidation)}，第一目标 {smart_px(target1)} / 次级 {smart_px(target2)}（仅作 K 线结构参考，不构成投资建议）。"
        )

        return {
            "ok": True,
            "symbol": symbol,
            "positionSide": ps,
            "verdict": verdict,
            "verdictZh": verdict_zh,
            "verdictLevel": v_level,
            "trend1h": t1_label,
            "trend1hZh": t1_zh,
            "trend4h": t4_label,
            "trend4hZh": t4_zh,
            "trendAlignment": trend_align,
            "trendAlignmentZh": trend_align_zh,
            "entryZone": entry_zone,
            "entryZoneZh": entry_zone_zh,
            "entryRangePct": round(entry_pct, 1),
            "curRangePct": round(cur_pct, 1),
            "entryVsEma21Atr": round(entry_vs_ema21_atr, 2),
            "priceVsEntryPct": round(raw_diff_pct, 2),
            "edgePct": round(edge_pct, 2),
            "ema21_1h": round_px(ema21_1h),
            "ema55_1h": round_px(ema55_1h),
            "ema21_4h": round_px(ema21_4h),
            "ema55_4h": round_px(ema55_4h),
            "rsi1h": round(rsi_1h, 1) if rsi_1h is not None else None,
            "rsi4h": round(rsi_4h, 1) if rsi_4h is not None else None,
            "atr1h": round_px(atr_1h),
            "atr1hPct": round(atr_1h / px * 100.0, 2) if px else 0.0,
            "high24h": round_px(high24),
            "low24h": round_px(low24),
            "high72h": round_px(high72),
            "low72h": round_px(low72),
            "eq72h": round_px(eq72),
            "keyResistance": round_px(key_res),
            "keySupport": round_px(key_sup),
            "invalidationLevel": round_px(invalidation),
            "target1": round_px(target1),
            "target2": round_px(target2),
            "rrFromEntry": round(rr_entry, 2),
            "rrFromCurrent": round(rr_cur, 2),
            "nearestBearFvg": (
                [round_px(bear_fvg["low"]), round_px(bear_fvg["high"])] if bear_fvg else None
            ),
            "nearestBullFvg": (
                [round_px(bull_fvg["low"]), round_px(bull_fvg["high"])] if bull_fvg else None
            ),
            "scalingStyle": scaling_style,
            "scalingStyleZh": scaling_style_zh,
            "scalingDesc": scaling_desc,
            "scalingDescZh": scaling_desc_zh,
            "summary": summary,
            "summaryZh": summary_zh,
            "analyzedAt": int(time.time() * 1000),
        }

    async def analyze_position(
        self,
        symbol: str,
        position_side: str,
        avg_entry: Optional[float],
        current_price: Optional[float] = None,
        entry_orders: Optional[Sequence[Dict[str, Any]]] = None,
        proxy_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not avg_entry or avg_entry <= 0:
            return {"ok": False, "reason": "invalid_input"}
        c1h, c4h = await asyncio.gather(
            self.fetch_klines(symbol, "1h", 120, proxy_url=proxy_url),
            self.fetch_klines(symbol, "4h", 100, proxy_url=proxy_url),
        )
        return self.analyze_from_candles(
            symbol=symbol,
            position_side=position_side,
            avg_entry=avg_entry,
            c1h=c1h,
            c4h=c4h,
            current_price=current_price,
            entry_orders=entry_orders,
        )
