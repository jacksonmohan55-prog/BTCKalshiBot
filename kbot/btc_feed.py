"""Public BTC/USD trade streams. Each trade -> engine.on_trade(venue, ts, price, size, taker_side)."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

log = logging.getLogger("kbot.feed")


async def _stream(name: str, url: str, sub: dict, parse, engine):
    import websockets
    backoff = 1
    while True:
        try:
            async with websockets.connect(url, ping_interval=10, max_size=None) as ws:
                await ws.send(json.dumps(sub))
                log.info("%s connected", name)
                backoff = 1
                async for raw in ws:
                    for ts, px, sz, side in parse(json.loads(raw)):
                        engine.on_trade(name, ts, px, sz, side)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("%s feed error: %s", name, e)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


def _iso(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def parse_coinbase(m: dict):
    if m.get("type") in ("match", "last_match"):
        # Coinbase `side` is the maker's side; a "sell" maker means the taker bought.
        yield _iso(m["time"]), float(m["price"]), float(m["size"]), (1 if m["side"] == "sell" else -1)


def parse_kraken(m: dict):
    if m.get("channel") == "trade" and m.get("type") == "update":
        for t in m.get("data", []):
            yield _iso(t["timestamp"]), float(t["price"]), float(t["qty"]), (1 if t["side"] == "buy" else -1)


def coinbase(engine):
    return _stream("coinbase", "wss://ws-feed.exchange.coinbase.com",
                   {"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["matches"]},
                   parse_coinbase, engine)


def kraken(engine):
    return _stream("kraken", "wss://ws.kraken.com/v2",
                   {"method": "subscribe", "params": {"channel": "trade", "symbol": ["BTC/USD"]}},
                   parse_kraken, engine)
