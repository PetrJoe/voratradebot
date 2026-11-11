"""Read-only setup check: connects to MT5 and Groq, places NO orders."""
import asyncio

from app.ai.groq_client import GroqClient
from app.config import EnvSettings
from app.mt5.client import MT5Client


def main() -> None:
    env = EnvSettings()
    print(f"DEMO_ONLY={env.demo_only}  AUTO_TRADING={env.auto_trading}  model={env.groq_model}")
    c = MT5Client(env)
    print("MT5 terminal:", c.find_terminal() or "NOT FOUND (set MT5_PATH in .env)")
    st = c.connect()
    print(f"MT5 connected={st.connected} demo={st.demo} account={st.login} server={st.server} | {st.message}")
    if st.connected:
        a = c.account()
        print(f"Balance {a['balance']} {a['currency']}, equity {a['equity']}, leverage 1:{a['leverage']}")
        print("Algo Trading enabled in terminal:", st.algo_trading_terminal, "| allowed on account:", st.trade_allowed_account)
        name = c.discover_symbol()
        print("Volatility 50 symbol:", name)
        if name:
            spec = c.symbol_spec(name)
            print(f"  digits={spec.digits} point={spec.point} lot min/max/step={spec.volume_min}/{spec.volume_max}/{spec.volume_step} "
                  f"stops_level={spec.stops_level} filling_flags={spec.filling_mode} tradeable={spec.tradeable}")
            r = c.rates(name, "M5", 300)
            print("  M5 candles fetched:", None if r is None else len(r))
        c.shutdown()
    g = GroqClient(env)
    res = asyncio.run(g.check(env.groq_model))
    print(f"Groq ok={res['ok']} ({res['message']})")
    if not res["ok"] and res.get("models"):
        print("  models available to your key:", ", ".join(res["models"][:25]))


if __name__ == "__main__":
    main()
