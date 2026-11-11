"""Python independently validates every AI answer. Anything uncertain => NO TRADE."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import TradingParams
from ..strategy.signals import Candidate
from .groq_client import AIResult


@dataclass
class Verdict:
    approved: bool
    reason: str
    sl: float | None = None   # validated override, or None to keep the engine's value
    tp: float | None = None
    notes: list[str] = field(default_factory=list)


def vet(result: AIResult, cand: Candidate, p: TradingParams) -> Verdict:
    if not result.ok or result.decision is None:
        return Verdict(False, f"AI unavailable/invalid: {result.error}")
    d = result.decision
    if d.decision != "APPROVE":
        return Verdict(False, f"AI decision {d.decision}")
    if d.direction != cand.direction:
        return Verdict(False, f"AI direction {d.direction} != setup {cand.direction}")
    if d.confidence < p.ai_confidence_threshold:
        return Verdict(False, f"AI confidence {d.confidence:.0f} < {p.ai_confidence_threshold:.0f}")
    if d.risk_assessment == "UNACCEPTABLE":
        return Verdict(False, "AI risk assessment UNACCEPTABLE")
    if not (d.entry_valid and d.sl_valid and d.tp_valid):
        return Verdict(False, "AI flagged entry/SL/TP as invalid")

    v = Verdict(True, "approved")
    s = 1 if cand.direction == "BUY" else -1
    sl, tp = cand.stop_loss, cand.take_profit
    adj = d.suggested_adjustments
    if adj.stop_loss is not None:
        dist = (cand.entry - adj.stop_loss) * s
        if 0 < dist and p.sl_min_atr * cand.atr <= dist <= p.sl_max_atr * cand.atr:
            sl, v.sl = adj.stop_loss, adj.stop_loss
        else:
            v.notes.append("ignored AI stop-loss suggestion (wrong side or outside ATR bounds)")
    if adj.take_profit is not None:
        reward, risk = (adj.take_profit - cand.entry) * s, (cand.entry - sl) * s
        if reward > 0 and risk > 0 and reward / risk >= p.min_rr:
            v.tp = adj.take_profit
        else:
            v.notes.append("ignored AI take-profit suggestion (R:R below minimum)")
    return v
