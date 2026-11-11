"""Background engine. Blocking MT5/DB work runs in worker threads; the FastAPI event loop is never blocked."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd
from pydantic import ValidationError

from .ai.groq_client import GroqClient
from .ai.validate import vet
from .config import EnvSettings, TIMEFRAME_SECONDS, TradingParams, risky_changes
from .db import AIAnalysis, AccountSnapshot, Database, Signal, Trade, row_dict, utcnow
from .execution import Executor
from .mt5.client import MT5Client
from .mt5.symbols import SymbolSpec
from .positions import PositionManager
from .risk import RiskManager
from .state import BotState, RuntimeFlags
from .strategy.indicators import add_indicators
from .strategy.price_action import add_price_action
from .strategy.signals import evaluate, snapshot

log = logging.getLogger(__name__)


class Hub:
    """WebSocket fan-out."""
    def __init__(self):
        self.clients: set = set()

    async def broadcast(self, payload: dict) -> None:
        dead = []
        msg = json.dumps(payload, default=str)
        for ws in list(self.clients):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


@dataclass
class Cycle:
    df: pd.DataFrame
    tick: dict
    account: dict
    forming_time: int
    last_closed_time: int
    positions: list
    data_ok: bool


class Engine:
    def __init__(self, env: EnvSettings, db: Database, client: MT5Client, groq: GroqClient, risk: RiskManager,
                 executor: Executor, posmgr: PositionManager, flags: RuntimeFlags, hub: Hub):
        self.env, self.db, self.c, self.groq, self.risk, self.ex, self.pm, self.flags, self.hub = \
            env, db, client, groq, risk, executor, posmgr, flags, hub
        self.params = self._load_params()
        self.bot_state = BotState.STOPPED
        self.symbol: str | None = None
        self.spec: SymbolSpec | None = None
        self.symbol_error: str | None = None
        self.snapshot: dict = {}
        self.last_analysis: dict | None = None
        self.last_ai: dict | None = None
        self.last_analyzed_bar: int | None = None
        self._df: pd.DataFrame | None = None
        self._df_time: int | None = None
        self._task: asyncio.Task | None = None
        self._last_account_snap = 0.0
        self._last_groq_check = 0.0
        self.last_error: str | None = None
        self.flags.auto_trading = env.auto_trading
        if env.auto_trading:
            log.warning("AUTO_TRADING=true in .env: orders will be sent once the bot is started")

    # ------------------------------------------------------------------ params
    def _load_params(self) -> TradingParams:
        saved = self.db.kv_all("param.")
        clean = {}
        for k, v in saved.items():
            key = k[len("param."):]
            if key in TradingParams.model_fields:
                try:
                    TradingParams.model_validate({**TradingParams().model_dump(), key: v})
                    clean[key] = v
                except ValidationError:
                    log.warning("Ignoring invalid saved setting %s", key)
        return TradingParams(**clean)

    def effective_model(self) -> str:
        return self.params.groq_model or self.env.groq_model

    def update_params(self, changes: dict, confirm: bool) -> tuple[int, dict]:
        try:
            new = TradingParams(**{**self.params.model_dump(), **changes})
        except ValidationError as e:
            return 400, {"error": "invalid settings", "details": json.loads(e.json())}
        risky = risky_changes(self.params, new)
        if risky and not confirm:
            return 409, {"needs_confirmation": risky, "message": "These changes increase risk. Resend with confirm=true."}
        if new.symbol != self.params.symbol and new.symbol:
            spec = self.c.symbol_spec(new.symbol) if self.c.state.connected else None
            if spec is None or not spec.tradeable:
                return 400, {"error": f"symbol '{new.symbol}' not found or not tradeable on this account"}
        old = self.params
        for k, v in changes.items():
            if getattr(old, k) != getattr(new, k):
                self.db.kv_set(f"param.{k}", getattr(new, k))
                self.db.event("settings", f"{k}: {getattr(old, k)} -> {getattr(new, k)}", {"key": k})
        self.params = new
        self.ex.get_params = lambda: self.params
        if new.symbol != old.symbol:
            self.spec = None
        return 200, {"ok": True, "values": new.model_dump()}

    # ------------------------------------------------------------------ bot control
    def start(self) -> None:
        if self.bot_state == BotState.EMERGENCY:
            self.db.event("control", "Emergency stop cleared by user")
        self.risk.emergency_stop = False
        self.last_analyzed_bar = self._df_time  # only trade candles that close AFTER start
        self.bot_state = BotState.RUNNING
        self.db.event("control", "Bot started")

    def pause(self) -> None:
        if self.bot_state == BotState.RUNNING:
            self.bot_state = BotState.PAUSED
            self.db.event("control", "Bot paused")

    def resume(self) -> None:
        if self.bot_state == BotState.PAUSED:
            self.last_analyzed_bar = self._df_time
            self.bot_state = BotState.RUNNING
            self.db.event("control", "Bot resumed")

    def stop(self) -> None:
        self.bot_state = BotState.STOPPED
        self.db.event("control", "Bot stopped")

    def emergency(self) -> None:
        self.risk.emergency_stop = True  # takes effect immediately inside Executor
        self.bot_state = BotState.EMERGENCY
        self.db.event("emergency", "EMERGENCY STOP: new orders blocked")
        log.critical("EMERGENCY STOP engaged")

    def set_auto_trading(self, on: bool) -> None:
        self.flags.auto_trading = on
        self.db.event("control", f"Auto trading {'ENABLED' if on else 'disabled'}")

    # ------------------------------------------------------------------ lifecycle
    async def start_loop(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="vora-engine")

    async def shutdown(self) -> None:
        self.bot_state = BotState.STOPPED
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        await asyncio.to_thread(self.c.shutdown)

    async def _loop(self) -> None:
        while True:
            try:
                await self._cycle()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("engine cycle failed")
            await asyncio.sleep(self.env.poll_seconds)

    async def _cycle(self) -> None:
        cyc = await asyncio.to_thread(self._monitor)
        if time.time() - self._last_groq_check > 300 and self.groq.configured:
            self._last_groq_check = time.time()
            await self.groq.check(self.effective_model())
        await self.hub.broadcast({"type": "state", "data": self.public_state()})
        if cyc and self.bot_state == BotState.RUNNING and cyc.data_ok:
            await self._maybe_trade(cyc)

    # ------------------------------------------------------------------ monitoring (worker thread)
    def _monitor(self) -> Cycle | None:
        p = self.params
        if not self.c.ensure_connected():
            self.snapshot = {**self.snapshot, "market": None}
            return None
        target = p.symbol or self.symbol
        if self.spec is None or (p.symbol and self.spec.name != p.symbol):
            name = p.symbol or self.c.discover_symbol()
            spec = self.c.symbol_spec(name) if name else None
            if spec is None:
                self.symbol_error = "Volatility 50 symbol not found on this account" if not p.symbol else f"symbol {p.symbol} not found"
                self.spec = None
                return None
            self.spec, self.symbol, self.symbol_error = spec, spec.name, None
            self._df = self._df_time = None
            log.info("Using symbol %s", spec.name)
        sym, spec = self.symbol, self.spec
        rates = self.c.rates(sym, p.timeframe, 700)
        tick, acc = self.c.tick(sym), self.c.account()
        if rates is None or len(rates) < 150 or tick is None or acc is None:
            return None
        forming_time = int(rates["time"].iloc[-1])
        closed = rates.iloc[:-1].reset_index(drop=True)
        last_closed = int(closed["time"].iloc[-1])
        if last_closed != self._df_time:
            self._df = add_price_action(add_indicators(closed))
            self._df_time = last_closed
        df = self._df
        tfs = TIMEFRAME_SECONDS[p.timeframe]
        data_ok = (forming_time - last_closed) <= 1.5 * tfs and self.c.quote_fresh(tick, forming_time, p.timeframe) \
            and not df.iloc[-1][["ema50", "rsi", "adx", "atr"]].isna().any()
        atr = float(df["atr"].iloc[-1]) if pd.notna(df["atr"].iloc[-1]) else None
        lock = self.risk.update_lock(acc["equity"], p)
        positions = self.pm.sync(sym, spec, p, atr)
        self.risk.refresh_trade_counts(p)
        if time.time() - self._last_account_snap > 60:
            self._last_account_snap = time.time()
            with self.db.session() as s:
                s.add(AccountSnapshot(balance=acc["balance"], equity=acc["equity"], margin=acc["margin"],
                                      free_margin=acc["free_margin"], daily_baseline=lock["starting_equity"]))
        last = df.iloc[-1]
        spread = tick["ask"] - tick["bid"]
        floating = sum(x["profit"] for x in positions)
        self.snapshot = {
            "account": {**acc, "floating": floating, "daily_baseline": lock["starting_equity"], "daily_pnl": lock["pnl"],
                        "daily_limit": lock["limit"], "daily_remaining": lock["remaining"], "risk_locked": lock["locked"],
                        "lock_reason": lock["lock_reason"]},
            "market": {"symbol": sym, "timeframe": p.timeframe, "bid": tick["bid"], "ask": tick["ask"], "price": (tick["bid"] + tick["ask"]) / 2,
                       "spread": spread, "spread_points": spread / spec.point if spec.point else None, "digits": spec.digits,
                       "trend": snapshot_trend(last), "rsi": _r(last.rsi), "atr": _r(last.atr), "adx": _r(last.adx),
                       "data_ok": data_ok, "last_candle": last_closed},
            "positions": positions,
            "spec": spec.to_dict(),
        }
        return Cycle(df, tick, acc, forming_time, last_closed, positions, data_ok)

    # ------------------------------------------------------------------ trading flow
    async def _maybe_trade(self, cyc: Cycle) -> None:
        if cyc.last_closed_time == self.last_analyzed_bar:
            return
        self.last_analyzed_bar = cyc.last_closed_time
        p = self.params
        res = evaluate(cyc.df, p)
        if res is None:
            return
        self.last_analysis = {"time": utcnow().isoformat() + "Z", "bar": cyc.last_closed_time, "trend": res.trend,
                              "buy_score": res.buy_score, "sell_score": res.sell_score, "buy_blocks": res.buy_blocks,
                              "sell_blocks": res.sell_blocks, "candidate": res.candidate.direction if res.candidate else None}
        cand = res.candidate
        if cand is None:
            return
        sig_id = await asyncio.to_thread(self._store_signal, cand)
        if sig_id is None:
            return  # already processed this bar (e.g. before a restart)
        spread = cyc.tick["ask"] - cyc.tick["bid"]
        dec = await asyncio.to_thread(lambda: self.risk.pre_trade_check(
            p, equity=cyc.account["equity"], open_positions=len([x for x in cyc.positions if x.get("ours")]),
            spread=spread, atr=cand.atr))
        if not dec.allowed:
            await asyncio.to_thread(self._set_signal, sig_id, "rejected_risk", "; ".join(dec.reasons))
            return
        # ---- Groq (only for qualified, risk-cleared setups)
        model = self.effective_model()
        snap = self._ai_snapshot(cand, cyc)
        result = await self.groq.evaluate(model, snap)
        verdict = vet(result, cand, p)
        await asyncio.to_thread(self._store_ai, sig_id, model, result, verdict)
        self.last_ai = {"time": utcnow().isoformat() + "Z", "signal_id": sig_id, "direction": cand.direction,
                        "decision": result.decision.decision if result.decision else "ERROR",
                        "confidence": result.decision.confidence if result.decision else None,
                        "reasoning": result.decision.reasoning if result.decision else result.error,
                        "risk_assessment": result.decision.risk_assessment if result.decision else None,
                        "warnings": result.decision.warnings if result.decision else [],
                        "accepted": verdict.approved, "rejection_reason": None if verdict.approved else verdict.reason}
        if not verdict.approved:
            await asyncio.to_thread(self._set_signal, sig_id, "rejected_ai", verdict.reason)
            return
        if not self.flags.auto_trading:
            await asyncio.to_thread(self._set_signal, sig_id, "approved_not_executed", "AUTO_TRADING is off")
            return
        summary = f"{result.decision.decision} {result.decision.confidence:.0f}%: {result.decision.reasoning}"
        ex = await asyncio.to_thread(lambda: self.ex.execute(cand, self.symbol, self.spec, sig_id, sl_override=verdict.sl,
                                                              tp_override=verdict.tp, ai_summary=summary))
        await asyncio.to_thread(self._set_signal, sig_id, "executed" if ex.ok else "execution_failed", ex.message, ex.ok)

    def _ai_snapshot(self, cand, cyc: Cycle) -> dict:
        df, spec = cyc.df, self.spec
        candles = df.tail(30)[["time", "open", "high", "low", "close"]].round(spec.digits).to_dict("records")
        today = self.risk.today(self.params)
        info = self.snapshot.get("account", {})
        return {
            "instrument": self.symbol, "timeframe": self.params.timeframe,
            "bid": cyc.tick["bid"], "ask": cyc.tick["ask"], "spread": cyc.tick["ask"] - cyc.tick["bid"],
            "recent_candles_oldest_first": candles, "indicators": cand.indicators, "trend": cand.trend,
            "candlestick_patterns": cand.patterns, "support_resistance_fibonacci": cand.levels,
            "signal": {"direction": cand.direction, "score": cand.score, "components": cand.components},
            "proposal": {"entry": cand.entry, "stop_loss": cand.stop_loss, "take_profit": cand.take_profit, "risk_reward": round(cand.rr, 2)},
            "account": {"equity": cyc.account["equity"], "currency": cyc.account["currency"]},
            "open_positions": [{"direction": x["direction"], "volume": x["volume"], "price_open": x["price_open"], "profit": x["profit"]}
                               for x in cyc.positions if x.get("ours")],
            "daily_stats": {"date": today, "pnl": info.get("daily_pnl"), "trades_today": self.risk.trades_today(self.params),
                            "daily_loss_limit_remaining": info.get("daily_remaining")},
        }

    # ------------------------------------------------------------------ persistence helpers
    def _store_signal(self, cand) -> int | None:
        with self.db.session() as s:
            if s.query(Signal).filter_by(symbol=self.symbol, timeframe=self.params.timeframe, bar_time=cand.bar_time).first():
                return None
            sig = Signal(symbol=self.symbol, timeframe=self.params.timeframe, direction=cand.direction, score=cand.score,
                         indicator_snapshot=json.dumps(cand.indicators), bar_time=cand.bar_time, entry=cand.entry,
                         stop_loss=cand.stop_loss, take_profit=cand.take_profit,
                         price_action_snapshot=json.dumps({"patterns": cand.patterns, "components": cand.components, "levels": cand.levels}, default=str),
                         reason=cand.evidence())
            s.add(sig)
            s.flush()
            return sig.id

    def _set_signal(self, sig_id: int, status: str, reason: str, executed: bool = False) -> None:
        with self.db.session() as s:
            sig = s.get(Signal, sig_id)
            sig.status, sig.executed = status, executed
            sig.reason = (sig.reason or "")[:1500] + f" || {reason}"

    def _store_ai(self, sig_id, model, result, verdict) -> None:
        d = result.decision
        with self.db.session() as s:
            s.add(AIAnalysis(signal_id=sig_id, model=model, decision=d.decision if d else "ERROR",
                             confidence=d.confidence if d else None, reasoning=d.reasoning if d else result.error,
                             warnings=json.dumps(d.warnings if d else []), raw_response=(result.raw or "")[:8000],
                             direction=d.direction if d else None, risk_assessment=d.risk_assessment if d else None,
                             accepted=verdict.approved, rejection_reason=None if verdict.approved else verdict.reason))

    # ------------------------------------------------------------------ state for UI
    def public_state(self) -> dict:
        st = self.c.state
        lock = self.snapshot.get("account", {})
        connected = st.connected
        flags = {
            "connected": connected, "running": self.bot_state == BotState.RUNNING, "paused": self.bot_state == BotState.PAUSED,
            "stopped": self.bot_state == BotState.STOPPED, "emergency": self.bot_state == BotState.EMERGENCY,
            "risk_locked": bool(lock.get("risk_locked")), "connection_lost": not connected,
            "ai_unavailable": (not self.groq.configured) or self.groq.available is False,
        }
        ok, why = self.c.trading_permitted()
        return {
            "time": datetime.now(timezone.utc).isoformat(), "bot_state": self.bot_state.value, "flags": flags,
            "auto_trading": self.flags.auto_trading, "demo_only": self.env.demo_only,
            "trading_permitted": ok, "not_permitted_reasons": why,
            "mt5": {"connected": connected, "message": st.message, "demo": st.demo, "login": st.login, "server": st.server,
                    "algo_trading": st.algo_trading_terminal},
            "groq": {"configured": self.groq.configured, "available": self.groq.available, "message": self.groq.status_message,
                     "model": self.effective_model()},
            "symbol": self.symbol, "symbol_error": self.symbol_error, "timeframe": self.params.timeframe,
            "account": self.snapshot.get("account"), "market": self.snapshot.get("market"),
            "positions": self.snapshot.get("positions", []), "last_analysis": self.last_analysis, "last_ai": self.last_ai,
            "engine_error": self.last_error,
        }

    # ------------------------------------------------------------------ chart data (worker thread)
    def chart_data(self, tf: str, count: int = 300) -> dict:
        if not self.symbol or not self.c.state.connected:
            return {"candles": [], "ema": {}, "markers": [], "lines": []}
        r = self.c.rates(self.symbol, tf, count + 60)
        if r is None:
            return {"candles": [], "ema": {}, "markers": [], "lines": []}
        d = add_indicators(r)
        d = d.tail(count)
        candles = d[["time", "open", "high", "low", "close"]].to_dict("records")
        ema = {f"ema{n}": [{"time": int(a), "value": float(b)} for a, b in zip(d["time"], d[f"ema{n}"]) if pd.notna(b)] for n in (9, 21, 50)}
        first = int(d["time"].iloc[0])
        markers, lines = [], []
        with self.db.session() as s:
            for sg in s.query(Signal).filter(Signal.symbol == self.symbol, Signal.bar_time >= first).all():
                markers.append({"time": sg.bar_time, "dir": sg.direction, "executed": sg.executed, "status": sg.status})
            for t in s.query(Trade).filter_by(status="open", symbol=self.symbol).all():
                lines.append({"price": t.entry_price, "kind": "entry", "dir": t.direction})
                if t.stop_loss:
                    lines.append({"price": t.stop_loss, "kind": "sl"})
                if t.take_profit:
                    lines.append({"price": t.take_profit, "kind": "tp"})
        return {"candles": candles, "ema": ema, "markers": markers, "lines": lines, "digits": self.spec.digits if self.spec else 2}


def _r(x) -> float | None:
    return None if pd.isna(x) else round(float(x), 4)


def snapshot_trend(row) -> str:
    from .strategy.signals import trend_of
    return trend_of(row)
