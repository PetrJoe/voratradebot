"""Thread-safe wrapper around the MetaTrader5 package: connection, verification, data, positions."""
from __future__ import annotations

import glob
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from ..config import EnvSettings, TIMEFRAME_SECONDS
from .symbols import SymbolSpec, rank_volatility50, spec_from_info

log = logging.getLogger(__name__)

try:  # the package only exists on Windows
    import MetaTrader5 as _mt5
except Exception:  # pragma: no cover
    _mt5 = None


class MT5Error(RuntimeError):
    pass


@dataclass
class ConnectionState:
    connected: bool = False
    demo: bool | None = None
    algo_trading_terminal: bool | None = None
    trade_allowed_account: bool | None = None
    login: int | None = None
    server: str = ""
    company: str = ""
    message: str = "not connected"
    since: float | None = None
    attempts: int = 0
    next_attempt: float = 0.0


class MT5Client:
    def __init__(self, env: EnvSettings, module: Any = _mt5):
        self.env = env
        self.m = module
        self.lock = threading.RLock()
        self.state = ConnectionState()
        self._tf: dict[str, int] = {}
        if module is not None:
            self._tf = {k: getattr(module, f"TIMEFRAME_{k}") for k in TIMEFRAME_SECONDS if hasattr(module, f"TIMEFRAME_{k}")}

    # ------------------------------------------------------------------ connect
    def find_terminal(self) -> str | None:
        if self.env.mt5_path:
            if os.path.isfile(self.env.mt5_path):
                return self.env.mt5_path
            log.warning("MT5_PATH %s does not exist; trying auto-detect", self.env.mt5_path)
        pats = [r"C:\Program Files\*\terminal64.exe", r"C:\Program Files (x86)\*\terminal64.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\*\terminal64.exe")]
        found = sorted(p for pat in pats for p in glob.glob(pat))
        deriv = [p for p in found if "deriv" in p.lower()]
        return (deriv or found or [None])[0]

    def connect(self) -> ConnectionState:
        with self.lock:
            st = self.state
            st.attempts += 1
            if self.m is None:
                return self._fail("MetaTrader5 package not available (Windows + `pip install MetaTrader5` required)")
            try:
                self.m.shutdown()
            except Exception:
                pass
            kw: dict[str, Any] = {"timeout": 60_000}
            path = self.find_terminal()
            if path:
                kw["path"] = path
            pw = self.env.mt5_password.get_secret_value()
            if self.env.mt5_login and pw and self.env.mt5_server:
                kw.update(login=int(self.env.mt5_login), password=pw, server=self.env.mt5_server)
            else:
                log.warning("MT5 credentials incomplete in .env; using the terminal's last logged-in account")
            if not self.m.initialize(**kw):
                return self._fail(f"initialize failed: {self.m.last_error()}")
            deadline = time.time() + 30
            while time.time() < deadline:
                ti = self.m.terminal_info()
                if ti and ti.connected:
                    break
                time.sleep(1)
            return self.verify()

    def _fail(self, msg: str) -> ConnectionState:
        st = self.state
        st.connected, st.message, st.since = False, msg, None
        st.next_attempt = time.time() + min(60, 3 * 2 ** min(st.attempts, 5))
        log.error("MT5: %s", msg)
        return st

    def verify(self) -> ConnectionState:
        """Confirm terminal link, account identity and demo status."""
        with self.lock:
            st = self.state
            ti, acc = self.m.terminal_info(), self.m.account_info()
            if ti is None or acc is None:
                return self._fail(f"terminal/account info unavailable: {self.m.last_error()}")
            if not ti.connected:
                return self._fail("terminal is not connected to the broker")
            if self.env.mt5_login and acc.login != self.env.mt5_login:
                return self._fail(f"logged into account {acc.login}, expected {self.env.mt5_login}")
            st.demo = acc.trade_mode == self.m.ACCOUNT_TRADE_MODE_DEMO
            st.algo_trading_terminal = bool(ti.trade_allowed)
            st.trade_allowed_account = bool(acc.trade_allowed)
            st.login, st.server, st.company = acc.login, acc.server, getattr(acc, "company", "")
            st.connected, st.since, st.attempts = True, st.since or time.time(), 0
            st.message = "connected" if st.demo else "REAL account detected"
            if not st.demo:
                log.critical("Account %s is NOT a demo account", acc.login)
            return st

    def ensure_connected(self) -> bool:
        with self.lock:
            if self.m is None:
                return False
            try:
                ti = self.m.terminal_info()
                if ti is not None and ti.connected and self.m.account_info() is not None:
                    if not self.state.connected:
                        self.verify()
                    return self.state.connected
            except Exception:
                log.exception("terminal_info failed")
            self.state.connected = False
            if time.time() >= self.state.next_attempt:
                self.connect()
            return self.state.connected

    def shutdown(self) -> None:
        with self.lock:
            try:
                if self.m is not None:
                    self.m.shutdown()
            finally:
                self.state.connected = False

    def trading_permitted(self) -> tuple[bool, list[str]]:
        st, why = self.state, []
        if not st.connected:
            why.append("MT5 not connected")
        if st.demo is None:
            why.append("account type unverified")
        elif not st.demo and self.env.demo_only:
            why.append("REAL account while DEMO_ONLY=true")
        if st.algo_trading_terminal is False:
            why.append("Algo Trading is disabled in the MT5 terminal")
        if st.trade_allowed_account is False:
            why.append("trading not allowed on this account")
        return not why, why

    # ------------------------------------------------------------------ account
    def account(self) -> dict | None:
        with self.lock:
            a = self.m.account_info()
            if a is None:
                return None
            return {"login": a.login, "server": a.server, "currency": a.currency, "leverage": a.leverage,
                    "balance": a.balance, "equity": a.equity, "margin": a.margin, "free_margin": a.margin_free,
                    "profit": a.profit, "demo": a.trade_mode == self.m.ACCOUNT_TRADE_MODE_DEMO,
                    "company": getattr(a, "company", "")}

    # ------------------------------------------------------------------ symbols
    def discover_symbol(self) -> str | None:
        with self.lock:
            syms = self.m.symbols_get() or []
            ranked = rank_volatility50(syms)
            for s in ranked:
                if self.m.symbol_select(s.name, True):
                    return s.name
            return None

    def symbol_spec(self, name: str) -> SymbolSpec | None:
        with self.lock:
            info = self.m.symbol_info(name)
            if info is None:
                return None
            if not info.visible:
                self.m.symbol_select(name, True)
                info = self.m.symbol_info(name)
            return spec_from_info(info, self.m.SYMBOL_TRADE_MODE_FULL) if info else None

    def tick(self, symbol: str) -> dict | None:
        with self.lock:
            t = self.m.symbol_info_tick(symbol)
            if t is None or t.bid <= 0 or t.ask <= 0:
                return None
            return {"bid": t.bid, "ask": t.ask, "time": t.time, "time_msc": getattr(t, "time_msc", t.time * 1000)}

    # ------------------------------------------------------------------ market data
    def timeframe(self, tf: str) -> int:
        if tf not in self._tf:
            raise MT5Error(f"unsupported timeframe {tf}")
        return self._tf[tf]

    def rates(self, symbol: str, tf: str, count: int = 600) -> pd.DataFrame | None:
        """Returns DataFrame including the still-forming candle as the last row."""
        with self.lock:
            r = self.m.copy_rates_from_pos(symbol, self.timeframe(tf), 0, count)
            if r is None or len(r) == 0:
                return None
            df = pd.DataFrame(r)
            return df[["time", "open", "high", "low", "close", "tick_volume", "spread"]].astype(
                {"time": "int64", "spread": "float64"})

    def rates_range(self, symbol: str, tf: str, start, end) -> pd.DataFrame | None:
        with self.lock:
            self.m.symbol_select(symbol, True)
            r = self.m.copy_rates_range(symbol, self.timeframe(tf), start, end)
            if r is None or len(r) == 0:
                return None
            return pd.DataFrame(r)[["time", "open", "high", "low", "close", "tick_volume", "spread"]]

    @staticmethod
    def quote_fresh(tick: dict, forming_bar_time: int, tf: str) -> bool:
        """Tick must belong to the current candle window (both values are broker-server time)."""
        age = tick["time"] - forming_bar_time
        return -5 <= age < TIMEFRAME_SECONDS[tf] + 10

    # ------------------------------------------------------------------ positions
    def positions(self, symbol: str | None = None) -> list[dict]:
        with self.lock:
            ps = self.m.positions_get(symbol=symbol) if symbol else self.m.positions_get()
            out = []
            for p in ps or []:
                out.append({"ticket": p.ticket, "symbol": p.symbol, "direction": "BUY" if p.type == self.m.POSITION_TYPE_BUY else "SELL",
                            "volume": p.volume, "price_open": p.price_open, "price_current": p.price_current,
                            "sl": p.sl, "tp": p.tp, "profit": p.profit + getattr(p, "swap", 0.0),
                            "time": p.time, "magic": p.magic, "comment": p.comment})
            return out
