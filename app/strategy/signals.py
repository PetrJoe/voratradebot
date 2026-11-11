"""Deterministic setup detection and weighted confluence scoring."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from ..config import TradingParams
from .levels import analyse_levels, levels_summary
from .price_action import patterns_on

SNAP_COLS = ["close", "ema9", "ema21", "ema50", "ema200", "rsi", "macd", "macd_signal", "macd_hist",
             "adx", "plus_di", "minus_di", "atr", "bb_upper", "bb_mid", "bb_lower"]


@dataclass
class Candidate:
    direction: str
    score: float
    entry: float
    stop_loss: float
    take_profit: float
    rr: float
    atr: float
    bar_time: int
    components: dict
    patterns: list
    trend: str
    indicators: dict
    levels: dict
    blocks: list = field(default_factory=list)

    def evidence(self) -> str:
        parts = [f"{k}:{v['points']:.0f}/{v['max']:.0f} ({v['note']})" for k, v in self.components.items() if v["max"] > 0]
        return f"{self.direction} score {self.score:.0f} | " + "; ".join(parts)


@dataclass
class SetupResult:
    candidate: Candidate | None
    buy_score: float
    sell_score: float
    trend: str
    buy_blocks: list
    sell_blocks: list
    indicators: dict
    patterns: list
    levels: dict


def _f(x) -> float | None:
    try:
        return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)
    except (TypeError, ValueError):
        return None


def snapshot(row: pd.Series) -> dict:
    return {k: (round(v, 6) if (v := _f(row.get(k))) is not None else None) for k in SNAP_COLS}


def trend_of(row: pd.Series) -> str:
    e9, e21, e50 = row["ema9"], row["ema21"], row["ema50"]
    if e9 > e21 > e50:
        return "UPTREND"
    if e9 < e21 < e50:
        return "DOWNTREND"
    return "RANGE"


def propose_levels(direction: str, entry: float, atr: float, swings: list, p: TradingParams) -> tuple[float, float, float]:
    """Technical stop: beyond the latest confirmed swing if its distance is sane, else ATR-based."""
    s = 1 if direction == "BUY" else -1
    sl = entry - s * p.atr_sl_mult * atr
    cands = [w.price for w in swings if (w.kind == "low" if s == 1 else w.kind == "high") and (entry - w.price) * s > 0]
    if cands:
        sw = max(cands) if s == 1 else min(cands)
        sl_sw = sw - s * 0.2 * atr
        if p.sl_min_atr * atr <= abs(entry - sl_sw) <= p.sl_max_atr * atr:
            sl = sl_sw
    risk = abs(entry - sl)
    tp = entry + s * max(p.min_rr, 2.0) * risk
    return sl, tp, risk


def _score_direction(df: pd.DataFrame, lv: dict, p: TradingParams, direction: str) -> tuple[float, dict, list, Candidate | None]:
    last, prev5 = df.iloc[-1], df.iloc[-6]
    buy = direction == "BUY"
    s = 1 if buy else -1
    atr = float(last["atr"])
    entry = float(last["close"])
    blocks: list[str] = []
    comp: dict = {}

    def put(name, pts, mx, note):
        comp[name] = {"points": pts if mx > 0 else 0.0, "max": mx, "note": note}

    # EMA trend
    aligned = (last.ema9 > last.ema21 > last.ema50) if buy else (last.ema9 < last.ema21 < last.ema50)
    slope_ok = (last.ema21 - prev5.ema21) * s > 0
    partial = (last.ema9 - last.ema21) * s > 0 and (last.close - last.ema50) * s > 0
    if aligned and slope_ok:
        put("ema", p.w_ema, p.w_ema, "9/21/50 aligned, EMA21 sloping with trend")
    elif aligned:
        put("ema", p.w_ema * 0.7, p.w_ema, "aligned, EMA21 flat")
    elif partial:
        put("ema", p.w_ema * 0.3, p.w_ema, "only 9/21 + price vs 50")
    else:
        put("ema", 0, p.w_ema, "not aligned")
    if p.require_full_ema_alignment and not aligned:
        blocks.append("EMA 9/21/50 not aligned")

    # RSI
    r = float(last.rsi)
    lo, hi = (p.rsi_buy_min, p.rsi_buy_max) if buy else (p.rsi_sell_min, p.rsi_sell_max)
    if lo <= r <= hi:
        put("rsi", p.w_rsi, p.w_rsi, f"RSI {r:.0f} in {lo:.0f}-{hi:.0f}")
    else:
        put("rsi", 0, p.w_rsi, f"RSI {r:.0f} outside {lo:.0f}-{hi:.0f}")

    # ADX
    di_ok = (last.plus_di > last.minus_di) if buy else (last.minus_di > last.plus_di)
    if last.adx >= p.adx_min and di_ok:
        put("adx", p.w_adx, p.w_adx, f"ADX {last.adx:.0f}")
    elif last.adx >= 0.8 * p.adx_min and di_ok:
        put("adx", p.w_adx * 0.5, p.w_adx, f"ADX {last.adx:.0f} borderline")
    else:
        put("adx", 0, p.w_adx, f"ADX {last.adx:.0f} weak/against")
    if p.require_adx and not (last.adx >= p.adx_min and di_ok):
        blocks.append(f"ADX {last.adx:.0f} below {p.adx_min:.0f} or DI against")

    # Candles
    pats = patterns_on(last)
    key = ("bull_engulf", "bull_pin") if buy else ("bear_engulf", "bear_pin")
    mid = ("strong_bull", "breakout_up") if buy else ("strong_bear", "breakout_down")
    rej = "reject_bull" if buy else "reject_bear"
    if any(k in pats for k in key):
        put("candle", p.w_candle, p.w_candle, ", ".join(k for k in key if k in pats))
    elif any(k in pats for k in mid):
        put("candle", p.w_candle * 0.6, p.w_candle, ", ".join(k for k in mid if k in pats))
    elif rej in pats:
        put("candle", p.w_candle * 0.5, p.w_candle, rej)
    else:
        put("candle", 0, p.w_candle, "no confirming candle")

    # Stops / targets
    sl, tp, risk = propose_levels(direction, entry, atr, lv["swings"], p)
    rr = abs(tp - entry) / risk if risk > 0 else 0.0

    # ATR suitability
    atr_pct = atr / entry * 100
    sl_atr = risk / atr
    if p.atr_pct_min <= atr_pct <= p.atr_pct_max and p.sl_min_atr <= sl_atr <= p.sl_max_atr:
        put("atr", p.w_atr, p.w_atr, f"ATR {atr_pct:.3f}% of price, stop {sl_atr:.1f} ATR")
    else:
        put("atr", 0, p.w_atr, f"ATR {atr_pct:.3f}% / stop {sl_atr:.1f} ATR out of range")
        blocks.append("ATR or stop distance outside allowed range")

    # Support / resistance in the way of the trade
    opp = [z for z in lv["zones"] if (z.low > entry if buy else z.high < entry)]
    near = min(opp, key=lambda z: abs((z.low if buy else z.high) - entry)) if opp else None
    if near is None:
        put("sr", p.w_sr, p.w_sr, "no opposing zone in lookback")
    else:
        dist = abs((near.low if buy else near.high) - entry)
        if dist < 1.0 * risk:
            put("sr", 0, p.w_sr, f"opposing zone {dist / risk:.1f}R away")
            blocks.append("opposing S/R zone closer than 1R")
        elif dist < abs(tp - entry):
            put("sr", p.w_sr * 0.4, p.w_sr, f"zone {dist / risk:.1f}R away, before target")
        else:
            put("sr", p.w_sr, p.w_sr, f"nearest opposing zone {dist / risk:.1f}R away")

    # MACD (momentum must be strengthening, not merely positive)
    h, hp = float(last.macd_hist), float(df.iloc[-2].macd_hist)
    if h * s > 0 and (h - hp) * s > 0:
        put("macd", p.w_macd, p.w_macd, "histogram with trend and expanding")
    elif h * s > 0:
        put("macd", p.w_macd * 0.5, p.w_macd, "histogram with trend, fading")
    else:
        put("macd", 0, p.w_macd, "histogram against")

    # Fibonacci confluence (supporting evidence only)
    fib = lv["fib"]
    fib_pts, note = 0.0, "no fib leg"
    if fib and fib["leg"] == ("up" if buy else "down"):
        near_lv = [(k, v) for k, v in fib["levels"].items() if k in ("0.382", "0.5", "0.618") and abs(entry - v) <= 0.5 * atr]
        if near_lv:
            fib_pts, note = p.w_fib, f"price at fib {near_lv[0][0]}"
        else:
            note = "not at a key fib level"
    put("fib", fib_pts, p.w_fib, note)

    # Correlated-indicator cap, then rescale to 100
    corr_keys = ("ema", "rsi", "adx", "macd")
    corr_pts = sum(comp[k]["points"] for k in corr_keys)
    corr_max = sum(comp[k]["max"] for k in corr_keys)
    excess = max(0.0, corr_pts - p.correlated_cap)
    max_possible = sum(c["max"] for c in comp.values()) - max(0.0, corr_max - p.correlated_cap)
    raw = sum(c["points"] for c in comp.values()) - excess
    score = round(100 * raw / max_possible, 1) if max_possible > 0 else 0.0

    cand = Candidate(direction, score, entry, sl, tp, rr, atr, int(last["time"]), comp, pats,
                     trend_of(last), snapshot(last), levels_summary(lv, entry), blocks)
    return score, comp, blocks, cand


def evaluate(df: pd.DataFrame, p: TradingParams) -> SetupResult | None:
    """df: closed candles with indicator + price-action columns. The last row is the decision bar."""
    need = max(60, p.swing_strength * 2 + 10)
    if len(df) < need:
        return None
    last = df.iloc[-1]
    if any(pd.isna(last[c]) for c in ("ema9", "ema21", "ema50", "rsi", "adx", "atr", "macd_hist")) or last["atr"] <= 0:
        return None
    lv = analyse_levels(df, float(last["atr"]), p.swing_strength, p.sr_lookback)
    res = {d: _score_direction(df, lv, p, d) for d in ("BUY", "SELL")}
    eligible = [(res[d][0], d) for d in res if not res[d][2] and res[d][0] >= p.min_score]
    cand = None
    if eligible:
        _, d = max(eligible)
        cand = res[d][3]
    return SetupResult(cand, res["BUY"][0], res["SELL"][0], trend_of(last), res["BUY"][2], res["SELL"][2],
                       snapshot(last), patterns_on(last), levels_summary(lv, float(last["close"])))
