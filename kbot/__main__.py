"""CLI.

  python -m kbot discover                 list Kalshi crypto series (find the 1m/5m/15m BTC tickers)
  python -m kbot run [--live]             stream BTC + Kalshi; paper trades unless --live
  python -m kbot report                   per-horizon scoreboard from the database
  python -m kbot dashboard                live dashboard (open in a second window while the bot runs)
  python -m kbot review [--all] [--csv f] grade past bets and show which kinds win or lose
  python -m kbot sim [--hours 2]          offline end-to-end simulation
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time

import warnings

# macOS's built-in Python is linked against LibreSSL, which makes urllib3 print a harmless warning.
warnings.filterwarnings("ignore", message=r"urllib3 v2 only supports OpenSSL")

from .config import load_config  # noqa: E402


async def _discover(cfg):
    from .kalshi import KalshiAuth, KalshiREST
    rest = KalshiREST(cfg.kalshi, KalshiAuth(cfg.kalshi, load=False))   # public data, no key needed
    for s in await rest.series("Crypto"):
        if "BTC" in s.get("ticker", "").upper() or "bitcoin" in s.get("title", "").lower():
            mk = await rest.open_markets(s["ticker"])
            print(f"{s['ticker']:<16} freq={s.get('frequency', '?'):<10} open={len(mk):<4} {s.get('title', '')}")


async def _run(cfg, live: bool):
    from . import btc_feed
    from .broker import LiveBroker, PaperBroker
    from .engine import Engine, RealClock
    from .kalshi import KalshiAuth, KalshiPoller, KalshiREST, KalshiWS, parse_market
    from .tracker import Tracker

    from .kalshi import KeyFileError
    log = logging.getLogger("kbot")
    try:
        auth = KalshiAuth(cfg.kalshi)
    except KeyFileError as e:
        sys.exit(str(e))
    if not auth.ok and live:
        sys.exit("Live trading needs a Kalshi API key: set key_id / private_key_path in config.toml.")
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

    poller = KalshiPoller(cfg.kalshi, rest, engine, cfg.trade.min_seconds_to_close)
    if auth.ok:
        ws = KalshiWS(cfg.kalshi, auth, engine)
        poller.skip_fresh_s = 5.0          # healthy websocket: poll only markets it isn't updating

        async def books_with_fallback():
            """Websocket first; if it delivers no orderbook updates for 60 s, poll the public
            REST orderbooks until it recovers."""
            import time as _t
            started = _t.time()

            async def watchdog():
                while True:
                    await asyncio.sleep(15)
                    healthy = _t.time() - max(ws.last_book_msg, started) < 60
                    if not healthy and poller.skip_fresh_s:
                        log.error("Kalshi websocket sent no orderbook data for 60s; falling back to "
                                  "public REST polling (check the API key if this persists)")
                    elif healthy and not poller.skip_fresh_s:
                        log.warning("Kalshi websocket healthy again")
                    poller.skip_fresh_s = 5.0 if healthy else 0.0
            await asyncio.gather(ws.run(), poller.run(), watchdog())
        book_task = books_with_fallback()
        log.info("Kalshi API key loaded: using the authenticated websocket feed (%s)", cfg.kalshi.env)
    else:
        ws = None
        book_task = poller.run()
        log.warning("No Kalshi API key: paper mode on %s public orderbooks, polled every %.1fs "
                    "(%d markets per horizon). Add a key for the faster websocket feed.",
                    cfg.kalshi.env, cfg.kalshi.poll_s, cfg.kalshi.poll_markets_per_series)

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
                if ws:
                    await ws.ensure(tickers)
            await asyncio.sleep(cfg.kalshi.market_refresh_s)

    async def status():
        from .pricing import implied_sigma
        while True:
            await asyncio.sleep(60)
            now = clock.now()
            fresh = [(t, b) for t, b in engine.books.items() if now - b.updated < 10]
            parts = []
            for t, b in sorted(fresh):
                m = engine.markets.get(t)
                yb, ya = b.best_bid("yes"), b.best_ask("yes")
                iv = None
                if m and engine.price and yb is not None and ya is not None and 3 < (yb + ya) / 2 < 97:
                    iv = implied_sigma(m, engine.price, now, (yb + ya) / 200)
                parts.append(f"{t} yes {yb}/{ya}" + (f" iv {iv * 1e4:.2f}" if iv else ""))
            px = f"{engine.price:,.2f}" if engine.price else "n/a"
            log.info("STATUS btc=%s realized vol=%.2fbps/s live books=%d | %s", px, engine.detector.sigma_1s * 1e4,
                     len(fresh), " ; ".join(parts) or "none")

    async def write_status():
        """Snapshot for `python -m kbot dashboard` (a separate process), written atomically."""
        import json
        from collections import deque

        from .pricing import implied_sigma
        hist: deque = deque(maxlen=360)            # 5-second BTC samples, last 30 minutes
        started = time.time()
        while True:
            now = time.time()
            if engine.price and (not hist or now - hist[-1][0] >= 5):
                hist.append((now, engine.price))
            books = []
            for t, b in engine.books.items():
                m = engine.markets.get(t)
                if m is None or now - b.updated > 15:
                    continue
                yb, ya = b.best_bid("yes"), b.best_ask("yes")
                iv = None
                if engine.price and yb is not None and ya is not None and 3 < (yb + ya) / 2 < 97:
                    iv = implied_sigma(m, engine.price, now, (yb + ya) / 200)
                books.append({"ticker": t, "horizon": m.horizon, "close_ts": m.close_ts,
                              "strike_type": m.strike_type, "floor": m.floor_strike, "cap": m.cap_strike,
                              "yes_bid": yb, "yes_ask": ya, "iv_bps": iv * 1e4 if iv else None})
            pos = [{"ticker": p.ticker, "side": p.side, "qty": p.qty, "cost": p.cost_cents / 100}
                   for p in engine.positions.values()]
            data = {
                "ts": now, "started": started, "pid": os.getpid(), "mode": "live" if live else "paper",
                "env": cfg.kalshi.env,
                "feed": "websocket" if ws is not None and poller.skip_fresh_s else "polling",
                "btc": engine.price, "sigma_bps": engine.detector.sigma_1s * 1e4,
                "venues": {v: now - t for v, (t, _) in engine.venue_last.items()},
                "warmup_left": max(0.0, cfg.signal.warmup_s - (now - started)),
                "history": list(hist), "books": books, "positions": pos,
                "realized_today": engine.realized_cents / 100,
                "horizons": cfg.horizons, "min_edge": cfg.trade.min_edge_cents,
            }
            tmp = cfg.status_path + ".tmp"
            try:
                with open(tmp, "w") as fh:
                    json.dump(data, fh)
                os.replace(tmp, cfg.status_path)
            except OSError as e:
                log.debug("status write failed: %s", e)
            await asyncio.sleep(2)

    async def periodic_report():
        while True:
            await asyncio.sleep(300)
            log.info("\n%s", tracker.report(cfg.markout_offsets_s, cfg.trade.min_edge_cents))

    tasks = [book_task, refresh_markets(), periodic_report(), status(), write_status()]
    if cfg.feeds.coinbase:
        tasks.append(btc_feed.coinbase(engine))
    if cfg.feeds.kraken:
        tasks.append(btc_feed.kraken(engine))
    await asyncio.gather(*tasks)


def main():
    ap = argparse.ArgumentParser(prog="kbot")
    ap.add_argument("cmd", choices=["discover", "run", "report", "sim", "dashboard", "review"])
    ap.add_argument("-c", "--config", default="config.toml" if os.path.exists("config.toml") else None)
    ap.add_argument("--live", action="store_true", help="send real orders (also needs KBOT_CONFIRM_LIVE=yes)")
    ap.add_argument("--hours", type=float, default=2.0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--once", action="store_true", help="dashboard: print one frame and exit")
    ap.add_argument("--svg", help="dashboard: with --once, also save the frame as an SVG image")
    ap.add_argument("--all", action="store_true", help="review: list every bet, not just the latest 25")
    ap.add_argument("--csv", help="review: also save every bet to this spreadsheet file")
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
    elif a.cmd == "dashboard":
        from .dashboard import run as run_dashboard
        run_dashboard(cfg, once=a.once, svg=a.svg)
    elif a.cmd == "review":
        from .review import review
        review(cfg, show_all=a.all, csv_path=a.csv)
    elif a.cmd == "sim":
        from .sim import run_sim
        if not a.verbose:
            logging.getLogger("kbot").setLevel(logging.WARNING)
        print(run_sim(cfg, hours=a.hours, seed=a.seed))


if __name__ == "__main__":
    main()
