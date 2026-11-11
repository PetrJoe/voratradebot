from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from app.ai.groq_client import AIResult, AIDecision, build_payload, parse_decision
from app.ai.validate import vet
from app.backtest import run_backtest
from app.config import TradingParams, risky_changes
from app.db import Database, Trade, utcnow
from app.risk import RiskManager, calc_volume, round_volume_down, trading_date
from app.strategy.indicators import add_indicators, rsi
from app.strategy.price_action import add_price_action
from app.strategy.signals import evaluate
from app.strategy.levels import find_swings
from conftest import make_prices


# ---------------- indicators ----------------
def test_indicator_ranges(ind):
    last = ind.iloc[-1]
    assert 0 <= last.rsi <= 100 and 0 <= last.adx <= 100 and last.atr > 0
    assert last.bb_upper > last.bb_mid > last.bb_lower


def test_indicators_are_causal(prices):
    full = add_indicators(prices)
    part = add_indicators(prices.iloc[:400])
    for col in ("ema50", "rsi", "adx", "atr", "macd_hist"):
        assert full[col].iloc[399] == pytest.approx(part[col].iloc[399])


def test_swings_are_confirmed_only(prices):
    sw = find_swings(prices, 3)
    assert all(s.idx <= len(prices) - 1 - 3 for s in sw)


def test_engulfing_detected():
    df = pd.DataFrame(dict(time=range(30), open=[10.0] * 30, high=[10.5] * 30, low=[9.5] * 30, close=[10.0] * 30))
    df.loc[28, ["open", "close", "high", "low"]] = [10.2, 9.8, 10.3, 9.7]      # bearish
    df.loc[29, ["open", "close", "high", "low"]] = [9.7, 10.4, 10.5, 9.6]      # bullish engulfs
    assert bool(add_price_action(df).iloc[-1].bull_engulf)


# ---------------- risk ----------------
def test_volume_rounding_never_rounds_up():
    assert round_volume_down(0.0379, 0.001, 100, 0.001) == pytest.approx(0.037)
    assert round_volume_down(0.0004, 0.001, 100, 0.001) is None
    assert round_volume_down(500, 0.01, 100, 0.01) == 100


def test_calc_volume_respects_risk():
    vol, _ = calc_volume(50, 120, 0.001, 100, 0.001)
    assert vol * 120 <= 50
    vol, why = calc_volume(5, 120, 0.5, 100, 0.5)
    assert vol is None and "minimum" in why
    assert calc_volume(50, 0, 0.01, 1, 0.01)[0] is None


def test_trading_day_boundary():
    t = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)
    assert trading_date(t, "UTC", 0) == "2026-01-01"
    assert trading_date(t, "UTC", 2) == "2025-12-31"


def test_baseline_persists_across_restart_and_locks(tmp_path):
    url = f"sqlite:///{tmp_path / 't.db'}"
    p = TradingParams()
    r1 = RiskManager(Database(url))
    assert r1.ensure_baseline(10_000, p)["starting_equity"] == 10_000
    r2 = RiskManager(Database(url))                       # "restart"
    assert r2.ensure_baseline(9_000, p)["starting_equity"] == 10_000   # not reset
    info = r2.update_lock(9_810, p)
    assert not info["locked"]
    info = r2.update_lock(9_799 - 100, p)                 # below -2%
    assert info["locked"]
    dec = r2.pre_trade_check(p, equity=9_000, open_positions=0, spread=0.1, atr=5)
    assert not dec.allowed and any("daily loss" in x for x in dec.reasons)


def test_pre_trade_blocks(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 't.db'}")
    rm, p = RiskManager(db), TradingParams()
    assert rm.pre_trade_check(p, equity=10_000, open_positions=0, spread=0.1, atr=5).allowed
    assert not rm.pre_trade_check(p, equity=10_000, open_positions=1, spread=0.1, atr=5).allowed
    assert not rm.pre_trade_check(p, equity=10_000, open_positions=0, spread=5, atr=5).allowed   # spread too wide
    assert not rm.pre_trade_check(p, equity=None, open_positions=0, spread=0.1, atr=5).allowed
    rm.emergency_stop = True
    assert not rm.pre_trade_check(p, equity=10_000, open_positions=0, spread=0.1, atr=5).allowed


def test_loss_streak_cooldown(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 't.db'}")
    rm, p = RiskManager(db), TradingParams(cooldown_minutes_after_trade=0)
    with db.session() as s:
        for i in range(3):
            s.add(Trade(mt5_ticket=i, symbol="X", direction="BUY", volume=1, entry_price=1, profit=-5, status="closed",
                        opened_at=utcnow(), closed_at=utcnow()))
    dec = rm.pre_trade_check(p, equity=10_000, open_positions=0, spread=0.1, atr=5)
    assert not dec.allowed and any("consecutive" in x for x in dec.reasons)


# ---------------- settings safety ----------------
def test_risk_bounds_cannot_be_removed():
    with pytest.raises(Exception):
        TradingParams(risk_per_trade_pct=50)
    with pytest.raises(Exception):
        TradingParams(max_daily_loss_pct=0)
    with pytest.raises(Exception):
        TradingParams(min_rr=0.5)
    assert not hasattr(TradingParams(), "mandatory_stop_loss")


