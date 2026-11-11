"""Minimal in-memory stand-in for the MetaTrader5 module (for tests only)."""
from __future__ import annotations

import time
from types import SimpleNamespace as NS

import numpy as np


class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_REAL = 0, 2
    SYMBOL_TRADE_MODE_FULL, SYMBOL_TRADE_EXECUTION_MARKET = 4, 2
    TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15, TIMEFRAME_M30, TIMEFRAME_H1 = 1, 5, 15, 30, 16385
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    ORDER_TIME_GTC = 0
    TRADE_ACTION_DEAL, TRADE_ACTION_SLTP = 1, 6
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT = 0, 1
    DEAL_REASON_SL, DEAL_REASON_TP, DEAL_REASON_SO = 4, 5, 6

    def __init__(self, demo=True, filling_flags=2, exec_mode=2):
        self.demo, self.filling_flags, self.exec_mode = demo, filling_flags, exec_mode
        self.connected = True
        self.positions: list[NS] = []
        self.send_script: list = []          # retcodes (or None for no reply) consumed by order_send in order
        self.send_calls: list[dict] = []
        self.check_calls: list[dict] = []
        self.invalid_fill_modes: set[int] = set()
        self.leak_on_timeout = False         # if True a timed-out order still opens a position
        self.drop_sltp = False               # simulate broker ignoring SL/TP on open
        self.sltp_modify_ok = True
        self.equity = 10_000.0
        self.now = int(time.time())
        self._ticket = 1000

    # --- lifecycle
    def initialize(self, **kw): self.init_kw = kw; return True
    def shutdown(self): return True
    def last_error(self): return (1, "ok")
    def terminal_info(self): return NS(connected=self.connected, trade_allowed=True)
    def account_info(self):
        return NS(login=123, server="Deriv-Demo", currency="USD", leverage=500, balance=self.equity, equity=self.equity,
                  margin=0.0, margin_free=self.equity, profit=0.0, company="Deriv",
                  trade_mode=self.ACCOUNT_TRADE_MODE_DEMO if self.demo else self.ACCOUNT_TRADE_MODE_REAL, trade_allowed=True)

    # --- symbols
    def _info(self, name="Volatility 50 Index"):
        return NS(name=name, description=name, path="Volatility Indices\\Continuous", visible=True, digits=2, point=0.01,
                  trade_tick_size=0.01, trade_tick_value=0.01, trade_contract_size=1, volume_min=0.001, volume_max=100,
                  volume_step=0.001, trade_stops_level=10, trade_freeze_level=0, trade_mode=4, trade_exemode=self.exec_mode,
                  filling_mode=self.filling_flags, currency_profit="USD")
    def symbols_get(self): return [self._info("Volatility 50 (1s) Index"), self._info(), self._info("EURUSD")]
    def symbol_select(self, *a): return True
    def symbol_info(self, name): return self._info(name) if "Volatility 50" in name else None
    @staticmethod
    def _px(t): return 1000 + 0.1 * ((t - 1_700_000_000) // 300)      # price depends only on the candle's open time
    def symbol_info_tick(self, name):
        px = self._px(self.now - self.now % 300) + 0.2
        return NS(bid=px, ask=px + 0.2, time=self.now, time_msc=self.now * 1000)

    # --- data
    def copy_rates_from_pos(self, sym, tf, pos, count):
        t0 = self.now - (self.now % 300)
        times = np.arange(count) * 300 + (t0 - (count - 1) * 300)
        base = self._px(times)
        return np.rec.fromarrays([times, base, base * 1.001, base * 0.999, base + 0.2, np.ones(count), np.full(count, 20), np.ones(count)],
                                 names="time,open,high,low,close,tick_volume,spread,real_volume")
    copy_rates_range = None

    # --- trading
    def positions_get(self, symbol=None, **kw): return list(self.positions)
    def order_calc_profit(self, otype, sym, vol, p_open, p_close): return (p_close - p_open) / 0.01 * 0.01 * vol * (1 if otype == 0 else -1)
    def order_calc_margin(self, otype, sym, vol, price): return 5.0
    def order_check(self, req):
        self.check_calls.append(dict(req))
        if req["type_filling"] in self.invalid_fill_modes:
            return NS(retcode=10030, comment="Unsupported filling mode")
        return NS(retcode=0, comment="Done")
    def order_send(self, req):
        self.send_calls.append(dict(req))
        if req["action"] == self.TRADE_ACTION_SLTP:
            if not self.sltp_modify_ok:
                return NS(retcode=10016, comment="Invalid stops")
            for p in self.positions:
                if p.ticket == req["position"]:
                    p.sl, p.tp = req["sl"], req["tp"]
            return NS(retcode=10009, comment="ok")
        if "position" in req:  # closing deal
            self.positions = [p for p in self.positions if p.ticket != req["position"]]
            return NS(retcode=10009, comment="closed", deal=0, order=0)
        rc = self.send_script.pop(0) if self.send_script else 10009
        opens = rc in (10009, 10010) or (rc is None or rc in (10012, 10031)) and self.leak_on_timeout
        if opens:
            self._ticket += 1
            self.positions.append(NS(ticket=self._ticket, symbol=req["symbol"], type=req["type"], volume=req["volume"], price_open=req["price"],
                                     price_current=req["price"], sl=0.0 if self.drop_sltp else req["sl"], tp=0.0 if self.drop_sltp else req["tp"],
                                     profit=0.0, swap=0.0, time=self.now, magic=req["magic"], comment=req["comment"]))
        if rc is None:
            return None
        return NS(retcode=rc, comment=f"rc {rc}", deal=0, order=self._ticket)
    def history_deals_get(self, **kw): return []
