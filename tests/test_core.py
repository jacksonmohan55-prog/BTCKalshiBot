import base64
import random
import unittest

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kbot.btc_feed import parse_coinbase, parse_kraken
from kbot.config import Config, KalshiCfg, SignalCfg
from kbot.engine import Engine
from kbot.kalshi import KalshiAuth, KalshiWS, parse_market
from kbot.models import Market, OrderBook
from kbot.pricing import fair_yes, prob_above, taker_fee_cents
from kbot.sim import SimClock
from kbot.signals import Detector
from kbot.tracker import Tracker


class Pricing(unittest.TestCase):
    def test_at_the_money_is_half(self):
        self.assertAlmostEqual(prob_above(100, 100, 1e-4, 300), 0.5)

    def test_monotone_and_expiry(self):
        self.assertGreater(prob_above(100.2, 100, 1e-4, 300), 0.5)
        self.assertEqual(prob_above(101, 100, 1e-4, 0), 1.0)

    def test_between_and_less(self):
        m = Market("X", "S", "h", close_ts=300, strike_type="between", floor_strike=99, cap_strike=101)
        self.assertGreater(fair_yes(m, 100, 1e-4, 0), 0.9)
        m2 = Market("Y", "S", "h", close_ts=300, strike_type="less", cap_strike=100)
        self.assertAlmostEqual(fair_yes(m2, 100, 1e-4, 0), 0.5)

    def test_fee(self):
        # 0.07 * 10 * 0.5 * 0.5 = $0.175 -> 18c
        self.assertEqual(taker_fee_cents(50, 10), 18)
        self.assertEqual(taker_fee_cents(50, 1), 2)


class Book(unittest.TestCase):
    def test_asks_from_opposite_bids(self):
        b = OrderBook()
        b.set_level("yes", 40, 10, 1)
        b.set_level("no", 55, 5, 1)
        b.set_level("no", 54, 7, 1)
        self.assertEqual(b.best_ask("yes"), 45)
        self.assertEqual(b.best_ask("no"), 60)
        self.assertEqual(b.asks("yes"), [(45, 5), (46, 7)])


class Detect(unittest.TestCase):
    def feed(self, det, rng, t0, secs, jump_bps=0.0, at=None):
        p, sigs = 90000.0, []
        for i in range(secs * 10):
            t = t0 + i / 10
            p *= 1 + rng.gauss(0, 1e-4 * 0.316)
            if at is not None and at <= i / 10 < at + 2:
                p *= 1 + jump_bps / 1e4 / 20
            s = det.on_trade(t, p, 0.05, rng.choice((1, -1)))
            if s:
                sigs.append(s)
        return sigs

    def test_quiet_market_no_signals(self):
        self.assertEqual(self.feed(Detector(SignalCfg()), random.Random(1), 1000, 900), [])

    def test_jump_fires_in_right_direction(self):
        sigs = self.feed(Detector(SignalCfg()), random.Random(2), 1000, 600, jump_bps=-30, at=400)
        self.assertTrue(sigs)
        self.assertEqual(sigs[0].direction, -1)
        self.assertTrue(400 <= sigs[0].ts - 1000 <= 403)


class KalshiBits(unittest.TestCase):
    def test_signature_verifies(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        auth = KalshiAuth(KalshiCfg())
        auth.key, auth.key_id = key, "kid"
        h = auth.headers("POST", "/trade-api/v2/portfolio/orders")
        key.public_key().verify(
            base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
            (h["KALSHI-ACCESS-TIMESTAMP"] + "POST/trade-api/v2/portfolio/orders").encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256())

    def test_parse_market(self):
        m = parse_market({"ticker": "KXBTC15M-X", "close_time": "2026-11-16T12:15:00Z",
                          "open_time": "2026-11-16T12:00:00Z", "strike_type": "greater_or_equal",
                          "floor_strike": 91234.5}, "KXBTC15M", "15m")
        self.assertEqual(m.floor_strike, 91234.5)
        self.assertEqual(m.close_ts - m.open_ts, 900)

    def test_ws_messages_cents_and_dollars(self):
        clock = SimClock(0)
        eng = Engine(Config(), clock, Tracker(":memory:"))
        ws = KalshiWS(KalshiCfg(), None, eng)
        ws._handle({"type": "orderbook_snapshot", "sid": 1, "seq": 1,
                    "msg": {"market_ticker": "A", "yes": [[40, 10]], "no": [[55, 5]]}})
        ws._handle({"type": "orderbook_delta", "sid": 1, "seq": 2,
                    "msg": {"market_ticker": "A", "price_dollars": "0.5600", "delta": 3, "side": "no"}})
        self.assertEqual(eng.books["A"].best_ask("yes"), 44)
        self.assertEqual(ws._handle({"type": "orderbook_delta", "sid": 1, "seq": 9,
                                     "msg": {"market_ticker": "A", "price": 1, "delta": 1, "side": "no"}}),
                         "resync")
        ws._handle({"type": "orderbook_snapshot", "sid": 2, "seq": 1,
                    "msg": {"market_ticker": "B", "yes_dollars": [["0.31", 4]], "no_dollars": []}})
        self.assertEqual(eng.books["B"].best_bid("yes"), 31)


class Feeds(unittest.TestCase):
    def test_parse_iso_any_fraction(self):
        from kbot.models import parse_iso
        base = parse_iso("2026-11-16T12:00:00Z")
        self.assertAlmostEqual(parse_iso("2026-11-16T12:00:00.5Z") - base, 0.5, places=5)
        self.assertAlmostEqual(parse_iso("2026-11-16T12:00:00.123456789Z") - base, 0.123456, places=5)
        self.assertAlmostEqual(parse_iso("2026-11-16T12:00:00.12+00:00") - base, 0.12, places=5)

    def test_coinbase_taker_side(self):
        (t, p, s, side), = parse_coinbase({"type": "match", "time": "2026-11-16T12:00:00.5Z",
                                           "price": "91000.1", "size": "0.2", "side": "sell"})
        self.assertEqual((p, s, side), (91000.1, 0.2, 1))

    def test_kraken(self):
        out = list(parse_kraken({"channel": "trade", "type": "update", "data": [
            {"price": 91000, "qty": 0.1, "side": "sell", "timestamp": "2026-11-16T12:00:00.000Z"}]}))
        self.assertEqual(out[0][3], -1)


if __name__ == "__main__":
    unittest.main()
