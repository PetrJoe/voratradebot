from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import func

from .backtest import run_backtest
from .config import BASE_DIR, TIMEFRAME_SECONDS, TradingParams
from .db import AIAnalysis, DailyStatistic, ApplicationEvent, ExecutionAttempt, Signal, Trade, row_dict


def require_local_header(request: Request) -> None:
    """State-changing calls need a custom header; browsers will not add it cross-site without a CORS preflight (which we never allow)."""
    if request.headers.get("x-requested-with") != "vora":
        raise HTTPException(403, "missing X-Requested-With header")


router = APIRouter(prefix="/api")
guarded = APIRouter(prefix="/api", dependencies=[Depends(require_local_header)])


def eng(request: Request):
    return request.app.state.engine


# ------------------------------------------------------------------ read endpoints
@router.get("/health")
def health(request: Request):
    e = eng(request)
    return {"ok": True, "engine_error": e.last_error, "time": datetime.now(timezone.utc).isoformat()}


@router.get("/status")
def status(request: Request):
    return eng(request).public_state()


@router.get("/account")
def account(request: Request):
    return eng(request).snapshot.get("account") or {}


@router.get("/market")
def market(request: Request):
    e = eng(request)
    return {"market": e.snapshot.get("market"), "spec": e.snapshot.get("spec"), "symbol_error": e.symbol_error,
            "last_analysis": e.last_analysis}


@router.get("/candles")
def candles(request: Request, timeframe: str = "M5", count: int = 300):
    if timeframe not in TIMEFRAME_SECONDS:
        raise HTTPException(400, "unsupported timeframe")
    return eng(request).chart_data(timeframe, max(50, min(count, 1000)))


@router.get("/positions")
def positions(request: Request):
    return eng(request).snapshot.get("positions", [])


@router.get("/trades")
def trades(request: Request, limit: int = 100):
    with eng(request).db.session() as s:
        return [row_dict(t) for t in s.query(Trade).order_by(Trade.opened_at.desc()).limit(min(limit, 500))]


@router.get("/signals")
def signals(request: Request, limit: int = 100):
    db = eng(request).db
    with db.session() as s:
        ais = {a.signal_id: a for a in s.query(AIAnalysis).order_by(AIAnalysis.id).all()}
        out = []
        for sg in s.query(Signal).order_by(Signal.created_at.desc()).limit(min(limit, 500)):
            d = row_dict(sg)
            a = ais.get(sg.id)
            d["ai"] = {"decision": a.decision, "confidence": a.confidence} if a else None
            out.append(d)
        return out


@router.get("/ai/analyses")
def ai_analyses(request: Request, limit: int = 50):
    with eng(request).db.session() as s:
        return [row_dict(a) | {"warnings": json.loads(a.warnings or "[]")}
                for a in s.query(AIAnalysis).order_by(AIAnalysis.created_at.desc()).limit(min(limit, 200))]


@router.get("/events")
def events(request: Request, limit: int = 100):
    with eng(request).db.session() as s:
        return [row_dict(a) for a in s.query(ApplicationEvent).order_by(ApplicationEvent.id.desc()).limit(min(limit, 300))]


@router.get("/executions")
def executions(request: Request, limit: int = 100):
    with eng(request).db.session() as s:
        return [row_dict(a) for a in s.query(ExecutionAttempt).order_by(ExecutionAttempt.id.desc()).limit(min(limit, 300))]


@router.get("/statistics")
def statistics(request: Request):
    e = eng(request)
    with e.db.session() as s:
        closed = s.query(Trade).filter_by(status="closed").all()
        wins = [t.profit for t in closed if (t.profit or 0) > 0]
        losses = [t.profit for t in closed if (t.profit or 0) < 0]
        days = [row_dict(d) for d in s.query(DailyStatistic).order_by(DailyStatistic.trading_date.desc()).limit(30)]
    gl = -sum(losses)
    return {"source": "LIVE/DEMO trading results", "total_closed": len(closed), "wins": len(wins), "losses": len(losses),
            "win_rate": round(100 * len(wins) / len(closed), 2) if closed else 0.0,
            "net_profit": round(sum(wins) + sum(losses), 2),
            "profit_factor": round(sum(wins) / gl, 3) if gl > 0 else None,
            "loss_streak": e.risk.loss_streak()[0], "trades_today": e.risk.trades_today(e.params),
            "today": days[0] if days else None, "daily": days}


@router.get("/settings")
def get_settings(request: Request):
    e = eng(request)
    return {"values": e.params.model_dump(), "schema": TradingParams.schema_rows(),
            "effective_groq_model": e.effective_model(),
            "env": {"demo_only": e.env.demo_only, "auto_trading": e.flags.auto_trading}}


# ------------------------------------------------------------------ write endpoints
class SettingsUpdate(BaseModel):
    changes: dict
    confirm: bool = False


@guarded.put("/settings")
async def put_settings(body: SettingsUpdate, request: Request):
    code, payload = await asyncio.to_thread(eng(request).update_params, body.changes, body.confirm)
    if code != 200:
        raise HTTPException(code, payload)
    return payload


