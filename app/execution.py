"""Order execution. Every path is guarded; every attempt is recorded; uncertain outcomes are reconciled, not retried."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from .config import EnvSettings, TradingParams
from .db import Database, ExecutionAttempt, Trade, utcnow
from .mt5.client import MT5Client
from .mt5.symbols import SymbolSpec
from .risk import RiskManager, calc_volume
from .state import RuntimeFlags
from .strategy.signals import Candidate

log = logging.getLogger(__name__)

RC_OK = {10008, 10009, 10010}          # placed / done / done-partial
RC_REQUOTE = {10004, 10020, 10021}     # requote / price changed / price off  -> definitely not executed
RC_UNCERTAIN = {10012, 10031, 10024}   # timeout / no connection / too many requests -> must reconcile
RC_INVALID_FILL = 10030
RC_DONE_PARTIAL = 10010


@dataclass
class ExecResult:
    ok: bool
    status: str
    message: str
    ticket: int | None = None
    volume: float | None = None
    price: float | None = None


class Executor:
    def __init__(self, env: EnvSettings, client: MT5Client, risk: RiskManager, db: Database,
                 flags: RuntimeFlags, get_params: Callable[[], TradingParams]):
        self.env, self.c, self.risk, self.db, self.flags, self.get_params = env, client, risk, db, flags, get_params
        self.last_order_bar: int | None = None
        self.blocked_until = 0.0  # set after an uncertain outcome until reconciled

    # ------------------------------------------------------------------ helpers
    @property
    def m(self):
        return self.c.m

    def _log(self, sig: int | None, cand: Candidate | None, stage: str, status: str, detail: str = "", *,
             retcode: int | None = None, volume=None, price=None, sl=None, tp=None, symbol="", direction="") -> None:
        try:
            with self.db.session() as s:
                s.add(ExecutionAttempt(signal_id=sig, symbol=symbol, direction=direction or (cand.direction if cand else ""),
                                       volume=volume, price=price, stop_loss=sl, take_profit=tp, stage=stage,
                                       retcode=retcode, status=status, detail=detail[:1000]))
        except Exception:
            log.exception("could not record execution attempt")
        log.info("EXEC %s %s %s %s", stage, status, retcode if retcode is not None else "", detail[:200])

    def filling_candidates(self, spec: SymbolSpec) -> list[int]:
        """Map the symbol's filling *flags* (1=FOK, 2=IOC) to ORDER_FILLING_* enums. RETURN only if not Market execution."""
        m, out = self.m, []
        if spec.filling_mode & 2:
            out.append(m.ORDER_FILLING_IOC)
        if spec.filling_mode & 1:
            out.append(m.ORDER_FILLING_FOK)
        if spec.execution_mode != m.SYMBOL_TRADE_EXECUTION_MARKET:
            out.append(m.ORDER_FILLING_RETURN)
        return out or [m.ORDER_FILLING_IOC, m.ORDER_FILLING_FOK, m.ORDER_FILLING_RETURN]

    def _our_positions(self, symbol: str) -> list[dict]:
        return [p for p in self.c.positions(symbol) if p["magic"] == self.env.magic_number]

    # ------------------------------------------------------------------ pre-trade validation + sizing
    def build_order(self, cand: Candidate, spec: SymbolSpec, p: TradingParams, tick: dict, account: dict,
                    sl_override=None, tp_override=None) -> tuple[dict | None, str]:
        m, buy = self.m, cand.direction == "BUY"
        s = 1 if buy else -1
        entry = tick["ask"] if buy else tick["bid"]
        if abs(entry - cand.entry) > p.entry_drift_atr * cand.atr:
            return None, f"price moved {abs(entry - cand.entry):.5g} (> {p.entry_drift_atr} ATR) since signal"
        sl = sl_override if sl_override is not None else cand.stop_loss
        risk_dist = (entry - sl) * s
        if risk_dist <= 0:
            return None, "stop loss on wrong side of entry"
        tp = tp_override if tp_override is not None else entry + s * max(p.min_rr, 2.0) * risk_dist
        rr = ((tp - entry) * s) / risk_dist
        if (tp - entry) * s <= 0 or rr < p.min_rr - 1e-9:
            return None, f"risk/reward {rr:.2f} below minimum {p.min_rr}"
        spread = tick["ask"] - tick["bid"]
        min_dist = spec.stops_level * spec.point + spread
        if risk_dist < min_dist or (tp - entry) * s < min_dist:
            return None, "stop/target closer than broker stops level"
        sl, tp = round(sl, spec.digits), round(tp, spec.digits)
        otype = m.ORDER_TYPE_BUY if buy else m.ORDER_TYPE_SELL
        with self.c.lock:
            pl = m.order_calc_profit(otype, spec.name, 1.0, entry, sl)
        if pl is not None and pl != 0:
            loss_per_lot = abs(pl)
        elif spec.tick_size > 0 and spec.tick_value > 0:  # fallback to contract specs
            loss_per_lot = risk_dist / spec.tick_size * spec.tick_value
        else:
            return None, "cannot determine loss per lot"
        risk_money = account["equity"] * p.risk_per_trade_pct / 100
        vol, why = calc_volume(risk_money, loss_per_lot, spec.volume_min, spec.volume_max, spec.volume_step)
        if vol is None:
            return None, why
        with self.c.lock:
            margin = m.order_calc_margin(otype, spec.name, vol, entry)
        if margin is None or margin > 0.9 * account["free_margin"]:
            return None, f"insufficient free margin (need {margin}, free {account['free_margin']})"
        return {"otype": otype, "price": entry, "sl": sl, "tp": tp, "volume": vol, "risk_money": vol * loss_per_lot,
                "risk_dist": risk_dist}, "ok"

    # ------------------------------------------------------------------ main entry
    def execute(self, cand: Candidate, symbol: str, spec: SymbolSpec, signal_id: int | None, *,
                sl_override=None, tp_override=None, ai_summary: str = "") -> ExecResult:
        p, m = self.get_params(), self.m
        lg = lambda stage, status, detail="", **k: self._log(signal_id, cand, stage, status, detail, symbol=symbol, **k)
        with self.c.lock:
            def reject(msg: str, stage="validation") -> ExecResult:
                lg(stage, "rejected", msg)
                return ExecResult(False, "rejected", msg)

            if not self.flags.auto_trading:
                return reject("AUTO_TRADING is off")
            if self.risk.emergency_stop:
                return reject("emergency stop active")
            if time.time() < self.blocked_until:
                return reject("blocked: previous order outcome unresolved")
            ok, why = self.c.trading_permitted()
            if not ok:
                return reject("; ".join(why))
            if self.last_order_bar == cand.bar_time:
                return reject("an order was already sent for this candle")
            if cand.direction not in ("BUY", "SELL"):
                return reject("invalid direction")
            if not spec.tradeable:
                return reject("symbol not tradeable")
            tick = self.c.tick(symbol)
            acc = self.c.account()
            rates = self.c.rates(symbol, p.timeframe, 3)
            if tick is None or acc is None or rates is None:
                return reject("missing market/account data")
            if not self.c.quote_fresh(tick, int(rates["time"].iloc[-1]), p.timeframe):
                return reject("stale quote")
            ours = self._our_positions(symbol)
            dec = self.risk.pre_trade_check(p, equity=acc["equity"], open_positions=len(ours),
                                            spread=tick["ask"] - tick["bid"], atr=cand.atr)
            if not dec.allowed:
                return reject("risk: " + "; ".join(dec.reasons))
            if any(x["direction"] == cand.direction for x in ours):
                return reject("duplicate position in same direction")
            order, why = self.build_order(cand, spec, p, tick, acc, sl_override, tp_override)
            if order is None:
                return reject(why)

            fills = self.filling_candidates(spec)
            pre = {x["ticket"] for x in self._our_positions(symbol)}
            comment = f"VORA {cand.direction} {cand.score:.0f}"[:30]
            chosen_fill = None
            for fill in fills:
                req = self._request(order, symbol, p, fill, comment)
                chk = m.order_check(req)
                if chk is not None and chk.retcode in (0, 10009):
                    chosen_fill = fill
                    break
                rc = chk.retcode if chk is not None else None
                lg("order_check", "failed", f"filling {fill}: {getattr(chk, 'comment', 'no result')}",
                   retcode=rc, volume=order["volume"], price=order["price"], sl=order["sl"], tp=order["tp"])
                if rc != RC_INVALID_FILL:  # a different failure won't be fixed by changing fill mode
                    return ExecResult(False, "check_failed", f"order_check: {getattr(chk, 'comment', 'failed')} ({rc})")
            if chosen_fill is None:
                return ExecResult(False, "check_failed", "no supported filling mode")

            if self.risk.emergency_stop:  # last-moment re-check
                return reject("emergency stop active")
            result = self._send(order, symbol, p, chosen_fill, comment, lg)
            if result is None:  # requote path exhausted / not executed
                return ExecResult(False, "not_executed", "order not executed (requote/price change)")
            self.last_order_bar = cand.bar_time
            return self._finish(result, cand, order, symbol, spec, signal_id, pre, ai_summary, lg)

    def _request(self, order: dict, symbol: str, p: TradingParams, fill: int, comment: str, *, price=None) -> dict:
        m = self.m
        return {"action": m.TRADE_ACTION_DEAL, "symbol": symbol, "volume": order["volume"], "type": order["otype"],
                "price": price if price is not None else order["price"], "sl": order["sl"], "tp": order["tp"],
                "deviation": int(p.max_slippage_points), "magic": self.env.magic_number, "comment": comment,
                "type_time": m.ORDER_TIME_GTC, "type_filling": fill}

    def _send(self, order, symbol, p, fill, comment, lg):
        """Returns result object, an 'uncertain' marker object, or None when definitely not executed."""
        m = self.m
        pre = {x["ticket"] for x in self._our_positions(symbol)}
        t0 = time.time()
        for attempt in (1, 2):
            req = self._request(order, symbol, p, fill, comment)
            res = m.order_send(req)
            rc = res.retcode if res is not None else None
            lg("order_send", "sent" if rc in RC_OK else "failed", getattr(res, "comment", "no result"), retcode=rc,
               volume=order["volume"], price=req["price"], sl=order["sl"], tp=order["tp"])
            if res is not None and rc in RC_OK:
                return res
            if rc in RC_REQUOTE and attempt == 1:
                tick = self.c.tick(symbol)  # certain not executed -> one retry at the fresh price, same stops
                if tick is None:
                    return None
                order = dict(order, price=tick["ask"] if order["otype"] == m.ORDER_TYPE_BUY else tick["bid"])
                continue
            if res is None or rc in RC_UNCERTAIN:
                return self._reconcile(symbol, pre, t0, lg)
            return None
        return None

    def _reconcile(self, symbol, pre, t0, lg):
        """Uncertain outcome: look at the terminal before doing anything else. Never resend blindly."""
        self.blocked_until = time.time() + 120
        for _ in range(5):
            time.sleep(1)
            new = [x for x in self._our_positions(symbol) if x["ticket"] not in pre]
            if new:
                lg("reconcile", "found", f"position {new[0]['ticket']} exists despite uncertain reply")
                self.blocked_until = 0
                return _Found(new[0]["ticket"])
        lg("reconcile", "unresolved", "no position found; new orders blocked for 120s")
        return None

    def _finish(self, res, cand, order, symbol, spec, signal_id, pre, ai_summary, lg) -> ExecResult:
        ticket = None
        if isinstance(res, _Found):
            ticket = res.ticket
        else:
            deal = getattr(res, "deal", 0)
            if deal:
                with self.c.lock:
                    ds = self.m.history_deals_get(ticket=deal)
                if ds:
                    ticket = ds[0].position_id
            for _ in range(6):
                if ticket:
                    break
                new = [x for x in self._our_positions(symbol) if x["ticket"] not in pre]
                ticket = new[0]["ticket"] if new else None
                if not ticket:
                    time.sleep(0.5)
        if not ticket:
            self.blocked_until = time.time() + 120
            lg("confirm", "unresolved", "order accepted but position not found")
            return ExecResult(False, "unconfirmed", "order accepted but position could not be located")
        pos = next((x for x in self._our_positions(symbol) if x["ticket"] == ticket), None)
        if pos is None:
            return ExecResult(False, "unconfirmed", "position vanished right after opening")
        # Protective stops must be confirmed on the live position
        if not (pos["sl"] and pos["tp"]):
            lg("protect", "missing", "SL/TP not set on position; trying to apply")
            applied = False
            for _ in range(3):
                if self.modify_sltp(ticket, symbol, order["sl"], order["tp"]):
                    applied = True
                    break
                time.sleep(0.5)
            if not applied:
                self.db.event("critical", f"Position {ticket} unprotected; closing it", {"ticket": ticket})
                closed = self.close_position(ticket, symbol)
                lg("protect", "closed_unprotected" if closed else "UNPROTECTED_OPEN",
                   "could not confirm SL/TP -> position closed" if closed else "COULD NOT CLOSE: close it manually in MT5")
                return ExecResult(False, "unprotected", "protective stops could not be confirmed; position closed")
            pos = dict(pos, sl=order["sl"], tp=order["tp"])
        fill_price = pos["price_open"]
        with self.db.session() as s:
            s.add(Trade(mt5_ticket=ticket, symbol=symbol, direction=cand.direction, volume=pos["volume"],
                        entry_price=fill_price, stop_loss=pos["sl"], take_profit=pos["tp"], status="open",
                        strategy_reason=cand.evidence(), ai_decision=ai_summary[:1000], signal_id=signal_id,
                        initial_risk=abs(fill_price - pos["sl"])))
        partial = getattr(res, "retcode", 0) == RC_DONE_PARTIAL
        msg = f"opened #{ticket} {cand.direction} {pos['volume']} @ {fill_price}" + (" (PARTIAL FILL)" if partial else "")
        lg("filled", "ok", msg, volume=pos["volume"], price=fill_price, sl=pos["sl"], tp=pos["tp"])
        self.db.event("trade_open", msg, {"ticket": ticket, "signal_id": signal_id})
        return ExecResult(True, "filled", msg, ticket, pos["volume"], fill_price)

    # ------------------------------------------------------------------ position operations
    def modify_sltp(self, ticket: int, symbol: str, sl: float, tp: float) -> bool:
        m = self.m
        if not sl or not tp:  # never remove protection
            return False
        with self.c.lock:
            res = m.order_send({"action": m.TRADE_ACTION_SLTP, "position": ticket, "symbol": symbol, "sl": sl, "tp": tp,
                                "magic": self.env.magic_number})
        ok = res is not None and res.retcode in (10009, 10025)  # 10025 = no changes
        if not ok:
            log.warning("modify SL/TP failed for %s: %s", ticket, getattr(res, "comment", None))
        return ok

    def close_position(self, ticket: int, symbol: str, volume: float | None = None) -> bool:
        m = self.m
        with self.c.lock:
            pos = next((x for x in self.c.positions(symbol) if x["ticket"] == ticket), None)
            spec = self.c.symbol_spec(symbol)
            tick = self.c.tick(symbol)
            if not pos or not spec or not tick:
                return False
            buy = pos["direction"] == "BUY"
            for fill in self.filling_candidates(spec):
                res = m.order_send({"action": m.TRADE_ACTION_DEAL, "symbol": symbol, "position": ticket,
                                    "volume": volume or pos["volume"],
                                    "type": m.ORDER_TYPE_SELL if buy else m.ORDER_TYPE_BUY,
                                    "price": tick["bid"] if buy else tick["ask"], "deviation": 100,
                                    "magic": self.env.magic_number, "comment": "VORA close",
                                    "type_time": m.ORDER_TIME_GTC, "type_filling": fill})
                if res is not None and res.retcode in (10009, 10010):
                    return True
                if res is None or res.retcode != RC_INVALID_FILL:
                    break
        return False


class _Found:
    def __init__(self, ticket: int):
        self.ticket, self.retcode, self.deal = ticket, 10009, 0
