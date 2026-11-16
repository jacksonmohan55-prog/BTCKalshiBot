"""Kalshi Trade API v2: RSA-PSS request signing, REST, and the orderbook WebSocket."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .config import KalshiCfg
from .models import Market, parse_iso

log = logging.getLogger("kbot.kalshi")


class KeyFileError(Exception):
    pass


class KalshiAuth:
    def __init__(self, cfg: KalshiCfg, load: bool = True):
        self.key_id = (cfg.key_id or os.environ.get("KALSHI_KEY_ID", "")).strip()
        path = (cfg.private_key_path or os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")).strip().strip('"\'')
        self.key = None
        if load and self.key_id and path:
            self.key = self._load(path)

    @staticmethod
    def _load(path: str):
        full = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(full):
            folder = os.path.dirname(full)
            hint = ""
            if os.path.isdir(folder):
                near = [f for f in os.listdir(folder)
                        if f.lower().endswith((".pem", ".key", ".txt")) or "kalshi" in f.lower()]
                if near:
                    hint = "\n  Possible key files in that folder: " + ", ".join(sorted(near)[:10])
            else:
                hint = f"\n  The folder {folder} does not exist either."
            raise KeyFileError(f"Kalshi private key file not found:\n  {full}{hint}\n"
                               "  Fix private_key_path in config.toml (use / not \\) or KALSHI_PRIVATE_KEY_PATH.")
        with open(full, "rb") as fh:
            data = fh.read()
        try:
            return serialization.load_pem_private_key(data, password=None)
        except ValueError as e:
            raise KeyFileError(f"{full} is not a valid PEM private key ({e}). It should start with "
                               "'-----BEGIN RSA PRIVATE KEY-----' or '-----BEGIN PRIVATE KEY-----'.") from e

    @property
    def ok(self) -> bool:
        return self.key is not None

    def headers(self, method: str, path: str) -> dict[str, str]:
        """path is the URL path only (no query string), e.g. /trade-api/v2/portfolio/orders."""
        if not self.ok:
            raise RuntimeError("Kalshi API key not configured (KALSHI_KEY_ID / KALSHI_PRIVATE_KEY_PATH)")
        ts = str(int(time.time() * 1000))
        sig = self.key.sign((ts + method.upper() + path).encode(),
                            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                        salt_length=padding.PSS.DIGEST_LENGTH),
                            hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
                "KALSHI-ACCESS-TIMESTAMP": ts}


def _cents(d: dict, key: str) -> int | None:
    """Kalshi returns prices either as integer cents (`yes_ask`) or dollar strings (`yes_ask_dollars`)."""
    if d.get(key) is not None:
        return int(round(float(d[key])))
    if d.get(key + "_dollars") is not None:
        return int(round(float(d[key + "_dollars"]) * 100))
    return None


def _px(v, dollars: bool) -> float:
    """Price in cents, keeping sub-cent ticks (Kalshi quotes in $0.001 on some markets)."""
    return round(float(v) * (100 if dollars else 1), 4)


def parse_levels(body: dict, side: str) -> list[tuple[float, float]]:
    """Orderbook levels for `side` from any of Kalshi's formats:
    yes: [[42, 10]] (cents) | yes_dollars: [["0.4200", "10.00"]] | *_fp variants."""
    for key in (side, side + "_fp", side + "_dollars", side + "_dollars_fp"):
        if body.get(key) is not None:
            dollars = "dollars" in key
            return [(_px(p, dollars), float(q)) for p, q in body[key]]
    return []


def parse_delta(body: dict) -> tuple[float, float]:
    if body.get("price") is not None:
        price = _px(body["price"], False)
    else:
        price = _px(body["price_dollars"], True)
    delta = body.get("delta", body.get("delta_fp"))
    return price, float(delta)


def _ts(s: str | None) -> float | None:
    if not s:
        return None
    return parse_iso(s)


def parse_market(d: dict, series: str, horizon: str) -> Market | None:
    close = _ts(d.get("close_time"))
    if close is None:
        return None
    f = d.get("floor_strike")
    c = d.get("cap_strike")
    return Market(ticker=d["ticker"], series=series, horizon=horizon, close_ts=close,
                  open_ts=_ts(d.get("open_time")), strike_type=d.get("strike_type") or "greater",
                  floor_strike=float(f) if f is not None else None,
                  cap_strike=float(c) if c is not None else None)


class KalshiREST:
    def __init__(self, cfg: KalshiCfg, auth: KalshiAuth):
        self.cfg, self.auth = cfg, auth
        self.s = requests.Session()
        self._path_prefix = urlparse(cfg.rest_base).path

    def _req(self, method: str, path: str, signed=False, **kw):
        url = self.cfg.rest_base + path
        headers = self.auth.headers(method, self._path_prefix + path) if signed else {}
        if "json" in kw:
            headers["Content-Type"] = "application/json"
        r = self.s.request(method, url, headers=headers, timeout=10, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    async def call(self, *a, **kw):
        return await asyncio.to_thread(self._req, *a, **kw)

    async def series(self, category="Crypto"):
        return (await self.call("GET", "/series", params={"category": category})).get("series", [])

    async def open_markets(self, series_ticker: str) -> list[dict]:
        out, cursor = [], None
        while True:
            params = {"series_ticker": series_ticker, "status": "open", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            r = await self.call("GET", "/markets", params=params)
            out += r.get("markets", [])
            cursor = r.get("cursor")
            if not cursor:
                return out

    async def market(self, ticker: str) -> dict:
        return (await self.call("GET", f"/markets/{ticker}")).get("market", {})

    async def orderbook(self, ticker: str) -> dict:
        r = await self.call("GET", f"/markets/{ticker}/orderbook")
        return r.get("orderbook_fp") or r.get("orderbook") or {}

    async def balance(self) -> dict:
        return await self.call("GET", "/portfolio/balance", signed=True)

    async def create_order(self, order) -> dict:
        body = {"ticker": order.ticker, "client_order_id": order.client_id, "action": "buy",
                "side": order.side, "count": order.qty, "type": "limit",
                "time_in_force": "immediate_or_cancel"}
        if float(order.limit_cents).is_integer():
            body[order.side + "_price"] = int(order.limit_cents)
        else:
            body[order.side + "_price_dollars"] = f"{order.limit_cents / 100:.4f}"
        return await self.call("POST", "/portfolio/orders", signed=True, json=body)


class KalshiWS:
    """Streams orderbook snapshots/deltas into the engine; resubscribes as markets roll over."""

    def __init__(self, cfg: KalshiCfg, auth: KalshiAuth, engine):
        self.cfg, self.auth, self.engine = cfg, auth, engine
        self.wanted: set[str] = set()
        self._subscribed: set[str] = set()
        self._ws = None
        self._msg_id = 0
        self._seq: dict[int, int] = {}
        self.last_book_msg = 0.0          # time.time() of the last orderbook snapshot/delta

    async def ensure(self, tickers: set[str]):
        self.wanted |= tickers
        new = self.wanted - self._subscribed
        if self._ws is not None and new:
            await self._subscribe(sorted(new))

    async def _subscribe(self, tickers: list[str]):
        for i in range(0, len(tickers), 200):
            self._msg_id += 1
            chunk = tickers[i:i + 200]
            await self._ws.send(json.dumps({"id": self._msg_id, "cmd": "subscribe", "params": {
                "channels": ["orderbook_delta"], "market_tickers": chunk}}))
            self._subscribed |= set(chunk)

    async def run(self):
        from websockets.asyncio.client import connect
        backoff = 1
        while True:
            try:
                path = urlparse(self.cfg.ws_url).path
                async with connect(self.cfg.ws_url, additional_headers=self.auth.headers("GET", path),
                                              ping_interval=10, max_size=None) as ws:
                    self._ws, self._subscribed, self._seq = ws, set(), {}
                    log.info("kalshi ws connected")
                    backoff = 1
                    if self.wanted:
                        await self._subscribe(sorted(self.wanted))
                    async for raw in ws:
                        if self._handle(json.loads(raw)) == "resync":
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                if "401" in str(e):
                    log.warning("kalshi ws: Kalshi rejected the API key (HTTP 401). Check that key_id matches "
                                "the .pem file and that env (%s) matches where the key was made "
                                "(demo.kalshi.co vs kalshi.com).", self.cfg.env)
                else:
                    log.warning("kalshi ws error: %s", e)
            self._ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    def _handle(self, msg: dict):
        typ, body = msg.get("type"), msg.get("msg", {})
        sid, seq = msg.get("sid"), msg.get("seq")
        if sid is not None and seq is not None:
            prev = self._seq.get(sid)
            if prev is not None and seq != prev + 1 and typ != "orderbook_snapshot":
                log.warning("kalshi seq gap on sid %s (%s->%s); resyncing", sid, prev, seq)
                return "resync"
            self._seq[sid] = seq
        if typ in ("orderbook_snapshot", "orderbook_delta"):
            self.last_book_msg = time.time()
        if typ == "orderbook_snapshot":
            self.engine.book_snapshot(body["market_ticker"], parse_levels(body, "yes"), parse_levels(body, "no"))
        elif typ == "orderbook_delta":
            price, delta = parse_delta(body)
            self.engine.book_delta(body["market_ticker"], body["side"], price, delta)
        elif typ == "error":
            log.error("kalshi ws error msg: %s", body)


class KalshiPoller:
    """No-API-key fallback: polls public orderbooks over REST for the markets that matter
    (soonest expiry, strikes nearest the current BTC price). Slower than the websocket."""

    def __init__(self, cfg: KalshiCfg, rest: KalshiREST, engine, min_ttc: float):
        self.cfg, self.rest, self.engine, self.min_ttc = cfg, rest, engine, min_ttc
        self.enabled = True

    def pick(self) -> list[str]:
        now, px = self.engine.clock.now(), self.engine.price
        out = []
        by_h: dict[str, list[Market]] = {}
        for m in self.engine.markets.values():
            if m.result is None and m.close_ts - now >= self.min_ttc:
                by_h.setdefault(m.horizon, []).append(m)
        for ms in by_h.values():
            first = min(m.close_ts for m in ms)
            ms = [m for m in ms if m.close_ts == first]
            if px is not None:
                ms.sort(key=lambda m: abs((m.floor_strike or m.cap_strike or px) - px))
            out += [m.ticker for m in ms[:self.cfg.poll_markets_per_series]]
        return out

    async def _one(self, ticker: str):
        try:
            ob = await self.rest.orderbook(ticker)
        except Exception as e:  # noqa: BLE001
            log.debug("orderbook %s: %s", ticker, e)
            return
        self.engine.book_snapshot(ticker, parse_levels(ob, "yes"), parse_levels(ob, "no"))

    async def run(self):
        while True:
            t0 = time.time()
            if self.enabled:
                await asyncio.gather(*(self._one(t) for t in self.pick()))
            await asyncio.sleep(max(0.0, self.cfg.poll_s - (time.time() - t0)))
