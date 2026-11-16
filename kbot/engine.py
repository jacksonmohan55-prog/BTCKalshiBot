"""Core loop: BTC trades -> detector -> for each horizon pick the Kalshi market with the most
stale-quote edge -> risk checks -> order. Also schedules post-signal quote snapshots and settlement."""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Callable

from .broker import Order
from .config import Config
from .models import Market, OrderBook, Position, Signal
from .pricing import fair_yes, implied_sigma, settle_yes, taker_fee_cents
from .signals import Detector
from .tracker import Tracker

log = logging.getLogger("kbot.engine")


class RealClock:
    def __init__(self, loop):
        self.loop = loop

    def now(self) -> float:
        return time.time()

    def call_later(self, delay: float, fn: Callable[[], None]):
        self.loop.call_later(max(delay, 0), fn)


class Engine:
    def __init__(self, cfg: Config, clock, tracker: Tracker, broker=None,
                 resolver: Callable[[Market, Callable[[str], None]], None] | None = None):
        self.cfg, self.clock, self.tracker, self.broker = cfg, clock, tracker, broker
        self.resolver = resolver
        self.detector = Detector(cfg.signal)
        self.markets: dict[str, Market] = {}
        self.books: dict[str, OrderBook] = {}
        self.positions: dict[str, Position] = {}
        self.venue_last: dict[str, tuple[float, float]] = {}      # venue -> (ts, price)
        self.price: float | None = None
        self.price_hist: deque[tuple[float, float]] = deque()      # (ts, composite price), ~20 min
        self.realized_cents = 0.0
        self._day = None
        self._market_last_order: dict[str, float] = {}
        self._settle_scheduled: set[str] = set()
        self.on_signal_hooks: list[Callable[[Signal], None]] = []

    # ------------------------------------------------------------------ inputs
    def on_trade(self, venue: str, ts: float, price: float, size: float, taker_side: int):
        now = self.clock.now()
        self.venue_last[venue] = (now, price)
        fresh = [p for t, p in self.venue_last.values() if now - t <= self.cfg.feeds.venue_stale_s]
        self.price = sum(fresh) / len(fresh)
        self.price_hist.append((now, self.price))
        while self.price_hist and now - self.price_hist[0][0] > 1200:
            self.price_hist.popleft()
        sig = self.detector.on_trade(now, self.price, size, taker_side)
        if sig:
            self.handle_signal(sig)

    def upsert_market(self, m: Market):
        old = self.markets.get(m.ticker)
        if old and old.floor_strike is not None and m.floor_strike is None:
            m.floor_strike = old.floor_strike
        if m.floor_strike is None and m.strike_type.startswith("greater") and m.open_ts:
            # up/down markets: strike = BTC at the open, if we were watching then
            p = self.price_near(m.open_ts)
            if p is not None:
                m.floor_strike = p
        self.markets[m.ticker] = m
        self.books.setdefault(m.ticker, OrderBook())
        if m.ticker not in self._settle_scheduled:
            self._settle_scheduled.add(m.ticker)
            self.clock.call_later(m.close_ts - self.clock.now() + 2, lambda t=m.ticker: self._settle(t))

    def book_snapshot(self, ticker: str, yes: list[tuple[int, int]], no: list[tuple[int, int]]):
        b = self.books.setdefault(ticker, OrderBook())
        now = self.clock.now()
        before = (b.best_ask("yes"), b.best_ask("no"))
        b.yes_bids = {p: q for p, q in yes if q > 0}
        b.no_bids = {p: q for p, q in no if q > 0}
        b.updated = now
        if b.best_ask("yes") != before[0]:
            b.ask_changed["yes"] = now
        if b.best_ask("no") != before[1]:
            b.ask_changed["no"] = now

    def book_delta(self, ticker: str, side: str, price: int, delta: int):
        b = self.books.setdefault(ticker, OrderBook())
        bids = b.yes_bids if side == "yes" else b.no_bids
        b.set_level(side, price, bids.get(price, 0) + delta, self.clock.now())

    def price_near(self, ts: float) -> float | None:
        best = None
        for t, p in self.price_hist:
            if abs(t - ts) <= 2 and (best is None or abs(t - ts) < best[0]):
                best = (abs(t - ts), p)
        return best[1] if best else None

    # ------------------------------------------------------------------ signal handling
    def handle_signal(self, sig: Signal):
        log.info("SIGNAL %s %s %.1fbps z=%.1f volz=%.1f px=%.2f", "UP" if sig.direction > 0 else "DOWN",
                 sig.reason, sig.ret_bps, sig.z_ret, sig.z_vol, sig.price)
        for h in self.on_signal_hooks:
            h(sig)
        sid = self.tracker.record_signal(sig)
        onset = sig.ts - self.cfg.signal.lookback_s
        now = self.clock.now()
        for horizon in self.cfg.horizons:
            best = None
            for m in self.markets.values():
                if m.horizon != horizon or m.result is not None:
                    continue
                ttc = m.close_ts - now
                if ttc < self.cfg.trade.min_seconds_to_close:
                    continue
                book = self.books.get(m.ticker)
                if not book or now - book.updated > self.cfg.trade.max_book_age_s:
                    continue
                # Price with the volatility Kalshi's own quote implies at the pre-move BTC price,
                # so the only disagreement left is the move Kalshi hasn't reacted to yet.
                # Markets whose quote can't be matched by any volatility are skipped: there our
                # model and Kalshi disagree for reasons other than the move (e.g. index basis).
                yb, ya = book.best_bid("yes"), book.best_ask("yes")
                if yb is None or ya is None or ya - yb > self.cfg.trade.max_spread_cents:
                    continue
                mid = (yb + ya) / 200
                if not 0.03 < mid < 0.97:
                    continue
                sigma = implied_sigma(m, sig.price_before, now, mid)
                if sigma is None:
                    continue
                f_now = fair_yes(m, sig.price, sigma, now)
                f_before = fair_yes(m, sig.price_before, sigma, now)
                if f_now is None or f_before is None or f_now == f_before:
                    continue
                side = "yes" if f_now > f_before else "no"
                ask = book.best_ask(side)
                if ask is None or ask >= 100:
                    continue
                fair = f_now if side == "yes" else 1 - f_now
                edge = fair * 100 - ask - taker_fee_cents(ask, 1, self.cfg.trade.fee_rate)
                stale = book.ask_changed[side] < onset
                if best is None or edge > best[1]:
                    best = (m, edge, side, ask, book.best_bid(side), fair, stale, ttc)
            if best is None:
                self.tracker.record_obs(sid, horizon, None, None, None, None, None, None, 0, None,
                                        "no quotable market")
                continue
            m, edge, side, ask, bid, fair, stale, ttc = best
            skip = self._risk_check(m, side, ask, edge)
            obs_id = self.tracker.record_obs(sid, horizon, m.ticker, side, ask, bid, fair, edge,
                                             stale, ttc, skip)
            self._schedule_snapshots(obs_id, m.ticker, side)
            log.info("  %-4s %s %s ask=%s fair=%.1f edge=%.1f stale=%s %s", horizon, m.ticker, side,
                     ask, fair * 100, edge, stale, skip or "-> ORDER")
            if skip is None and self.broker is not None:
                self._order(obs_id, m, side, ask)

    def _risk_check(self, m: Market, side: str, ask: int, edge: float) -> str | None:
        t = self.cfg.trade
        now = self.clock.now()
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        if day != self._day:
            self._day, self.realized_cents = day, 0.0
        if t.require_edge and edge < t.min_edge_cents:
            return f"edge {edge:.1f} < {t.min_edge_cents}"
        if -self.realized_cents / 100 >= t.daily_loss_limit_usd:
            return "daily loss limit"
        if now - self._market_last_order.get(m.ticker, -1e18) < t.market_cooldown_s:
            return "market cooldown"
        pos = self.positions.get(m.ticker)
        if pos and pos.side != side and pos.qty > 0:
            return "holding opposite side"
        if pos and pos.qty + t.contracts_per_order > t.max_contracts_per_market:
            return "market position limit"
        exposure = sum(p.cost_cents for p in self.positions.values()) / 100
        if exposure + t.contracts_per_order * (ask + t.slippage_cents) / 100 > t.max_total_exposure_usd:
            return "exposure limit"
        return None

    def _order(self, obs_id: int, m: Market, side: str, ask: int):
        t = self.cfg.trade
        self._market_last_order[m.ticker] = self.clock.now()
        order = Order(m.ticker, side, t.contracts_per_order, min(ask + t.slippage_cents, 99))

        def on_fill(o: Order, filled: int, avg: float, fee: float, order_id: str):
            if filled <= 0:
                log.info("  no fill %s %s", o.ticker, o.side)
                return
            pos = self.positions.setdefault(o.ticker, Position(o.ticker, o.side))
            pos.side = o.side
            pos.qty += filled
            pos.cost_cents += filled * avg + fee
            self.tracker.record_trade(obs_id, self.clock.now(), o.ticker, m.horizon, o.side, filled, avg,
                                      fee, self.broker.mode, order_id)
            log.info("  FILL %s %s x%d @%.1f fee=%.0fc", o.ticker, o.side, filled, avg, fee)

        self.broker.submit(order, on_fill)

    def _schedule_snapshots(self, obs_id: int, ticker: str, side: str):
        for off in self.cfg.markout_offsets_s:
            def snap(off=off):
                b = self.books.get(ticker)
                self.tracker.record_snapshot(obs_id, off, b.best_bid(side) if b else None,
                                             b.best_ask(side) if b else None)
            self.clock.call_later(off, snap)

    # ------------------------------------------------------------------ settlement
    def estimate_settlement(self, m: Market) -> str | None:
        """Fallback: average composite price over the last 60 s before close (Kalshi's
        BTC contracts settle on a 60 s average of the CF Benchmarks index)."""
        pts = [p for t, p in self.price_hist if m.close_ts - 60 <= t <= m.close_ts]
        if not pts or m.floor_strike is None and m.cap_strike is None:
            return None
        return "yes" if settle_yes(m, sum(pts) / len(pts)) else "no"

    def _settle(self, ticker: str):
        m = self.markets.get(ticker)
        if m is None or m.result is not None:
            return

        def done(result: str | None):
            if result is None or m.result is not None:
                return
            m.result = result
            pnl = self.tracker.settle(ticker, result)
            if ticker in self.positions:
                self.realized_cents += pnl
                pos = self.positions.pop(ticker)
                log.info("SETTLED %s -> %s; held %s x%d, pnl $%.2f", ticker, result, pos.side, pos.qty,
                         pnl / 100)
            self.books.pop(ticker, None)
            self.markets.pop(ticker, None)

        if ticker not in self.positions:          # nothing to settle; don't spend API calls on it
            self.books.pop(ticker, None)
            self.markets.pop(ticker, None)
            return
        if self.resolver:
            self.resolver(m, done)
        else:
            done(self.estimate_settlement(m))
