from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

_FRAC = re.compile(r"\.(\d+)")


def parse_iso(s: str) -> float:
    """ISO-8601 -> unix seconds. Works on Python 3.9+ (pre-3.11 fromisoformat needs exactly
    3 or 6 fractional digits and no 'Z')."""
    s = s.replace("Z", "+00:00")
    s = _FRAC.sub(lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
    return datetime.fromisoformat(s).timestamp()


@dataclass
class Market:
    ticker: str
    series: str
    horizon: str
    close_ts: float
    open_ts: float | None = None
    strike_type: str = "greater"           # greater | greater_or_equal | less | less_or_equal | between
    floor_strike: float | None = None
    cap_strike: float | None = None
    result: str | None = None              # "yes" / "no" once settled


@dataclass
class OrderBook:
    """Kalshi books only carry bids: YES bids and NO bids. A YES ask at p is a NO bid at 100-p."""
    yes_bids: dict[float, float] = field(default_factory=dict)   # price cents -> contracts
    no_bids: dict[float, float] = field(default_factory=dict)
    updated: float = 0.0                                      # local time of last change
    ask_changed: dict[str, float] = field(default_factory=lambda: {"yes": 0.0, "no": 0.0})
    mid_hist: deque = field(default_factory=lambda: deque(maxlen=600))   # (ts, yes mid 0-1) on change

    def mid(self) -> float | None:
        b, a = self.best_bid("yes"), self.best_ask("yes")
        return None if b is None or a is None else (b + a) / 200

    def note_mid(self, now: float):
        m = self.mid()
        if m is not None and (not self.mid_hist or self.mid_hist[-1][1] != m):
            self.mid_hist.append((now, m))

    def mid_at(self, ts: float) -> float | None:
        """YES mid as it stood at time ts (None if we have no quote from before then)."""
        out = None
        for t, m in self.mid_hist:
            if t > ts:
                break
            out = m
        return out

    def _bids(self, side: str) -> dict[int, int]:
        return self.yes_bids if side == "yes" else self.no_bids

    def best_bid(self, side: str) -> int | None:
        b = [p for p, q in self._bids(side).items() if q > 0]
        return max(b) if b else None

    def best_ask(self, side: str) -> int | None:
        opp = self.best_bid("no" if side == "yes" else "yes")
        return None if opp is None else 100 - opp

    def asks(self, side: str) -> list[tuple[int, int]]:
        """Ask ladder for buying `side`, cheapest first."""
        opp = self._bids("no" if side == "yes" else "yes")
        return sorted(((100 - p, q) for p, q in opp.items() if q > 0))

    def set_level(self, side: str, price: int, qty: int, now: float):
        before = (self.best_ask("yes"), self.best_ask("no"))
        b = self._bids(side)
        if qty <= 0:
            b.pop(price, None)
        else:
            b[price] = qty
        self.updated = now
        after = (self.best_ask("yes"), self.best_ask("no"))
        if after[0] != before[0]:
            self.ask_changed["yes"] = now
        if after[1] != before[1]:
            self.ask_changed["no"] = now
        self.note_mid(now)


@dataclass
class Signal:
    ts: float
    direction: int                         # +1 up, -1 down
    reason: str                            # "jump" | "volume"
    price: float
    price_before: float
    ret_bps: float
    z_ret: float
    z_vol: float
    sigma_1s: float                        # per-second log-return std at signal time


@dataclass
class Position:
    ticker: str
    side: str
    qty: float = 0
    cost_cents: float = 0.0                # includes fees
