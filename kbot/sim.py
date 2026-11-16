"""Offline simulator: synthetic BTC tape with occasional jumps + volume bursts, and a Kalshi-style
market maker per horizon that re-quotes from a *delayed* view of BTC. Exercises the full pipeline
(detector -> pricing -> paper fills -> markouts -> settlement -> scoreboard) deterministically.

The lags below are made up. Sim P&L says the plumbing works, not that the strategy makes money."""
from __future__ import annotations

import heapq
import itertools
import math
import os
import random

from .broker import PaperBroker
from .config import Config
from .engine import Engine
from .models import Market
from .pricing import prob_above
from .tracker import Tracker

HORIZONS = {  # label: (period seconds, market-maker lag seconds)
    "1m": (60, 0.4),
    "5m": (300, 1.5),
    "15m": (900, 3.0),
}


class SimClock:
    def __init__(self, t0: float):
        self.t = t0
        self._q: list = []
        self._n = itertools.count()

    def now(self) -> float:
        return self.t

    def call_later(self, delay, fn):
        heapq.heappush(self._q, (self.t + max(delay, 0), next(self._n), fn))

    def advance(self, t: float):
        while self._q and self._q[0][0] <= t:
            when, _, fn = heapq.heappop(self._q)
            self.t = max(self.t, when)
            fn()
        self.t = t


def _poisson(rng: random.Random, lam: float) -> int:
    k, p, L = 0, 1.0, math.exp(-lam)
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


def run_sim(cfg: Config, hours: float = 2.0, seed: int = 7, db_path: str = "sim.sqlite") -> str:
    rng = random.Random(seed)
    if os.path.exists(db_path):
        os.remove(db_path)
    cfg.horizons = {h: f"SIM{h.upper()}" for h in HORIZONS}
    cfg.db_path = db_path
    t0 = 1_800_000_000.0
    clock = SimClock(t0)
    tracker = Tracker(db_path, run=f"sim-seed{seed}")
    engine = Engine(cfg, clock, tracker)
    engine.broker = PaperBroker(clock, lambda t: engine.books.get(t), cfg.trade.paper_latency_ms,
                                cfg.trade.fee_rate)
    true_sigma = 1.0e-4                      # 1 bp per sqrt(second)
    engine.resolver = lambda m, done: done("yes" if price_log[-1][1] > m.floor_strike else "no")

    dt = 0.1
    S = 90_000.0
    price_log: list[tuple[float, float]] = [(t0, S)]
    jump_left, jump_step, jump_dir = 0, 0.0, 0
    next_open = {h: t0 for h in HORIZONS}
    live: dict[str, list[Market]] = {h: [] for h in HORIZONS}
    next_quote = {h: t0 for h in HORIZONS}
    steps = int(hours * 3600 / dt)
    lag_idx = {h: 0 for h in HORIZONS}

    for i in range(steps):
        t = t0 + i * dt
        clock.advance(t)
        # --- price process
        if jump_left == 0 and rng.random() < dt / 240:
            jump_dir = rng.choice((1, -1))
            size = rng.uniform(15, 45) / 1e4
            jump_left = rng.randint(5, 25)
            jump_step = size / jump_left
        drift = jump_dir * jump_step if jump_left else 0.0
        S *= math.exp(drift + rng.gauss(0, true_sigma * math.sqrt(dt)))
        price_log.append((t, S))
        intensity = 2.0 * (8 if jump_left else 1)
        for _ in range(_poisson(rng, intensity)):
            side = (jump_dir if rng.random() < 0.8 else -jump_dir) if jump_left else rng.choice((1, -1))
            engine.on_trade("sim", t, S * (1 + rng.gauss(0, 2e-6)), rng.expovariate(1 / 0.05), side)
        if jump_left:
            jump_left -= 1

        # --- market lifecycle
        for h, (period, lag) in HORIZONS.items():
            if t >= next_open[h]:
                m = Market(ticker=f"SIM{h.upper()}-{int(t)}", series=f"SIM{h.upper()}", horizon=h,
                           close_ts=t + period, open_ts=t, strike_type="greater", floor_strike=S)
                engine.upsert_market(m)
                live[h].append(m)
                next_open[h] = t + period
            live[h] = [m for m in live[h] if m.close_ts > t]
            # --- lagged market maker: re-quote every 0.5 s from the price `lag` seconds ago
            if t >= next_quote[h]:
                next_quote[h] = t + 0.5
                while lag_idx[h] + 1 < len(price_log) and price_log[lag_idx[h] + 1][0] <= t - lag:
                    lag_idx[h] += 1
                seen = price_log[lag_idx[h]][1]
                for m in live[h]:
                    fv = prob_above(seen, m.floor_strike, true_sigma, m.close_ts - t) * 100
                    yb = max(1, min(98, math.floor(fv - 1.5)))
                    nb = max(1, min(98, math.floor(100 - fv - 1.5)))
                    engine.book_snapshot(m.ticker, [(yb, 40), (yb - 1, 150)], [(nb, 40), (nb - 1, 150)])
        if len(price_log) > 20000:
            cut = len(price_log) - 15000
            del price_log[:cut]
            for h in lag_idx:
                lag_idx[h] = max(0, lag_idx[h] - cut)
    clock.advance(t0 + steps * dt + 1000)
    return tracker.report(cfg.markout_offsets_s, cfg.trade.min_edge_cents)
