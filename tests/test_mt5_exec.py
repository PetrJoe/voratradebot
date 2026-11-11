import time

import pytest

from app.config import EnvSettings, TradingParams
from app.db import Database, Trade
from app.execution import Executor
from app.mt5.client import MT5Client
from app.risk import RiskManager
from app.state import RuntimeFlags
from app.strategy.signals import Candidate
from fake_mt5 import FakeMT5


def build(tmp_path, **fake_kw):
    fake = FakeMT5(**fake_kw)
    env = EnvSettings(_env_file=None, mt5_login=123, mt5_password="secretpw", mt5_server="Deriv-Demo", demo_only=True,
                      database_url=f"sqlite:///{tmp_path / 'x.db'}")
    c = MT5Client(env, fake)
    c.connect()
    db = Database(env.database_url)
    p = TradingParams()
    flags = RuntimeFlags(auto_trading=True)
    ex = Executor(env, c, RiskManager(db), db, flags, lambda: p)
    spec = c.symbol_spec("Volatility 50 Index")
    tick = c.tick(spec.name)
    entry = tick["ask"]
    cand = Candidate("BUY", 85, entry, entry - 3.0, entry + 6.0, 2.0, 2.0, int(fake.now), {}, [], "UPTREND", {}, {})
    return fake, env, c, db, ex, spec, cand, flags


def test_connect_verifies_demo_and_passes_credentials(tmp_path):
    fake, env, c, *_ = build(tmp_path)
    assert c.state.connected and c.state.demo is True
    assert fake.init_kw["login"] == 123 and fake.init_kw["server"] == "Deriv-Demo"
    assert c.trading_permitted()[0]


def test_real_account_blocks_trading(tmp_path):
    fake, env, c, db, ex, spec, cand, _ = build(tmp_path, demo=False)
    assert c.state.demo is False and not c.trading_permitted()[0]
    res = ex.execute(cand, spec.name, spec, None)
    assert not res.ok and "REAL" in res.message and not fake.send_calls


def test_symbol_discovery_skips_1s_variant(tmp_path):
    fake, env, c, *_ = build(tmp_path)
    assert c.discover_symbol() == "Volatility 50 Index"


def test_happy_path_sets_sl_tp_and_sizes_by_risk(tmp_path):
    fake, env, c, db, ex, spec, cand, _ = build(tmp_path)
    res = ex.execute(cand, spec.name, spec, 1)
    assert res.ok, res.message
    req = fake.send_calls[0]
    assert req["sl"] and req["tp"] and req["magic"] == env.magic_number
    risk_money = req["volume"] * abs(req["sl"] - req["price"]) / 0.01 * 0.01
    assert risk_money <= 10_000 * 0.005 + 1e-6
    with db.session() as s:
        t = s.query(Trade).one()
        assert t.status == "open" and t.stop_loss == req["sl"]


def test_filling_mode_flags_mapped_to_enums(tmp_path):
    fake, env, c, db, ex, spec, cand, _ = build(tmp_path, filling_flags=2)      # IOC only, market execution
    assert ex.filling_candidates(spec) == [fake.ORDER_FILLING_IOC]
    spec.filling_mode = 1
    assert ex.filling_candidates(spec) == [fake.ORDER_FILLING_FOK]
    spec.filling_mode, spec.execution_mode = 3, 1                                  # both + non-market -> RETURN allowed
    assert ex.filling_candidates(spec) == [fake.ORDER_FILLING_IOC, fake.ORDER_FILLING_FOK, fake.ORDER_FILLING_RETURN]


def test_unsupported_fill_falls_back(tmp_path):
    fake, env, c, db, ex, spec, cand, _ = build(tmp_path, filling_flags=3)
    fake.invalid_fill_modes = {fake.ORDER_FILLING_IOC}
    res = ex.execute(cand, spec.name, spec, None)
    assert res.ok and fake.send_calls[0]["type_filling"] == fake.ORDER_FILLING_FOK


def test_auto_trading_off_and_emergency_stop_block_orders(tmp_path):
    fake, env, c, db, ex, spec, cand, flags = build(tmp_path)
    flags.auto_trading = False
    assert not ex.execute(cand, spec.name, spec, None).ok
    flags.auto_trading = True
    ex.risk.emergency_stop = True
    assert not ex.execute(cand, spec.name, spec, None).ok
    assert not fake.send_calls


def test_same_candle_cannot_order_twice(tmp_path):
    fake, env, c, db, ex, spec, cand, _ = build(tmp_path)
    assert ex.execute(cand, spec.name, spec, None).ok
    fake.positions.clear()
    res = ex.execute(cand, spec.name, spec, None)
    assert not res.ok and "candle" in res.message


def test_max_one_position(tmp_path):
    fake, env, c, db, ex, spec, cand, _ = build(tmp_path)
    assert ex.execute(cand, spec.name, spec, None).ok
    cand2 = Candidate(**{**cand.__dict__, "bar_time": cand.bar_time + 300})
    res = ex.execute(cand2, spec.name, spec, None)
    assert not res.ok and len(fake.send_calls) == 1


def test_rejects_when_risk_reward_too_low(tmp_path):
    fake, env, c, db, ex, spec, cand, _ = build(tmp_path)
    res = ex.execute(cand, spec.name, spec, None, tp_override=cand.entry + 1.0)
    assert not res.ok and "risk/reward" in res.message and not fake.send_calls


def test_requote_retries_once_at_fresh_price(tmp_path):
    fake, env, c, db, ex, spec, cand, _ = build(tmp_path)
    fake.send_script = [10004, 10009]
    res = ex.execute(cand, spec.name, spec, None)
    assert res.ok and len(fake.send_calls) == 2


def test_timeout_reconciles_instead_of_resending(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    fake, env, c, db, ex, spec, cand, _ = build(tmp_path)
    fake.send_script, fake.leak_on_timeout = [None], True       # order executed but reply lost
    res = ex.execute(cand, spec.name, spec, None)
    assert res.ok and len(fake.send_calls) == 1 and len(fake.positions) == 1


def test_timeout_with_no_position_blocks_not_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    fake, env, c, db, ex, spec, cand, _ = build(tmp_path)
    fake.send_script = [10012]
    res = ex.execute(cand, spec.name, spec, None)
    assert not res.ok and len(fake.send_calls) == 1 and ex.blocked_until > time.time()


def test_unprotected_position_is_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    fake, env, c, db, ex, spec, cand, _ = build(tmp_path)
    fake.drop_sltp, fake.sltp_modify_ok = True, False
    res = ex.execute(cand, spec.name, spec, None)
    assert not res.ok and res.status == "unprotected" and fake.positions == []


def test_modify_never_removes_stops(tmp_path):
    fake, env, c, db, ex, spec, cand, _ = build(tmp_path)
    assert not ex.modify_sltp(1, spec.name, 0.0, 10.0)
    assert not ex.modify_sltp(1, spec.name, 10.0, 0.0)
