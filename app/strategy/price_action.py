"""Candlestick / price-action flags. Every flag on row i uses only rows <= i."""
from __future__ import annotations

import numpy as np
import pandas as pd


def add_price_action(df: pd.DataFrame, breakout_lookback: int = 20) -> pd.DataFrame:
    out = df.copy()
    o, h, l, c = out["open"], out["high"], out["low"], out["close"]
    rng = (h - l).replace(0, np.nan)
    body = (c - o).abs()
    upper = h - np.maximum(o, c)
    lower = np.minimum(o, c) - l
    avg_body = body.rolling(14).mean().shift(1)
    bull, bear = c > o, c < o
    po, pc = o.shift(1), c.shift(1)
    pbody = body.shift(1)

    out["bull_engulf"] = (pc < po) & bull & (c >= po) & (o <= pc) & (body > pbody)
    out["bear_engulf"] = (pc > po) & bear & (c <= po) & (o >= pc) & (body > pbody)
    out["bull_pin"] = (lower >= 2 * body) & (lower >= 0.6 * rng) & ((c - l) / rng >= 0.6)
    out["bear_pin"] = (upper >= 2 * body) & (upper >= 0.6 * rng) & ((h - c) / rng >= 0.6)
    out["strong_bull"] = bull & (body >= 0.6 * rng) & (body >= 1.2 * avg_body)
    out["strong_bear"] = bear & (body >= 0.6 * rng) & (body >= 1.2 * avg_body)
    out["breakout_up"] = bull & (c > h.rolling(breakout_lookback).max().shift(1))
    out["breakout_down"] = bear & (c < l.rolling(breakout_lookback).min().shift(1))
    out["reject_bull"] = (lower >= 0.5 * rng) & ((c - l) / rng >= 0.5)   # lower prices rejected
    out["reject_bear"] = (upper >= 0.5 * rng) & ((h - c) / rng >= 0.5)   # higher prices rejected
    flags = [k for k in out.columns if k in PATTERN_COLS]
    out[flags] = out[flags].fillna(False).astype(bool)
    return out


PATTERN_COLS = ["bull_engulf", "bear_engulf", "bull_pin", "bear_pin", "strong_bull", "strong_bear",
                "breakout_up", "breakout_down", "reject_bull", "reject_bear"]


def patterns_on(row: pd.Series) -> list[str]:
    return [p for p in PATTERN_COLS if bool(row.get(p, False))]
