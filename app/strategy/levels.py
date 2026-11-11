"""Swing points, support/resistance zones and Fibonacci retracements.
Swings are *confirmed*: a pivot at bar j is only reported once `strength` bars after it have closed."""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

FIB_RATIOS = (0.236, 0.382, 0.5, 0.618, 0.786)


@dataclass
class Swing:
    idx: int
    time: int
    price: float
    kind: str  # "high" | "low"


@dataclass
class Zone:
    low: float
    high: float
    price: float
    touches: int
    kind: str  # "support" | "resistance" (relative to the reference price)


def find_swings(df: pd.DataFrame, strength: int = 3) -> list[Swing]:
    h, l = df["high"].to_numpy(), df["low"].to_numpy()
    t = df["time"].to_numpy() if "time" in df.columns else np.arange(len(df))
    out: list[Swing] = []
    for j in range(strength, len(df) - strength):  # last `strength` bars are unconfirmed
        wh, wl = h[j - strength: j + strength + 1], l[j - strength: j + strength + 1]
        if h[j] == wh.max() and (wh == h[j]).sum() == 1:
            out.append(Swing(j, int(t[j]), float(h[j]), "high"))
        if l[j] == wl.min() and (wl == l[j]).sum() == 1:
            out.append(Swing(j, int(t[j]), float(l[j]), "low"))
    return out


def build_zones(swings: list[Swing], ref_price: float, tol: float) -> list[Zone]:
    if not swings or tol <= 0:
        return []
    pts = sorted(s.price for s in swings)
    clusters, cur = [], [pts[0]]
    for p in pts[1:]:
        if p - np.mean(cur) <= tol:
            cur.append(p)
        else:
            clusters.append(cur)
            cur = [p]
    clusters.append(cur)
    zones = []
    for c in clusters:
        mid = float(np.mean(c))
        zones.append(Zone(min(c) - tol * 0.25, max(c) + tol * 0.25, mid, len(c),
                          "support" if mid < ref_price else "resistance"))
    return zones


def fib_levels(swings: list[Swing]) -> dict | None:
    """Fibonacci retracement of the most recent confirmed swing leg."""
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]
    if not highs or not lows:
        return None
    hi, lo = highs[-1], lows[-1]
    if hi.price <= lo.price:
        return None
    up_leg = lo.idx < hi.idx  # low came first, then high -> pullbacks retrace down from the high
    span = hi.price - lo.price
    levels = {str(r): (hi.price - span * r) if up_leg else (lo.price + span * r) for r in FIB_RATIOS}
    return {"leg": "up" if up_leg else "down", "high": hi.price, "low": lo.price, "levels": levels}


def analyse_levels(df: pd.DataFrame, atr_value: float, strength: int, lookback: int) -> dict:
    window = df.iloc[-lookback:].reset_index(drop=True)
    swings = find_swings(window, strength)
    ref = float(window["close"].iloc[-1])
    zones = build_zones(swings, ref, 0.5 * atr_value)
    return {"swings": swings, "zones": zones, "fib": fib_levels(swings)}


def levels_summary(lv: dict, ref: float, n: int = 3) -> dict:
    sup = sorted([z for z in lv["zones"] if z.price < ref], key=lambda z: -z.price)[:n]
    res = sorted([z for z in lv["zones"] if z.price >= ref], key=lambda z: z.price)[:n]
    return {"support": [asdict(z) for z in sup], "resistance": [asdict(z) for z in res], "fibonacci": lv["fib"]}
