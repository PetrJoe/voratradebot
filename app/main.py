from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .ai.groq_client import GroqClient
from .api import guarded, router, ws_router
from .config import EnvSettings
from .db import Database
from .engine import Engine, Hub
from .execution import Executor
from .logging_setup import setup_logging
from .mt5.client import MT5Client
from .positions import PositionManager
from .risk import RiskManager
from .state import RuntimeFlags

STATIC = Path(__file__).parent / "static"


def create_app(env: EnvSettings | None = None, mt5_module=None) -> FastAPI:
    env = env or EnvSettings()
    setup_logging(env.log_level, env.log_dir, env.secrets())
    log = logging.getLogger("vora")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(env.database_url)
        client = MT5Client(env, mt5_module) if mt5_module is not None else MT5Client(env)
        flags, hub, risk = RuntimeFlags(), Hub(), RiskManager(db)
        engine = Engine(env, db, client, GroqClient(env), risk, None, None, flags, hub)  # type: ignore[arg-type]
        engine.ex = Executor(env, client, risk, db, flags, lambda: engine.params)
        engine.pm = PositionManager(env, client, engine.ex, db)
        app.state.engine = engine
        db.event("startup", "VORA started", {"demo_only": env.demo_only, "auto_trading": env.auto_trading})
        log.info("VORA MT5 AI Trader started (DEMO_ONLY=%s, AUTO_TRADING=%s, bot is STOPPED until you press Start)",
                 env.demo_only, env.auto_trading)
        await engine.start_loop()
        yield
        await engine.shutdown()
        db.event("shutdown", "VORA stopped")

    app = FastAPI(title="VORA MT5 AI Trader", lifespan=lifespan)
    app.include_router(router)
    app.include_router(guarded)
    app.include_router(ws_router)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    return app


app = create_app()
