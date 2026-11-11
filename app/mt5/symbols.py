"""Symbol discovery and specification."""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Any, Iterable

_V50 = re.compile(r"volatility\s*50(?!\d)", re.I)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def rank_volatility50(symbols: Iterable[Any]) -> list[Any]:
    """Return plain Volatility 50 Index candidates, best first. Excludes the '(1s)' variant."""
    scored = []
    for s in symbols:
        name, desc, path = _norm(getattr(s, "name", "")), _norm(getattr(s, "description", "")), _norm(getattr(s, "path", ""))
        hay = f"{name} {desc}"
        if not _V50.search(hay) and not _V50.search(name.replace("_", " ")):
            continue
        if "1s" in hay.replace(" ", "") or "(1s)" in hay:
            continue
        score = 0
        if name == "volatility 50 index":
            score += 100
        if "volatility indices" in path or "synthetic" in path:
            score += 10
        if "index" in name:
            score += 5
        score -= len(name) * 0.1
        scored.append((score, s))
    return [s for _, s in sorted(scored, key=lambda t: -t[0])]


@dataclass
class SymbolSpec:
    name: str
    description: str
    digits: int
    point: float
    tick_size: float
    tick_value: float
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level: int
    freeze_level: int
    trade_mode: int
    execution_mode: int
    filling_mode: int
    visible: bool
    tradeable: bool
    currency_profit: str

    def to_dict(self) -> dict:
        return asdict(self)


def spec_from_info(info: Any, full_trade_mode: int = 4) -> SymbolSpec:
    return SymbolSpec(
        name=info.name, description=getattr(info, "description", ""), digits=int(info.digits), point=float(info.point),
        tick_size=float(getattr(info, "trade_tick_size", 0) or info.point),
        tick_value=float(getattr(info, "trade_tick_value", 0) or 0),
        contract_size=float(getattr(info, "trade_contract_size", 1) or 1),
        volume_min=float(info.volume_min), volume_max=float(info.volume_max), volume_step=float(info.volume_step),
        stops_level=int(getattr(info, "trade_stops_level", 0)), freeze_level=int(getattr(info, "trade_freeze_level", 0)),
        trade_mode=int(info.trade_mode), execution_mode=int(getattr(info, "trade_exemode", 0)),
        filling_mode=int(getattr(info, "filling_mode", 0)), visible=bool(info.visible),
        tradeable=int(info.trade_mode) == full_trade_mode, currency_profit=getattr(info, "currency_profit", ""),
    )
