"""Environment settings (secrets, never stored in DB) and runtime-editable trading parameters."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

# Timeframes the *strategy* may run on. Add "M1","M15","M30","H1" here when ready.
ENABLED_TIMEFRAMES = ("M5",)
# Timeframes MT5 can serve (used by chart and backtests).
TIMEFRAME_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600}


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"), env_file_encoding="utf-8", extra="ignore"
    )
    mt5_path: str = ""
    mt5_login: int | None = None
    mt5_password: SecretStr = SecretStr("")
    mt5_server: str = ""
    groq_api_key: SecretStr = SecretStr("")
    groq_model: str = "openai/gpt-oss-120b"
    groq_reasoning_effort: str = "medium"
    groq_timeout_seconds: float = 30.0
    demo_only: bool = True
    auto_trading: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    database_url: str = f"sqlite:///{(BASE_DIR / 'data' / 'vora.db').as_posix()}"
    log_level: str = "INFO"
    log_dir: str = str(BASE_DIR / "logs")
    poll_seconds: float = 3.0
    magic_number: int = 50505050

    @field_validator("mt5_login", mode="before")
    @classmethod
    def _blank_login(cls, v: Any) -> Any:
        return None if v in ("", None) else v

    def secrets(self) -> list[str]:
        vals = [self.mt5_password.get_secret_value(), self.groq_api_key.get_secret_value()]
        return [v for v in vals if v and len(v) >= 4]


def F(default: Any, label: str, group: str, *, ge=None, le=None, danger: str | None = None, desc: str = ""):
    """Field helper. danger='up' => raising the value increases risk; 'down' => lowering does."""
    return Field(default, ge=ge, le=le,
                 json_schema_extra={"label": label, "group": group, "danger": danger, "desc": desc})


class TradingParams(BaseModel):
    """Everything editable at runtime. Hard bounds here are the 'risk controls cannot be removed' guarantee."""
    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    # Instrument
    symbol: str = F("", "Instrument", "Instrument", desc="Blank = auto-discover Volatility 50")
    timeframe: str = F("M5", "Timeframe", "Instrument")

    # Risk
    risk_per_trade_pct: float = F(0.5, "Risk per trade (% equity)", "Risk", ge=0.05, le=2.0, danger="up")
    max_daily_loss_pct: float = F(2.0, "Daily loss limit (%)", "Risk", ge=0.25, le=10.0, danger="up")
    max_open_positions: int = F(1, "Max open positions", "Risk", ge=1, le=3, danger="up")
    max_trades_per_day: int = F(10, "Max trades per day", "Risk", ge=1, le=50, danger="up")
    max_consecutive_losses: int = F(3, "Max consecutive losses", "Risk", ge=1, le=10, danger="up")
    min_rr: float = F(2.0, "Min risk/reward", "Risk", ge=1.0, le=10.0, danger="down")
    max_spread_atr_fraction: float = F(0.15, "Max spread (fraction of ATR)", "Risk", ge=0.01, le=0.5, danger="up")
    max_slippage_points: int = F(50, "Max slippage (points)", "Risk", ge=0, le=1000)
    cooldown_minutes_after_trade: int = F(10, "Cooldown after trade (min)", "Risk", ge=0, le=1440)
    cooldown_minutes_after_loss_streak: int = F(60, "Cooldown after loss streak (min)", "Risk", ge=1, le=1440)
    entry_drift_atr: float = F(0.5, "Max price drift before entry (ATR)", "Risk", ge=0.05, le=2.0, danger="up")

    # Strategy
    min_score: float = F(70, "Min strategy score", "Strategy", ge=40, le=100, danger="down")
    adx_min: float = F(20, "ADX minimum", "Strategy", ge=10, le=50)
    rsi_buy_min: float = F(52, "RSI buy min", "Strategy", ge=40, le=70)
    rsi_buy_max: float = F(72, "RSI buy max", "Strategy", ge=55, le=90)
    rsi_sell_min: float = F(28, "RSI sell min", "Strategy", ge=10, le=45)
    rsi_sell_max: float = F(48, "RSI sell max", "Strategy", ge=30, le=60)
    atr_sl_mult: float = F(1.5, "ATR stop multiple", "Strategy", ge=0.5, le=5.0)
    sl_min_atr: float = F(0.8, "Min stop distance (ATR)", "Strategy", ge=0.2, le=3.0)
    sl_max_atr: float = F(3.0, "Max stop distance (ATR)", "Strategy", ge=1.0, le=8.0)
    atr_pct_min: float = F(0.03, "Min ATR (% of price)", "Strategy", ge=0.0, le=5.0)
    atr_pct_max: float = F(0.6, "Max ATR (% of price)", "Strategy", ge=0.05, le=10.0)
    require_full_ema_alignment: bool = F(True, "Require EMA 9/21/50 alignment", "Strategy")
    require_adx: bool = F(True, "Require ADX trend strength", "Strategy")
    swing_strength: int = F(3, "Swing strength (bars each side)", "Strategy", ge=2, le=10)
    sr_lookback: int = F(200, "Support/resistance lookback (bars)", "Strategy", ge=50, le=500)
    w_ema: float = F(20, "Weight: EMA trend", "Scoring", ge=0, le=50)
    w_rsi: float = F(10, "Weight: RSI", "Scoring", ge=0, le=50)
    w_adx: float = F(10, "Weight: ADX", "Scoring", ge=0, le=50)
    w_candle: float = F(20, "Weight: candlestick", "Scoring", ge=0, le=50)
    w_sr: float = F(15, "Weight: support/resistance", "Scoring", ge=0, le=50)
    w_atr: float = F(10, "Weight: ATR suitability", "Scoring", ge=0, le=50)
    w_macd: float = F(10, "Weight: MACD", "Scoring", ge=0, le=50)
    w_fib: float = F(5, "Weight: Fibonacci", "Scoring", ge=0, le=50)
    correlated_cap: float = F(40, "Cap for EMA+RSI+ADX+MACD points", "Scoring", ge=10, le=100,
                              desc="These indicators overlap; combined points are capped, then the score is rescaled to 100")

    # AI
    ai_confidence_threshold: float = F(70, "AI confidence threshold", "AI", ge=50, le=100, danger="down")
    groq_model: str = F("", "Groq model", "AI", desc="Blank = GROQ_MODEL from .env")

    # Session / trading day
    session_enabled: bool = F(False, "Restrict to trading session", "Session")
    session_start: str = F("00:00", "Session start (HH:MM, trading tz)", "Session")
    session_end: str = F("23:59", "Session end (HH:MM, trading tz)", "Session")
    trading_timezone: str = F("UTC", "Trading timezone", "Session")
    trading_day_start_hour: int = F(0, "Trading day starts at hour", "Session", ge=0, le=23)

    # Position management
    breakeven_enabled: bool = F(False, "Break-even", "Position management")
    breakeven_trigger_r: float = F(1.0, "Break-even trigger (R)", "Position management", ge=0.3, le=5.0)
    breakeven_offset_r: float = F(0.1, "Break-even offset (R)", "Position management", ge=0.0, le=1.0)
    trailing_enabled: bool = F(False, "Trailing stop", "Position management")
    trailing_start_r: float = F(1.5, "Trailing start (R)", "Position management", ge=0.5, le=10.0)
    trailing_atr_mult: float = F(2.0, "Trailing distance (ATR)", "Position management", ge=0.5, le=6.0)
    partial_enabled: bool = F(False, "Partial profit-taking", "Position management")
    partial_trigger_r: float = F(1.0, "Partial trigger (R)", "Position management", ge=0.3, le=5.0)
    partial_fraction: float = F(0.5, "Partial close fraction", "Position management", ge=0.1, le=0.9)

    @field_validator("timeframe")
    @classmethod
    def _tf(cls, v: str) -> str:
        if v not in ENABLED_TIMEFRAMES:
            raise ValueError(f"timeframe must be one of {ENABLED_TIMEFRAMES}")
        return v

    @field_validator("trading_timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        ZoneInfo(v)
        return v

    @field_validator("session_start", "session_end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        h, m = v.split(":")
        if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
            raise ValueError("use HH:MM")
        return f"{int(h):02d}:{int(m):02d}"

    @classmethod
    def schema_rows(cls) -> list[dict]:
        rows = []
        for name, f in cls.model_fields.items():
            extra = f.json_schema_extra or {}
            lo = hi = None
            for m in f.metadata:
                lo = getattr(m, "ge", lo)
                hi = getattr(m, "le", hi)
            rows.append({
                "key": name, "label": extra.get("label", name), "group": extra.get("group", ""),
                "danger": extra.get("danger"), "desc": extra.get("desc", ""),
                "type": "bool" if f.annotation is bool else "int" if f.annotation is int
                        else "float" if f.annotation is float else "str",
                "min": lo, "max": hi,
            })
        return rows


def risky_changes(old: TradingParams, new: TradingParams) -> list[str]:
    """Names of settings whose change makes the bot take more risk or accept weaker setups."""
    out = []
    for name, f in TradingParams.model_fields.items():
        danger = (f.json_schema_extra or {}).get("danger")
        if not danger:
            continue
        a, b = getattr(old, name), getattr(new, name)
        if (danger == "up" and b > a) or (danger == "down" and b < a):
            out.append(name)
    return out
