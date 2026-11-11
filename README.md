# VORA MT5 AI Trader

Local Windows 11 app: watches **Volatility 50 Index (M5)** on your **Deriv MT5 demo** account, finds setups with deterministic
technical analysis, asks **Groq** to review qualified ones, and (only if you enable it) places orders with strict Python-side risk control.

> **Read first.** This software can place trades. Run it on a demo account only until you have watched it for a good while.
> A backtest or a green test suite is not evidence of profitability. Trading synthetic indices carries a high risk of loss.
> I could not run a real MT5 terminal while building this (the MetaTrader5 package is Windows-only), so the live connection and
> order path were tested against a faithful fake only. **Your first real run is the real test: use `check_setup.bat` first.**

## Setup (Windows 11)
1. Install **Python 3.12** (python.org, tick "Add to PATH") and the **Deriv MT5** terminal. Log in to your demo account once, and in
   MT5 enable **Tools > Options > Expert Advisors > Allow algorithmic trading** (also the "Algo Trading" toolbar button).
2. Double-click `install.bat`. It creates `.venv`, installs packages and opens `.env` for you.
3. Fill `.env` (never paste secrets anywhere else):
   `MT5_PATH` (blank = auto-detect), `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER` (exact server name shown in MT5), `GROQ_API_KEY`.
4. Run `check_setup.bat`: it connects, confirms demo status, finds the Volatility 50 symbol, prints lot/stop specs, checks Groq. **No orders.**
5. Run `start.bat`; the dashboard opens at http://127.0.0.1:8000. The bot starts **stopped** and **AUTO_TRADING is off**.
6. Press **Start bot**. With auto trading off it records signals and Groq opinions but sends no orders. Enable **Auto trading** (confirmation required) when satisfied.

## How a trade happens
1. Each time an M5 candle **closes** (once per candle, never repeated, restart-safe) indicators and price action are computed.
2. A weighted confluence score (0-100) is built for BUY and SELL. Hard gates (EMA 9/21/50 alignment, ADX, sane ATR/stop size, no opposing
   S/R zone within 1R) must pass. Below `min_score` => **no trade** (the default).
3. Risk gate (equity, daily loss lock, open positions, trades/day, spread, cooldowns, loss streak, session) runs *before* Groq is called.
4. Groq reviews a structured snapshot and must answer strict JSON. Python validates schema, direction match, confidence threshold, flags,
   and only accepts a suggested SL/TP if it is on the right side, within ATR bounds and keeps R:R >= minimum. Any error/timeout/invalid JSON => no trade.
5. Executor re-checks everything with a fresh tick (demo, auto-trading, lock, emergency stop, quote freshness, spread, price drift, R:R, stops level),
   sizes the lot from real contract data (`order_calc_profit`, fallback tick value/size), floors to the broker volume step, checks margin,
   maps filling flags to the right `ORDER_FILLING_*`, runs `order_check`, then `order_send`.
6. Requote => one retry at the fresh price. Timeout/no-connection => it **inspects positions** instead of resending. After a fill the live
   position must show SL **and** TP; if they cannot be applied the position is closed immediately.

## Risk defaults (all editable inside hard bounds; loosening asks for confirmation)
0.5% risk/trade, 2% daily loss lock (persisted baseline, survives restarts), 1 open position, 3 consecutive losses then cooldown, min R:R 2,
mandatory SL and TP (not a setting), no martingale/grid/averaging/stacking (not implemented). Emergency stop blocks new orders instantly;
it does **not** close open positions (they keep their SL/TP).

## Design notes
- **Indicators** are implemented directly in pandas (EMA, Wilder RSI/ATR/ADX, MACD, Bollinger) instead of `pandas-ta`, which often breaks
  with new numpy/Python versions. They are unit-tested for causality (no future data).
- **Correlated evidence:** EMA, RSI, MACD and ADX mostly restate trend. Their combined points are capped (`correlated_cap`, default 40) and the
  score is rescaled to 100, so four trend signals cannot masquerade as four independent confirmations. Fibonacci is a 5-point supporting factor only.
- **Groq model:** default `openai/gpt-oss-120b`, set in `.env`. Request parameters adapt to the model family (reasoning effort and strict schema for
  gpt-oss, `reasoning_format=hidden` for Qwen3, none for others). The app calls Groq `/models` at runtime and shows "Groq unavailable" in the header if
  your key lacks the model. Check https://console.groq.com/docs/models if Groq retires it.
- **APScheduler** is not used: a single asyncio loop with worker threads is simpler and sufficient.
- **Local API security:** binds to 127.0.0.1; state-changing calls require a custom header and the WebSocket is same-origin only. Do not expose it with `HOST=0.0.0.0`.
- The dashboard loads Tailwind, Lightweight Charts and fonts from CDNs, so it needs internet.
- Timeframes: M5 only is enabled for the strategy (`ENABLED_TIMEFRAMES` in `app/config.py`); the chart can show M1-H1.

## Backtesting
Dashboard > Backtest. Uses MT5 history and the same strategy code. Decisions use closed candles only and fill at the next open with per-bar
spread; SL wins when SL and TP are both hit in one bar. Results are labelled simulated. Groq is not simulated.
Tick value comes from the symbol's *current* spec, so treat money figures as approximate.

## Layout
`app/mt5` connection + symbols, `app/strategy` indicators / price action / levels / scoring, `app/risk.py`, `app/execution.py`, `app/positions.py`,
`app/ai`, `app/engine.py`, `app/backtest.py`, `app/api.py`, `app/static` dashboard, `tests/` (46 tests, fake MT5 included), `data/vora.db` SQLite, `logs/`.

## API
`GET /api/{health,status,account,market,candles,positions,trades,signals,ai/analyses,events,executions,statistics,settings,backtest/latest}`,
`PUT /api/settings`, `POST /api/bot/{start,pause,resume,stop,emergency-stop,auto-trading}`, `POST /api/symbol/validate`, `POST /api/backtest`, `WS /ws`.
POST/PUT need header `X-Requested-With: vora`.

## First live-run checklist
- `check_setup.bat` shows demo=True, the right symbol, and sensible lot min/step/stops level.
- Let it run with auto trading **off** for a day; read Signal history and Groq reasoning.
- Then enable auto trading on demo, and verify in MT5 that every position has SL and TP and the lot size matches ~0.5% risk.
- Check `logs/vora.log` and the Event log tab. Broker-specific behaviour (filling modes, stops level, tick value) is where surprises appear.
