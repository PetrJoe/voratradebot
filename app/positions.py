"""Keeps DB trades in sync with MT5 and applies optional break-even / trailing / partial exits."""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from .config import EnvSettings, TradingParams
from .db import Database, Trade, utcnow
from .execution import Executor
from .mt5.client import MT5Client
from .mt5.symbols import SymbolSpec

log = logging.getLogger(__name__)


class PositionManager:
    def __init__(self, env: EnvSettings, client: MT5Client, executor: Executor, db: Database):
        self.env, self.c, self.ex, self.db = env, client, executor, db

    def sync(self, symbol: str, spec: SymbolSpec, p: TradingParams, atr: float | None) -> list[dict]:
        """Returns the live positions for display (all positions on the symbol; ours flagged)."""
        live_all = self.c.positions(symbol)
        ours = {x["ticket"]: x for x in live_all if x["magic"] == self.env.magic_number}
        with self.db.session() as s:
            open_rows = {t.mt5_ticket: t for t in s.query(Trade).filter_by(status="open").all()}
            for tk, x in ours.items():
                t = open_rows.get(tk)
                if t is None:  # e.g. order accepted after an uncertain timeout -> adopt it
                    t = Trade(mt5_ticket=tk, symbol=x["symbol"], direction=x["direction"], volume=x["volume"],
                              entry_price=x["price_open"], stop_loss=x["sl"] or None, take_profit=x["tp"] or None,
                              strategy_reason="adopted existing position", status="open",
                              initial_risk=abs(x["price_open"] - x["sl"]) if x["sl"] else None,
                              opened_at=datetime.fromtimestamp(x["time"], timezone.utc).replace(tzinfo=None))
                    s.add(t)
                    s.flush()
                    self.db.event("adopt", f"Adopted position {tk}")
                sgn = 1 if x["direction"] == "BUY" else -1
                move = (x["price_current"] - x["price_open"]) * sgn
                t.mfe, t.mae = max(t.mfe or 0.0, move), max(t.mae or 0.0, -move)
                t.stop_loss, t.take_profit, t.volume = x["sl"] or t.stop_loss, x["tp"] or t.take_profit, x["volume"]
            for tk, t in open_rows.items():
                if tk not in ours:
                    self._close_row(t)
        for x in ours.values():
            try:
                self._manage(x, spec, p, atr)
            except Exception:
                log.exception("position management failed for %s", x["ticket"])
        return [dict(x, ours=x["ticket"] in ours) for x in live_all]

    def _close_row(self, t: Trade) -> None:
        with self.c.lock:
            deals = self.c.m.history_deals_get(position=t.mt5_ticket)
        if not deals:
            return  # history may lag; retry next cycle
        m = self.c.m
        outs = [d for d in deals if d.entry in (m.DEAL_ENTRY_OUT, getattr(m, "DEAL_ENTRY_OUT_BY", 3))]
        if not outs:
            return
        last = sorted(outs, key=lambda d: d.time_msc if hasattr(d, "time_msc") else d.time)[-1]
        t.exit_price = last.price
        t.profit = float(sum(d.profit + d.commission + d.swap + getattr(d, "fee", 0.0) for d in deals))
        reasons = {getattr(m, "DEAL_REASON_SL", 4): "stop_loss", getattr(m, "DEAL_REASON_TP", 5): "take_profit",
                   getattr(m, "DEAL_REASON_SO", 6): "stop_out"}
        t.exit_reason = reasons.get(last.reason, "closed")
        t.status, t.closed_at = "closed", utcnow()
        self.db.event("trade_close", f"#{t.mt5_ticket} closed {t.exit_reason} P/L {t.profit:.2f}", {"ticket": t.mt5_ticket})

    # ------------------------------------------------------------------ BE / trailing / partial
    def _manage(self, x: dict, spec: SymbolSpec, p: TradingParams, atr: float | None) -> None:
        if not (p.breakeven_enabled or p.trailing_enabled or p.partial_enabled):
            return
        with self.db.session() as s:
            t = s.query(Trade).filter_by(mt5_ticket=x["ticket"], status="open").one_or_none()
            if t is None or not t.initial_risk:
                return
            R, partial_done = t.initial_risk, t.partial_done
        sgn = 1 if x["direction"] == "BUY" else -1
        move = (x["price_current"] - x["price_open"]) * sgn
        if p.partial_enabled and not partial_done and move >= p.partial_trigger_r * R:
            part = math.floor(x["volume"] * p.partial_fraction / spec.volume_step + 1e-9) * spec.volume_step
            if part >= spec.volume_min and x["volume"] - part >= spec.volume_min:
                if self.ex.close_position(x["ticket"], x["symbol"], round(part, 8)):
                    with self.db.session() as s:
                        s.query(Trade).filter_by(mt5_ticket=x["ticket"]).update({"partial_done": True})
                    self.db.event("partial", f"Partial close {part} of #{x['ticket']}")
            else:
                with self.db.session() as s:
                    s.query(Trade).filter_by(mt5_ticket=x["ticket"]).update({"partial_done": True})
        cands = []
        if p.breakeven_enabled and move >= p.breakeven_trigger_r * R:
            cands.append(x["price_open"] + sgn * p.breakeven_offset_r * R)
        if p.trailing_enabled and atr and move >= p.trailing_start_r * R:
            cands.append(x["price_current"] - sgn * p.trailing_atr_mult * atr)
        if not cands or not x["sl"] or not x["tp"]:
            return
        new_sl = max(cands) if sgn == 1 else min(cands)
        new_sl = round(new_sl, spec.digits)
        min_gap = spec.stops_level * spec.point + (spec.point * 2)
        if (new_sl - x["sl"]) * sgn > spec.point and (x["price_current"] - new_sl) * sgn >= min_gap:
            if self.ex.modify_sltp(x["ticket"], x["symbol"], new_sl, x["tp"]):  # only ever tightens, never removes
                self.db.event("sl_move", f"#{x['ticket']} SL -> {new_sl}")
