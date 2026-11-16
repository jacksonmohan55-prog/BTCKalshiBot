"""Order execution. PaperBroker fills against the live Kalshi book after a simulated latency;
LiveBroker sends immediate-or-cancel limit orders to Kalshi."""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Callable

from .pricing import taker_fee_cents

log = logging.getLogger("kbot.broker")


@dataclass
class Order:
    ticker: str
    side: str          # "yes" | "no"
    qty: int
    limit_cents: float
    client_id: str = ""


FillCb = Callable[[Order, float, float, float, str], None]   # order, filled qty, avg price, fee, order_id


class PaperBroker:
    mode = "paper"

    def __init__(self, clock, get_book, latency_ms: float, fee_rate: float):
        self.clock, self.get_book, self.latency, self.fee_rate = clock, get_book, latency_ms / 1000, fee_rate

    def submit(self, order: Order, on_fill: FillCb):
        self.clock.call_later(self.latency, lambda: self._fill(order, on_fill))

    def _fill(self, order: Order, on_fill: FillCb):
        book = self.get_book(order.ticker)
        if book is None:
            return on_fill(order, 0, 0.0, 0.0, "")
        left, cost = order.qty, 0.0
        for price, qty in book.asks(order.side):
            if price > order.limit_cents or left == 0:
                break
            take = min(qty, left)
            cost += take * price
            left -= take
        filled = order.qty - left
        if filled == 0:
            return on_fill(order, 0, 0.0, 0.0, "")
        avg = cost / filled
        fee = taker_fee_cents(avg, filled, self.fee_rate)
        # consume liquidity so repeated paper orders can't fill against the same contracts
        opp = book.no_bids if order.side == "yes" else book.yes_bids
        rem = filled
        for p in sorted(opp, reverse=True):
            if rem == 0:
                break
            take = min(opp[p], rem)
            book.set_level("no" if order.side == "yes" else "yes", p, opp[p] - take, self.clock.now())
            rem -= take
        on_fill(order, filled, avg, fee, "paper-" + uuid.uuid4().hex[:8])


class LiveBroker:
    mode = "live"

    def __init__(self, rest, fee_rate: float):
        self.rest, self.fee_rate = rest, fee_rate

    def submit(self, order: Order, on_fill: FillCb):
        asyncio.get_running_loop().create_task(self._send(order, on_fill))

    async def _send(self, order: Order, on_fill: FillCb):
        order.client_id = order.client_id or uuid.uuid4().hex
        try:
            resp = await self.rest.create_order(order)
        except Exception as e:  # noqa: BLE001
            log.error("order failed %s: %s", order, e)
            return on_fill(order, 0, 0.0, 0.0, "")
        o = resp.get("order", resp)
        filled = o.get("fill_count", o.get("fill_count_fp", o.get("taker_fill_count")))
        if filled is None:
            filled = order.qty - float(o.get("remaining_count", order.qty))
        filled = float(filled)
        cost = o.get("taker_fill_cost")
        if cost is None and o.get("taker_fill_cost_dollars") is not None:
            cost = float(o["taker_fill_cost_dollars"]) * 100
        avg = (float(cost) / filled) if (cost and filled) else float(order.limit_cents)
        fee = o.get("taker_fees")
        if fee is None and o.get("taker_fees_dollars") is not None:
            fee = float(o["taker_fees_dollars"]) * 100
        fee = float(fee) if fee is not None else taker_fee_cents(avg, filled, self.fee_rate)
        log.info("LIVE order %s %s x%d @<=%d -> filled %d avg %.1f status %s", order.ticker,
                 order.side, order.qty, order.limit_cents, filled, avg, o.get("status"))
        on_fill(order, filled, avg, fee, o.get("order_id", ""))
