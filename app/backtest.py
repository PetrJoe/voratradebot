"""Bar-by-bar backtester using the SAME strategy code as live trading.

No look-ahead: a decision uses candles up to and including bar i (closed); the order fills at the OPEN of bar i+1.
Spread is applied from the per-bar `spread` column (points). If SL and TP are both reachable in one bar, SL is assumed first.
Groq is NOT simulated: AI-assisted backtests would need stored historical AI analyses or many paid API calls."""
from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from .config import TIMEFRAME_SECONDS, TradingParams
from .risk import calc_volume
from .strategy.indicators import add_indicators
from .strategy.price_action import add_price_action
from .strategy.signals import evaluate


def run_backtest(df: pd.DataFrame, p: TradingParams, spec: dict, *, timeframe: str = "M5", initial_balance: float = 10_000.0,
                 commission_per_lot: float = 0.0, slippage_points: float = 0.0, window: int = 300) -> dict:
    """spec: point, tick_size, tick_value, volume_min, volume_max, volume_step."""
    df = df.reset_index(drop=True)
    n = len(df)
    warm = 250
    if n < warm + 10:
        raise ValueError(f"need at least {warm + 10} candles, got {n}")
    d = add_price_action(add_indicators(df))
    o, h, l, t = d["open"].to_numpy(), d["high"].to_numpy(), d["low"].to_numpy(), d["time"].to_numpy()
    spr = d["spread"].to_numpy() * spec["point"] if "spread" in d else np.zeros(n)
    tfs = TIMEFRAME_SECONDS[timeframe]

    equity = initial_balance
    trades, curve = [], [(int(t[warm]), equity)]
    pos, pending = None, None
    day, day_start_eq, locked_day, day_trades, last_entry_t = None, equity, None, 0, -10**12

    for i in range(warm, n):
        dt = datetime.fromtimestamp(int(t[i]), timezone.utc).date()
        if dt != day:
            day, day_start_eq, day_trades = dt, equity, 0
        # 1) fill pending order at this bar's open
        if pending is not None and pos is None:
            c, s = pending, 1 if pending["direction"] == "BUY" else -1
            pending = None
            sp = spr[i]
            entry = o[i] + (sp if s == 1 else 0.0)
            risk_dist = (entry - c["sl"]) * s
            atr = c["atr"]
            ok = (risk_dist > 0 and abs(entry - c["entry"]) <= p.entry_drift_atr * atr and sp <= p.max_spread_atr_fraction * atr)
            if ok:
                tp = entry + s * max(p.min_rr, 2.0) * risk_dist
                lpl = risk_dist / spec["tick_size"] * spec["tick_value"]
                vol, _ = calc_volume(equity * p.risk_per_trade_pct / 100, lpl, spec["volume_min"], spec["volume_max"], spec["volume_step"])
                if vol:
                    pos = dict(direction=c["direction"], s=s, entry=entry, sl=c["sl"], tp=tp, vol=vol, i0=i, t0=int(t[i]),
                               risk_money=vol * lpl, score=c["score"])
                    day_trades += 1
                    last_entry_t = int(t[i])
        # 2) manage open position within bar i
        if pos is not None:
            s, sp, exit_px = pos["s"], spr[i], None
            slip = slippage_points * spec["point"]
            if s == 1:
                if o[i] <= pos["sl"]:
                    exit_px = o[i] - slip
                elif l[i] <= pos["sl"]:
                    exit_px = pos["sl"] - slip
                elif h[i] >= pos["tp"]:
                    exit_px = pos["tp"]
            else:
                if o[i] + sp >= pos["sl"]:
                    exit_px = o[i] + sp + slip
                elif h[i] + sp >= pos["sl"]:
                    exit_px = pos["sl"] + slip
                elif l[i] + sp <= pos["tp"]:
                    exit_px = pos["tp"]
            if exit_px is not None:
                pnl = (exit_px - pos["entry"]) * s / spec["tick_size"] * spec["tick_value"] * pos["vol"] - commission_per_lot * pos["vol"] * 2
                equity += pnl
                trades.append({"direction": pos["direction"], "entry": pos["entry"], "exit": exit_px, "sl": pos["sl"], "tp": pos["tp"],
                               "volume": pos["vol"], "profit": pnl, "r": pnl / pos["risk_money"] if pos["risk_money"] else 0.0,
                               "bars": i - pos["i0"] + 1, "duration_s": (i - pos["i0"] + 1) * tfs, "opened": pos["t0"], "closed": int(t[i]),
                               "score": pos["score"]})
                curve.append((int(t[i]), equity))
                pos = None
                if equity - day_start_eq <= -day_start_eq * p.max_daily_loss_pct / 100:
                    locked_day = day
        # 3) decide on the close of bar i (fills next bar)
        if pos is None and pending is None and i < n - 1 and locked_day != day and day_trades < p.max_trades_per_day \
                and int(t[i]) - last_entry_t >= p.cooldown_minutes_after_trade * 60:
            row = d.iloc[i]
            if p.require_full_ema_alignment and not (row.ema9 > row.ema21 > row.ema50 or row.ema9 < row.ema21 < row.ema50):
                continue
            if p.require_adx and not row.adx >= p.adx_min:
                continue
            res = evaluate(d.iloc[max(0, i - window + 1): i + 1], p)
            if res and res.candidate:
                c = res.candidate
                pending = dict(direction=c.direction, sl=c.stop_loss, entry=c.entry, atr=c.atr, score=c.score)
    return _metrics(trades, curve, initial_balance)


def _metrics(trades: list[dict], curve: list, initial: float) -> dict:
    wins = [x for x in trades if x["profit"] > 0]
    losses = [x for x in trades if x["profit"] <= 0]
    gp, gl = sum(x["profit"] for x in wins), -sum(x["profit"] for x in losses)
    peak, mdd, mdd_pct = initial, 0.0, 0.0
    for _, e in curve:
        peak = max(peak, e)
        mdd = max(mdd, peak - e)
        mdd_pct = max(mdd_pct, (peak - e) / peak * 100 if peak else 0)

    def streak(flag):
        best = cur = 0
        for x in trades:
            cur = cur + 1 if (x["profit"] > 0) == flag else 0
            best = max(best, cur)
        return best

    return {
        "kind": "BACKTEST (simulated, not live/demo results)", "ai_used": False,
        "total_trades": len(trades), "winning_trades": len(wins), "losing_trades": len(losses),
        "win_rate": round(100 * len(wins) / len(trades), 2) if trades else 0.0,
        "net_profit": round(gp - gl, 2), "gross_profit": round(gp, 2), "gross_loss": round(-gl, 2),
        "profit_factor": round(gp / gl, 3) if gl > 0 else (None if gp == 0 else math.inf),
        "max_drawdown": round(mdd, 2), "max_drawdown_pct": round(mdd_pct, 2),
        "avg_reward_risk": round(float(np.mean([x["r"] for x in trades])), 3) if trades else 0.0,
        "avg_trade_duration_min": round(float(np.mean([x["duration_s"] for x in trades])) / 60, 1) if trades else 0.0,
        "max_consecutive_wins": streak(True), "max_consecutive_losses": streak(False),
        "initial_balance": initial, "final_balance": round(curve[-1][1], 2),
        "equity_curve": [{"time": a, "equity": round(b, 2)} for a, b in curve], "trades": trades,
    }
