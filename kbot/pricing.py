"""Fair value of a Kalshi BTC contract and Kalshi's taker fee."""
from __future__ import annotations

import math

from .models import Market


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_above(spot: float, strike: float, sigma_1s: float, seconds: float) -> float:
    """P(BTC at expiry > strike) under driftless lognormal moves."""
    if seconds <= 0:
        return 1.0 if spot > strike else 0.0
    sd = sigma_1s * math.sqrt(seconds)
    if sd <= 0:
        return 1.0 if spot > strike else 0.0
    return _phi(math.log(spot / strike) / sd)


def fair_yes(m: Market, spot: float, sigma_1s: float, now: float) -> float | None:
    """Probability the YES side pays out, or None if the market's strike is unknown."""
    t = m.close_ts - now
    st = m.strike_type
    if st in ("greater", "greater_or_equal"):
        return None if m.floor_strike is None else prob_above(spot, m.floor_strike, sigma_1s, t)
    if st in ("less", "less_or_equal"):
        k = m.cap_strike if m.cap_strike is not None else m.floor_strike
        return None if k is None else 1.0 - prob_above(spot, k, sigma_1s, t)
    if st == "between":
        if m.floor_strike is None or m.cap_strike is None:
            return None
        return (prob_above(spot, m.floor_strike, sigma_1s, t)
                - prob_above(spot, m.cap_strike, sigma_1s, t))
    return None


def implied_sigma(m: Market, spot: float, now: float, target: float,
                  lo: float = 1e-6, hi: float = 5e-3) -> float | None:
    """Per-second volatility at which our model reproduces `target` (a Kalshi mid, 0-1) at `spot`.
    Bisection; None if the price can't be matched (e.g. too close to 0/1 or not monotone)."""
    f_lo, f_hi = fair_yes(m, spot, lo, now), fair_yes(m, spot, hi, now)
    if f_lo is None or f_hi is None:
        return None
    if (f_lo - target) * (f_hi - target) > 0:
        return None
    for _ in range(60):
        mid = (lo * hi) ** 0.5
        f = fair_yes(m, spot, mid, now)
        if (f - target) * (f_lo - target) > 0:
            lo, f_lo = mid, f
        else:
            hi = mid
    return (lo * hi) ** 0.5


def settle_yes(m: Market, settle_price: float) -> bool:
    st = m.strike_type
    if st == "greater":
        return settle_price > m.floor_strike
    if st == "greater_or_equal":
        return settle_price >= m.floor_strike
    k = m.cap_strike if m.cap_strike is not None else m.floor_strike
    if st == "less":
        return settle_price < k
    if st == "less_or_equal":
        return settle_price <= k
    if st == "between":
        return m.floor_strike <= settle_price <= m.cap_strike
    raise ValueError(f"unknown strike_type {st}")


def taker_fee_cents(price_cents: float, contracts: int, rate: float = 0.07) -> float:
    """Kalshi taker fee for the whole order, rounded up to the cent."""
    p = price_cents / 100.0
    return math.ceil(rate * contracts * p * (1 - p) * 100 - 1e-9)
