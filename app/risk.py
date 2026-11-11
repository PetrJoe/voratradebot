"""All risk controls live here (Python only; the AI cannot touch them)."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import TradingParams
from .db import DailyStatistic, Database, Trade, utcnow

log = logging.getLogger(__name__)


def round_volume_down(vol: float, vmin: float, vmax: float, step: float) -> float | None:
    """Floor to the broker step. None if below the minimum (we never round up into extra risk)."""
    if step <= 0 or vol <= 0 or not math.isfinite(vol):
        return None
    steps = math.floor(vol / step + 1e-9)
    v = steps * step
    decimals = max(0, -int(math.floor(math.log10(step))) + 1) if step < 1 else 0
    v = round(min(v, vmax), decimals)
    return v if v >= vmin - 1e-12 else None


def calc_volume(risk_money: float, loss_per_lot: float, vmin: float, vmax: float, step: float) -> tuple[float | None, str]:
    """loss_per_lot: money lost per 1.0 lot if the stop is hit (positive number)."""
    if not (loss_per_lot > 0 and math.isfinite(loss_per_lot)):
        return None, "invalid loss per lot"
    if risk_money <= 0:
        return None, "no risk budget"
    v = round_volume_down(risk_money / loss_per_lot, vmin, vmax, step)
    if v is None:
        return None, f"volume for {risk_money:.2f} risk is below broker minimum {vmin}"
    return v, "ok"


def trading_date(now_utc: datetime, tz: str, day_start_hour: int) -> str:
    local = now_utc.astimezone(ZoneInfo(tz)) - timedelta(hours=day_start_hour)
    return local.date().isoformat()


@dataclass
class RiskDecision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)


class RiskManager:
    def __init__(self, db: Database):
        self.db = db
        self.emergency_stop = False

    # ---------------- trading day baseline ----------------
    def today(self, p: TradingParams, now: datetime | None = None) -> str:
        return trading_date(now or datetime.now(timezone.utc), p.trading_timezone, p.trading_day_start_hour)

    def ensure_baseline(self, equity: float, p: TradingParams, now: datetime | None = None) -> dict:
        """Create today's baseline once; restarts reuse the persisted value."""
        d = self.today(p, now)
        with self.db.session() as s:
            row = s.query(DailyStatistic).filter_by(trading_date=d).one_or_none()
            if row is None:
                row = DailyStatistic(trading_date=d, starting_equity=equity)
                s.add(row)
                s.flush()
                log.info("New trading day %s baseline equity %.2f", d, equity)
            return {"date": d, "starting_equity": row.starting_equity, "locked": row.locked, "lock_reason": row.lock_reason}

    def daily_pnl(self, equity: float, baseline: float) -> float:
        return equity - baseline

    def update_lock(self, equity: float, p: TradingParams, now: datetime | None = None) -> dict:
        info = self.ensure_baseline(equity, p, now)
        base = info["starting_equity"]
        pnl = equity - base
        limit = base * p.max_daily_loss_pct / 100
        with self.db.session() as s:
            row = s.query(DailyStatistic).filter_by(trading_date=info["date"]).one()
            row.ending_equity, row.daily_profit_loss = equity, pnl
            if not row.locked and pnl <= -limit:
                row.locked, row.lock_reason = True, f"daily loss limit {p.max_daily_loss_pct}% reached"
                self.db.event("risk_lock", row.lock_reason, {"equity": equity, "baseline": base})
                log.warning("RISK LOCK: %s", row.lock_reason)
            info.update(locked=row.locked, lock_reason=row.lock_reason)
        info.update(pnl=pnl, limit=limit, remaining=max(0.0, limit + pnl))
        return info

    def refresh_trade_counts(self, p: TradingParams, now: datetime | None = None) -> None:
        d = self.today(p, now)
        with self.db.session() as s:
            row = s.query(DailyStatistic).filter_by(trading_date=d).one_or_none()
            if not row:
                return
            trades = [t for t in s.query(Trade).all() if self._trade_date(t.opened_at, p) == d]
            row.total_trades = len(trades)
            closed = [t for t in trades if t.status == "closed"]
            row.winning_trades = sum(1 for t in closed if (t.profit or 0) > 0)
            row.losing_trades = sum(1 for t in closed if (t.profit or 0) < 0)

    @staticmethod
    def _trade_date(opened_at: datetime, p: TradingParams) -> str:
        return trading_date(opened_at.replace(tzinfo=timezone.utc), p.trading_timezone, p.trading_day_start_hour)

    # ---------------- streaks / cooldowns ----------------
    def loss_streak(self) -> tuple[int, datetime | None]:
        reset = self.db.kv_get("risk.streak_reset_at")
        reset_dt = datetime.fromisoformat(reset) if reset else None
        with self.db.session() as s:
            q = s.query(Trade).filter(Trade.status == "closed").order_by(Trade.closed_at.desc()).limit(50)
            streak, last_loss = 0, None
            for t in q:
                if reset_dt and t.closed_at and t.closed_at <= reset_dt:
                    break
                if (t.profit or 0) < 0:
                    streak += 1
                    last_loss = last_loss or t.closed_at
                else:
                    break
        return streak, last_loss

    def last_trade_open(self) -> datetime | None:
        with self.db.session() as s:
            t = s.query(Trade).order_by(Trade.opened_at.desc()).first()
            return t.opened_at if t else None

    def trades_today(self, p: TradingParams, now: datetime | None = None) -> int:
        d = self.today(p, now)
        with self.db.session() as s:
            return sum(1 for t in s.query(Trade).filter(Trade.opened_at >= utcnow() - timedelta(days=2)).all()
                       if self._trade_date(t.opened_at, p) == d)

    # ---------------- main gate ----------------
    def pre_trade_check(self, p: TradingParams, *, equity: float | None, open_positions: int,
                        spread: float | None, atr: float | None, now: datetime | None = None) -> RiskDecision:
        now = now or datetime.now(timezone.utc)
        why: list[str] = []
        if self.emergency_stop:
            why.append("emergency stop is active")
        if equity is None or equity <= 0:
            why.append("account equity unavailable")
            return RiskDecision(False, why)
        info = self.update_lock(equity, p, now)
        if info["locked"]:
            why.append(info["lock_reason"] or "daily loss lock")
        if open_positions >= p.max_open_positions:
            why.append(f"open positions {open_positions} >= max {p.max_open_positions}")
        if self.trades_today(p, now) >= p.max_trades_per_day:
            why.append(f"max trades per day ({p.max_trades_per_day}) reached")
        if spread is None or atr is None or atr <= 0:
            why.append("spread/ATR unavailable")
        elif spread > p.max_spread_atr_fraction * atr:
            why.append(f"spread {spread:.5g} > {p.max_spread_atr_fraction:.2f} x ATR")
        last_open = self.last_trade_open()
        if last_open and now.replace(tzinfo=None) - last_open < timedelta(minutes=p.cooldown_minutes_after_trade):
            why.append("cooldown after last trade")
        streak, last_loss = self.loss_streak()
        if streak >= p.max_consecutive_losses and last_loss:
            wait = timedelta(minutes=p.cooldown_minutes_after_loss_streak)
            if now.replace(tzinfo=None) - last_loss < wait:
                why.append(f"{streak} consecutive losses: cooling down")
            else:  # cooldown served -> start a fresh streak count
                self.db.kv_set("risk.streak_reset_at", now.replace(tzinfo=None).isoformat())
                self.db.event("streak_reset", f"loss streak {streak} cooldown served")
        if p.session_enabled and not self._in_session(p, now):
            why.append("outside trading session")
        return RiskDecision(not why, why)

    @staticmethod
    def _in_session(p: TradingParams, now: datetime) -> bool:
        t = now.astimezone(ZoneInfo(p.trading_timezone)).strftime("%H:%M")
        a, b = p.session_start, p.session_end
        return a <= t <= b if a <= b else (t >= a or t <= b)

    def lock_state(self, p: TradingParams) -> dict:
        d = self.today(p)
        with self.db.session() as s:
            row = s.query(DailyStatistic).filter_by(trading_date=d).one_or_none()
            return {"locked": bool(row and row.locked), "reason": row.lock_reason if row else None}