def test_risky_changes_detected():
    a = TradingParams()
    assert risky_changes(a, TradingParams(risk_per_trade_pct=1.0)) == ["risk_per_trade_pct"]
    assert risky_changes(a, TradingParams(risk_per_trade_pct=0.25)) == []
    assert "min_rr" in risky_changes(a, TradingParams(min_rr=1.5))


# ---------------- strategy ----------------
def test_no_trade_when_nothing_aligns():
    df = add_price_action(add_indicators(make_prices(drift=0.0, vol=1.0, seed=7)))
    p = TradingParams(min_score=95)
    assert evaluate(df, p).candidate is None


def test_candidate_has_valid_levels(ind):
    p = TradingParams(min_score=50)
    found = [evaluate(ind.iloc[:i + 1], p) for i in range(150, len(ind), 3)]
    cands = [r.candidate for r in found if r and r.candidate]
    assert cands, "expected at least one candidate in a drifting series"
    for c in cands:
        s = 1 if c.direction == "BUY" else -1
        assert (c.entry - c.stop_loss) * s > 0 and (c.take_profit - c.entry) * s > 0
        assert c.rr >= p.min_rr - 1e-9 and 0 <= c.score <= 100


def test_score_weights_normalised_to_100(ind):
    p = TradingParams(min_score=40)
    for i in range(150, len(ind), 5):
        r = evaluate(ind.iloc[:i + 1], p)
        assert r is None or (0 <= r.buy_score <= 100 and 0 <= r.sell_score <= 100)


# ---------------- backtest ----------------
SPEC = dict(point=0.01, tick_size=0.01, tick_value=0.01, volume_min=0.001, volume_max=100, volume_step=0.001)


def test_backtest_runs_and_reports_metrics():
    res = run_backtest(make_prices(900), TradingParams(min_score=55), SPEC)
    assert res["total_trades"] == res["winning_trades"] + res["losing_trades"]
    assert "BACKTEST" in res["kind"] and res["ai_used"] is False
    assert res["max_drawdown"] >= 0 and len(res["equity_curve"]) == res["total_trades"] + 1


def test_backtest_has_no_lookahead():
    base = make_prices(900)
    cut = 700
    alt = base.copy()
    rng = np.random.default_rng(99)
    for c in ("open", "high", "low", "close"):
        alt.loc[cut:, c] = alt.loc[cut:, c] + rng.normal(0, 25, len(alt) - cut)   # rewrite the future
    p = TradingParams(min_score=55)
    a = run_backtest(base, p, SPEC)["trades"]
    b = run_backtest(alt, p, SPEC)["trades"]
    t_cut = int(base["time"].iloc[cut - 1])
    early_a = [t for t in a if t["closed"] < t_cut]
    early_b = [t for t in b if t["closed"] < t_cut]
    assert early_a == early_b and len(early_a) > 0


# ---------------- AI ----------------
GOOD = dict(decision="APPROVE", direction="BUY", confidence=85, reasoning="ok", risk_assessment="ACCEPTABLE",
            entry_valid=True, sl_valid=True, tp_valid=True, warnings=[], suggested_adjustments={})


def _cand(ind):
    p = TradingParams(min_score=40)
    for i in range(150, len(ind)):
        r = evaluate(ind.iloc[:i + 1], p)
        if r and r.candidate:
            return r.candidate
    raise AssertionError


def test_payload_adapts_to_model_family():
    b = build_payload("openai/gpt-oss-120b", "{}", "high")
    assert b["reasoning_effort"] == "high" and b["response_format"]["type"] == "json_schema"
    b = build_payload("qwen/qwen3-32b", "{}", "high")
    assert b["reasoning_format"] == "hidden" and "include_reasoning" not in b
    b = build_payload("llama-3.3-70b-versatile", "{}", "high")
    assert not any("reasoning" in k for k in b)


def test_invalid_ai_json_rejected():
    for bad in ("not json", "{}", '{"decision":"MAYBE"}'):
        with pytest.raises(Exception):
            parse_decision(bad)


def test_vet_rules(ind):
    cand = _cand(ind)
    p = TradingParams()
    mk = lambda **kw: AIResult(True, "m", AIDecision(**{**GOOD, "direction": cand.direction, **kw}), "")
    assert vet(mk(), cand, p).approved
    assert not vet(mk(decision="WAIT"), cand, p).approved
    assert not vet(mk(confidence=50), cand, p).approved
    assert not vet(mk(entry_valid=False), cand, p).approved
    assert not vet(mk(risk_assessment="UNACCEPTABLE"), cand, p).approved
    assert not vet(mk(direction="SELL" if cand.direction == "BUY" else "BUY"), cand, p).approved
    assert not vet(AIResult(False, "m", error="timeout"), cand, p).approved


def test_ai_cannot_set_dangerous_stop(ind):
    cand = _cand(ind)
    s = 1 if cand.direction == "BUY" else -1
    wrong_side = cand.entry + s * cand.atr
    far = cand.entry - s * 20 * cand.atr
    for sl in (wrong_side, far):
        r = AIResult(True, "m", AIDecision(**{**GOOD, "direction": cand.direction, "suggested_adjustments": {"stop_loss": sl}}), "")
        v = vet(r, cand, TradingParams())
        assert v.approved and v.sl is None
