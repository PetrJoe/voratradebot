from __future__ import annotations

import enum
from dataclasses import dataclass


class BotState(str, enum.Enum):
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    EMERGENCY = "EMERGENCY_STOP"


@dataclass
class RuntimeFlags:
    auto_trading: bool = False  # starts from AUTO_TRADING in .env; toggled from the dashboard
