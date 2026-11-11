import pytest
from fastapi.testclient import TestClient

from app.config import EnvSettings
from app.main import create_app
from fake_mt5 import FakeMT5

H = {"X-Requested-With": "vora"}


@pytest.fixture
def client(tmp_path):
    env = EnvSettings(_env_file=None, mt5_login=123, mt5_password="secretpw", mt5_server="Deriv-Demo", poll_seconds=0.2,
                      database_url=f"sqlite:///{tmp_path / 'api.db'}", log_dir=str(tmp_path / "logs"))
    with TestClient(create_app(env, FakeMT5())) as c:
        yield c


def test_read_endpoints(client):
    for path in ("/api/health", "/api/status", "/api/account", "/api/market", "/api/positions", "/api/trades", "/api/signals",
                 "/api/ai/analyses", "/api/statistics", "/api/settings", "/api/events", "/api/executions"):
        assert client.get(path).status_code == 200, path
    assert client.get("/").status_code == 200


def test_state_changing_calls_need_header(client):
    assert client.post("/api/bot/start").status_code == 403
    assert client.post("/api/bot/start", headers=H).status_code == 200


def test_bot_state_machine(client):
    st = lambda: client.get("/api/status").json()["bot_state"]
    assert st() == "STOPPED"
    client.post("/api/bot/start", headers=H); assert st() == "RUNNING"
    client.post("/api/bot/pause", headers=H); assert st() == "PAUSED"
    client.post("/api/bot/resume", headers=H); assert st() == "RUNNING"
    client.post("/api/bot/emergency-stop", headers=H); assert st() == "EMERGENCY_STOP"
    client.post("/api/bot/stop", headers=H); assert st() == "STOPPED"


def test_auto_trading_needs_confirmation(client):
    assert client.post("/api/bot/auto-trading", json={"enabled": True}, headers=H).status_code == 409
    assert client.post("/api/bot/auto-trading", json={"enabled": True, "confirm": True}, headers=H).status_code == 200
    assert client.get("/api/status").json()["auto_trading"] is True


def test_dangerous_setting_needs_confirmation_and_bounds_enforced(client):
    r = client.put("/api/settings", json={"changes": {"risk_per_trade_pct": 1.5}}, headers=H)
    assert r.status_code == 409
    assert client.put("/api/settings", json={"changes": {"risk_per_trade_pct": 1.5}, "confirm": True}, headers=H).status_code == 200
    assert client.get("/api/settings").json()["values"]["risk_per_trade_pct"] == 1.5
    assert client.put("/api/settings", json={"changes": {"risk_per_trade_pct": 40}, "confirm": True}, headers=H).status_code == 400
    assert client.put("/api/settings", json={"changes": {"risk_per_trade_pct": 0.25}}, headers=H).status_code == 200   # safer: no confirm


def test_symbol_validation(client):
    assert client.post("/api/symbol/validate", json={"symbol": "Volatility 50 Index"}, headers=H).json()["valid"] is True
    assert client.post("/api/symbol/validate", json={"symbol": "Nope"}, headers=H).json()["valid"] is False


def test_engine_connects_and_exposes_market(client):
    import time
    for _ in range(30):
        s = client.get("/api/status").json()
        if s["market"]:
            break
        time.sleep(0.2)
    assert s["mt5"]["demo"] is True and s["symbol"] == "Volatility 50 Index" and s["market"]["bid"] > 0
    assert client.get("/api/candles?timeframe=M5&count=100").json()["candles"]


def test_groq_backtest_refused(client):
    r = client.post("/api/backtest", json={"date_from": "2026-01-01T00:00:00", "date_to": "2026-01-10T00:00:00", "use_ai": True}, headers=H)
    assert r.status_code == 400


def test_websocket_same_origin(client):
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "state"


def test_full_loop_new_candle_to_order(tmp_path, monkeypatch):
    """New closed candle -> strategy -> (mock) Groq -> Python validation -> order with SL/TP -> DB records."""
    import time
    from app.ai.groq_client import AIDecision, AIResult, GroqClient

    async def fake_eval(self, model, snap):
        assert "password" not in str(snap).lower()
        return AIResult(True, model, AIDecision(decision="APPROVE", direction=snap["signal"]["direction"], confidence=90, reasoning="clear trend",
                                                risk_assessment="ACCEPTABLE", entry_valid=True, sl_valid=True, tp_valid=True), "{}")
    monkeypatch.setattr(GroqClient, "evaluate", fake_eval)
    fake = FakeMT5()
    env = EnvSettings(_env_file=None, mt5_login=123, mt5_password="secretpw", mt5_server="Deriv-Demo", poll_seconds=0.1,
                      database_url=f"sqlite:///{tmp_path / 'loop.db'}", log_dir=str(tmp_path / "logs"))
    with TestClient(create_app(env, fake)) as c:
        c.post("/api/symbol/validate", json={"symbol": "Volatility 50 Index"}, headers=H)
        c.put("/api/settings", json={"changes": {"min_score": 40, "require_adx": False, "cooldown_minutes_after_trade": 0}, "confirm": True}, headers=H)
        c.post("/api/bot/auto-trading", json={"enabled": True, "confirm": True}, headers=H)
        time.sleep(0.6)
        c.post("/api/bot/start", headers=H)       # waits for the NEXT closed candle
        time.sleep(0.5)
        assert c.get("/api/trades").json() == []
        fake.now += 300                           # a new candle closes
        for _ in range(60):
            trades = c.get("/api/trades").json()
            if trades:
                break
            time.sleep(0.2)
        sigs = c.get("/api/signals").json()
        assert sigs, "a candidate should have been recorded"
        assert trades, f"expected an order; signal status={sigs[0]['status']} reason={sigs[0]['reason']}"
        assert trades[0]["stop_loss"] and trades[0]["take_profit"] and "clear trend" in trades[0]["ai_decision"]
        assert sigs[0]["executed"] is True
        assert c.get("/api/ai/analyses").json()[0]["accepted"] is True
        assert len(fake.send_calls) == 1          # never analysed/ordered twice for the same candle
        time.sleep(0.8)
        assert len(fake.send_calls) == 1
