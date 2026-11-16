"""CLI.

  python -m kbot discover                 list Kalshi crypto series (find the 1m/5m/15m BTC tickers)
  python -m kbot run [--live]             stream BTC + Kalshi; paper trades unless --live
  python -m kbot report                   per-horizon scoreboard from the database
  python -m kbot sim [--hours 2]          offline end-to-end simulation
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from .config import load_config


async def _discover(cfg):
    from .kalshi import KalshiAuth, KalshiREST
    rest = KalshiREST(cfg.kalshi, KalshiAuth(cfg.kalshi))
    for s in await rest.series("Crypto"):
        if "BTC" in s.get("ticker", "").upper() or "bitcoin" in s.get("title", "").lower():
            mk = await rest.open_markets(s["ticker"])
            print(f"{s['ticker']:<16} freq={s.get('frequency', '?'):<10} open={len(mk):<4} {s.get('title', '')}")


async def _run(cfg, live: bool):
    from . import btc_feed
    from .broker import LiveBroker, PaperBroker
    from .engine import Engine, RealClock
    from .kalshi import KalshiAuth, KalshiREST, KalshiWS, parse_market
    from .tracker import Tracker

    log = logging.getLogger("kbot")
    auth = KalshiAuth(cfg.kalshi)
    if not auth.ok:
        sys.exit("Kalshi API key required (the orderbook websocket is authenticated even for paper mode). "
                 "Set KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH.")
    rest = KalshiREST(cfg.kalshi, auth)
    clock = RealClock(asyncio.get_running_loop())
    tracker = Tracker(cfg.db_path, run="live" if live else "paper")
    engine = Engine(cfg, clock, tracker)
    if live:
        engine.broker = LiveBroker(rest, cfg.trade.fee_rate)
        log.warning("LIVE TRADING on %s; balance: %s", cfg.kalshi.env, await rest.balance())
    else:
        engine.broker = PaperBroker(clock, lambda t: engine.books.get(t), cfg.trade.paper_latency_ms,
                                    cfg.trade.fee_rate)

    def resolver(m, done):
        async def poll():
            for _ in range(60):                              # up to ~10 minutes
                try:
                    r = (await rest.market(m.ticker)).get("result")
                    if r in ("yes", "no"):
                        return done(r)
                except Exception as e:  # noqa: BLE001
                    log.debug("settle poll %s: %s", m.ticker, e)
                await asyncio.sleep(10)
            log.warning("no official result for %s; using feed estimate", m.ticker)
            done(engine.estimate_settlement(m))
        asyncio.get_running_loop().create_task(poll())
    engine.resolver = resolver

    ws = KalshiWS(cfg.kalshi, auth, engine)

    async def refresh_markets():
        warned = set()
        while True:
            for horizon, series in cfg.horizons.items():
                try:
                    raw = await rest.open_markets(series)
                except Exception as e:  # noqa: BLE001
                    log.warning("listing %s failed: %s", series, e)
                    continue
                if not raw and series not in warned:
                    warned.add(series)
                    log.warning("series %s (%s) has no open markets - check the ticker with `discover`",
                                series, horizon)
                tickers = set()
                for d in raw:
                    m = parse_market(d, series, horizon)
                    if m:
                        engine.upsert_market(m)
                        tickers.add(m.ticker)
                await ws.ensure(tickers)
            await asyncio.sleep(cfg.kalshi.market_refresh_s)

    async def periodic_report():
        while True:
            await asyncio.sleep(300)
            log.info("\n%s", tracker.report(cfg.markout_offsets_s, cfg.trade.min_edge_cents))

    tasks = [ws.run(), refresh_markets(), periodic_report()]
    if cfg.feeds.coinbase:
        tasks.append(btc_feed.coinbase(engine))
    if cfg.feeds.kraken:
        tasks.append(btc_feed.kraken(engine))
    await asyncio.gather(*tasks)


def main():
    ap = argparse.ArgumentParser(prog="kbot")
    ap.add_argument("cmd", choices=["discover", "run", "report", "sim"])
    ap.add_argument("-c", "--config", default="config.toml" if os.path.exists("config.toml") else None)
    ap.add_argument("--live", action="store_true", help="send real orders (also needs KBOT_CONFIRM_LIVE=yes)")
    ap.add_argument("--hours", type=float, default=2.0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(a.config)

    if a.cmd == "discover":
        asyncio.run(_discover(cfg))
    elif a.cmd == "run":
        if a.live and os.environ.get("KBOT_CONFIRM_LIVE") != "yes":
            sys.exit("Refusing to trade live: set KBOT_CONFIRM_LIVE=yes as well as --live.")
        try:
            asyncio.run(_run(cfg, a.live))
        except KeyboardInterrupt:
            pass
    elif a.cmd == "report":
        from .tracker import Tracker
        print(Tracker(cfg.db_path).report(cfg.markout_offsets_s, cfg.trade.min_edge_cents))
    elif a.cmd == "sim":
        from .sim import run_sim
        if not a.verbose:
            logging.getLogger("kbot").setLevel(logging.WARNING)
        print(run_sim(cfg, hours=a.hours, seed=a.seed))


if __name__ == "__main__":
    main()
