"""SQLite persistence (SQLAlchemy 2.0). Schema is created on start and new columns are added automatically."""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # stored as naive UTC


class Base(DeclarativeBase):
    pass


class Setting(Base):
    __tablename__ = "settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AccountSnapshot(Base):
    __tablename__ = "account_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    balance: Mapped[float] = mapped_column(Float)
    equity: Mapped[float] = mapped_column(Float)
    margin: Mapped[float] = mapped_column(Float)
    free_margin: Mapped[float] = mapped_column(Float)
    daily_baseline: Mapped[float | None] = mapped_column(Float, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Signal(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(64))
    timeframe: Mapped[str] = mapped_column(String(8))
    direction: Mapped[str] = mapped_column(String(8))
    score: Mapped[float] = mapped_column(Float)
    indicator_snapshot: Mapped[str] = mapped_column(Text, default="{}")
    price_action_snapshot: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(40), default="candidate")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    bar_time: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    executed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class AIAnalysis(Base):
    __tablename__ = "ai_analyses"
    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True, index=True)
    model: Mapped[str] = mapped_column(String(100))
    decision: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings: Mapped[str] = mapped_column(Text, default="[]")
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    direction: Mapped[str | None] = mapped_column(String(8), nullable=True)
    risk_assessment: Mapped[str | None] = mapped_column(String(20), nullable=True)
    accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Trade(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(primary_key=True)
    mt5_ticket: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    symbol: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(8))
    volume: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    strategy_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_decision: Mapped[str | None] = mapped_column(Text, nullable=True)
    signal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    initial_risk: Mapped[float | None] = mapped_column(Float, nullable=True)  # price distance entry->SL
    mfe: Mapped[float] = mapped_column(Float, default=0.0)  # best favourable move (price units)
    mae: Mapped[float] = mapped_column(Float, default=0.0)  # worst adverse move (price units)
    exit_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    partial_done: Mapped[bool] = mapped_column(Boolean, default=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DailyStatistic(Base):
    __tablename__ = "daily_statistics"
    id: Mapped[int] = mapped_column(primary_key=True)
    trading_date: Mapped[str] = mapped_column(String(10), unique=True, index=True)
    starting_equity: Mapped[float] = mapped_column(Float)
    ending_equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    daily_profit_loss: Mapped[float] = mapped_column(Float, default=0.0)
    total_trades: Mapped[int] = mapped_column(Integer, default=0)
    winning_trades: Mapped[int] = mapped_column(Integer, default=0)
    losing_trades: Mapped[int] = mapped_column(Integer, default=0)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    lock_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)


class ApplicationEvent(Base):
    __tablename__ = "application_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(40), index=True)
    message: Mapped[str] = mapped_column(Text)
    meta: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class ExecutionAttempt(Base):
    __tablename__ = "execution_attempts"
    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    symbol: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(8))
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    stage: Mapped[str] = mapped_column(String(30))
    retcode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Database:
    def __init__(self, url: str):
        connect_args = {"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, connect_args=connect_args, future=True)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def _pragma(dbapi_conn, _):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        self._Session = sessionmaker(self.engine, expire_on_commit=False, future=True)
        self.init_schema()

    def init_schema(self) -> None:
        Base.metadata.create_all(self.engine)
        insp = inspect(self.engine)
        with self.engine.begin() as conn:
            for table in Base.metadata.sorted_tables:
                existing = {c["name"] for c in insp.get_columns(table.name)}
                for col in table.columns:
                    if col.name not in existing:
                        ctype = col.type.compile(self.engine.dialect)
                        conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ctype}'))
                        log.info("Added column %s.%s", table.name, col.name)

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self._Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # --- key/value settings -------------------------------------------------
    def kv_get(self, key: str, default: Any = None) -> Any:
        with self.session() as s:
            row = s.query(Setting).filter_by(key=key).one_or_none()
            return json.loads(row.value) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        with self.session() as s:
            row = s.query(Setting).filter_by(key=key).one_or_none()
            if row:
                row.value = json.dumps(value)
            else:
                s.add(Setting(key=key, value=json.dumps(value)))

    def kv_all(self, prefix: str = "") -> dict[str, Any]:
        with self.session() as s:
            return {r.key: json.loads(r.value) for r in s.query(Setting).filter(Setting.key.like(prefix + "%"))}

    # --- events -------------------------------------------------------------
    def event(self, event_type: str, message: str, meta: dict | None = None) -> None:
        try:
            with self.session() as s:
                s.add(ApplicationEvent(event_type=event_type, message=message[:2000],
                                       meta=json.dumps(meta, default=str) if meta else None))
        except Exception:
            log.exception("Could not persist event")


def row_dict(obj: Any) -> dict:
    from sqlalchemy import inspect as sa_inspect
    out = {}
    for attr in sa_inspect(obj).mapper.column_attrs:  # attribute names (e.g. `meta` for the `metadata` column)
        v = getattr(obj, attr.key)
        out[attr.key] = v.isoformat() + "Z" if isinstance(v, datetime) else v
    return out