@guarded.post("/bot/start")
def bot_start(request: Request):
    eng(request).start()
    return {"bot_state": eng(request).bot_state}


@guarded.post("/bot/pause")
def bot_pause(request: Request):
    eng(request).pause()
    return {"bot_state": eng(request).bot_state}


@guarded.post("/bot/resume")
def bot_resume(request: Request):
    eng(request).resume()
    return {"bot_state": eng(request).bot_state}


@guarded.post("/bot/stop")
def bot_stop(request: Request):
    eng(request).stop()
    return {"bot_state": eng(request).bot_state}


@guarded.post("/bot/emergency-stop")
def bot_emergency(request: Request):
    eng(request).emergency()
    return {"bot_state": eng(request).bot_state}


class AutoBody(BaseModel):
    enabled: bool
    confirm: bool = False


@guarded.post("/bot/auto-trading")
def auto_trading(body: AutoBody, request: Request):
    e = eng(request)
    if body.enabled and not body.confirm:
        raise HTTPException(409, {"needs_confirmation": ["auto_trading"], "message": "Enabling auto trading lets the bot place orders."})
    e.set_auto_trading(body.enabled)
    return {"auto_trading": e.flags.auto_trading}


class SymbolBody(BaseModel):
    symbol: str


@guarded.post("/symbol/validate")
async def validate_symbol(body: SymbolBody, request: Request):
    e = eng(request)

    def _go():
        if not e.c.state.connected:
            return {"valid": False, "reason": "MT5 not connected"}
        spec = e.c.symbol_spec(body.symbol)
        if spec is None:
            return {"valid": False, "reason": "symbol not found"}
        return {"valid": spec.tradeable, "reason": None if spec.tradeable else "trading disabled for symbol", "spec": spec.to_dict()}
    return await asyncio.to_thread(_go)


class BacktestBody(BaseModel):
    symbol: str | None = None
    timeframe: str = "M5"
    date_from: datetime
    date_to: datetime
    overrides: dict = Field(default_factory=dict)
    initial_balance: float = Field(10_000, gt=0)
    commission_per_lot: float = Field(0.0, ge=0)
    slippage_points: float = Field(0.0, ge=0)
    use_ai: bool = False


@guarded.post("/backtest")
async def backtest(body: BacktestBody, request: Request):
    e = eng(request)
    if body.use_ai:
        raise HTTPException(400, "Groq-assisted backtests are not available: they need stored historical AI analyses or many paid API calls. Run without use_ai.")
    if body.timeframe not in TIMEFRAME_SECONDS:
        raise HTTPException(400, "unsupported timeframe")
    try:
        params = TradingParams(**{**e.params.model_dump(), **body.overrides, "timeframe": "M5"})
    except Exception as ex:
        raise HTTPException(400, f"invalid parameters: {ex}")

    def _go():
        if not e.c.state.connected:
            raise HTTPException(503, "MT5 not connected (historical data comes from MT5)")
        sym = body.symbol or e.symbol
        spec = e.c.symbol_spec(sym) if sym else None
        if spec is None:
            raise HTTPException(400, "symbol unavailable")
        df = e.c.rates_range(sym, body.timeframe, body.date_from.replace(tzinfo=None), body.date_to.replace(tzinfo=None))
        if df is None:
            raise HTTPException(400, "no historical data for that range")
        tv = spec.tick_value or 1.0
        res = run_backtest(df, params, {"point": spec.point, "tick_size": spec.tick_size, "tick_value": tv,
                                        "volume_min": spec.volume_min, "volume_max": spec.volume_max, "volume_step": spec.volume_step},
                           timeframe=body.timeframe, initial_balance=body.initial_balance,
                           commission_per_lot=body.commission_per_lot, slippage_points=body.slippage_points)
        res.update(symbol=sym, timeframe=body.timeframe, candles=len(df), ran_at=time.time(),
                   note="Tick value taken from the symbol's current specification; results are simulated.")
        out = BASE_DIR / "data" / "backtests"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"backtest_{int(time.time())}.json").write_text(json.dumps(res, default=str))
        e.last_backtest = res
        return res

    try:
        return await asyncio.to_thread(_go)
    except ValueError as ex:
        raise HTTPException(400, str(ex))


@router.get("/backtest/latest")
def backtest_latest(request: Request):
    return getattr(eng(request), "last_backtest", None)


# ------------------------------------------------------------------ websocket
ws_router = APIRouter()


@ws_router.websocket("/ws")
async def ws(websocket: WebSocket):
    origin = websocket.headers.get("origin", "")
    host = websocket.headers.get("host", "")
    if origin and not origin.endswith("//" + host):  # same-origin only
        await websocket.close(code=1008)
        return
    await websocket.accept()
    e = websocket.app.state.engine
    e.hub.clients.add(websocket)
    try:
        await websocket.send_text(json.dumps({"type": "state", "data": e.public_state()}, default=str))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        e.hub.clients.discard(websocket)
