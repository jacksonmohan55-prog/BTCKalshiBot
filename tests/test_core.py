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

    def test_implied_sigma_round_trip(self):
        from kbot.pricing import implied_sigma
        m = Market("X", "S", "h", close_ts=1000, strike_type="greater", floor_strike=65250)
        target = fair_yes(m, 65276, 1.7e-4, 0)
        self.assertAlmostEqual(implied_sigma(m, 65276, 0, target), 1.7e-4, delta=1e-7)
        self.assertIsNone(implied_sigma(m, 65276, 0, 0.2))   # can't be below 50% when spot > strike

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


class KalshiFormats(unittest.TestCase):
    def test_fixed_point_dollar_levels(self):
        from kbot.kalshi import parse_levels, parse_delta
        ob = {"yes_dollars": [["0.0010", "1803365.63"], ["0.8400", "8616.74"]],
              "no_dollars": [["0.1400", "10583.62"]]}
        self.assertEqual(parse_levels(ob, "yes")[0], (0.1, 1803365.63))
        b = OrderBook()
        for p, q in parse_levels(ob, "yes"):
            b.set_level("yes", p, q, 1)
        for p, q in parse_levels(ob, "no"):
            b.set_level("no", p, q, 1)
        self.assertAlmostEqual(b.best_ask("yes"), 86.0)
        self.assertAlmostEqual(b.best_ask("no"), 16.0)
        self.assertEqual(parse_delta({"price_dollars": "0.4150", "delta_fp": "-2.50"}), (41.5, -2.5))

    def test_poller_picks_soonest_nearest_strikes(self):
        from kbot.kalshi import KalshiPoller
        clock = SimClock(0)
        cfg = Config()
        cfg.kalshi.poll_markets_per_series = 2
        eng = Engine(cfg, clock, Tracker(":memory:"))
        eng.price = 65000
        for k in (60000, 64900, 65050, 70000):
            eng.markets[f"H1-{k}"] = Market(f"H1-{k}", "KXBTCD", "60m", close_ts=3600, floor_strike=k)
        eng.markets["H2-65000"] = Market("H2-65000", "KXBTCD", "60m", close_ts=7200, floor_strike=65000)
        picked = KalshiPoller(cfg.kalshi, None, eng, 30).pick()
        self.assertEqual(sorted(picked), ["H1-64900", "H1-65050"])

    def test_websocket_mode_polls_only_markets_it_isnt_updating(self):
        from kbot.kalshi import KalshiPoller
        clock = SimClock(100)
        cfg = Config()
        eng = Engine(cfg, clock, Tracker(":memory:"))
        eng.price = 65000
        eng.markets["Q"] = Market("Q", "KXBTC15M", "15m", close_ts=900, floor_strike=65000)
        eng.markets["H"] = Market("H", "KXBTCD", "60m", close_ts=3600, floor_strike=65000)
        eng.book_snapshot("Q", [(50.0, 10)], [(49.0, 10)])          # websocket keeps 15m fresh
        p = KalshiPoller(cfg.kalshi, None, eng, 30)
        p.skip_fresh_s = 5
        self.assertEqual(p.pick(), ["H"])                           # 60m never arrived -> poll it
        clock.advance(110)
        self.assertEqual(sorted(p.pick()), ["H", "Q"])              # 15m went quiet too


class SignalPricing(unittest.TestCase):
    def test_at_the_money_small_move_is_not_a_sure_thing(self):
        """Replays the live 21:49 signal: +2.1 bps, ATM 15m market at 46/47. The old implied-vol
        pricing called YES 98.5c; fair must stay near Kalshi's pre-move mid plus a small delta."""
        from kbot.models import Signal
        clock = SimClock(1000)
        eng = Engine(Config(), clock, Tracker(":memory:"))
        m = Market("M15", "KXBTC15M", "15m", close_ts=1000 + 640, strike_type="greater_or_equal",
                   floor_strike=65265.0)
        eng.upsert_market(m)
        eng.book_snapshot("M15", [(46.0, 500)], [(53.0, 500)])
        clock.advance(1010)
        eng.handle_signal(Signal(ts=1010, direction=1, reason="volume", price=65278.79,
                                 price_before=65265.1, ret_bps=2.1, z_ret=2.8, z_vol=5.0, sigma_1s=0.3e-4))
        fair, = eng.tracker.db.execute("SELECT fair FROM observations WHERE horizon='15m'").fetchone()
        self.assertGreater(fair, 0.465)
        self.assertLess(fair, 0.60)

    def test_already_repriced_market_has_no_edge(self):
        from kbot.models import Signal
        clock = SimClock(1000)
        eng = Engine(Config(), clock, Tracker(":memory:"))
        eng.upsert_market(Market("M", "S", "15m", close_ts=1600, floor_strike=65000.0))
        eng.book_snapshot("M", [(49.0, 500)], [(50.0, 500)])          # 49/50 before the move
        clock.advance(1008)
        eng.book_snapshot("M", [(84.0, 500)], [(15.0, 500)])          # Kalshi already moved to 84/85
        clock.advance(1010)
        eng.handle_signal(Signal(ts=1010, direction=1, reason="jump", price=65100, price_before=65000,
                                 ret_bps=15.4, z_ret=6, z_vol=1, sigma_1s=0.6e-4))
        edge, = eng.tracker.db.execute("SELECT edge FROM observations").fetchone()
        self.assertLess(edge, 3.0)


class Dashboard(unittest.TestCase):
    def test_renders_with_and_without_data(self):
        import io, json, os, tempfile, time
        from rich.console import Console
        from kbot.dashboard import render
        from kbot.models import Signal
        d = tempfile.mkdtemp()
        cfg = Config()
        cfg.db_path, cfg.status_path = os.path.join(d, "k.sqlite"), os.path.join(d, "status.json")
        for width in (80, 140):                                   # nothing exists yet
            Console(file=io.StringIO(), width=width, height=40).print(render(cfg, width, 40))
        tr = Tracker(cfg.db_path)
        sid = tr.record_signal(Signal(time.time(), 1, "jump", 65100, 65000, 15.4, 6, 1, 6e-5))
        oid = tr.record_obs(sid, "15m", "KXBTC15M-X", "yes", 55, 54, 0.7, 12.0, 1, 500, None)
        tr.record_trade(oid, time.time(), "KXBTC15M-X", "15m", "yes", 10, 55, 18, "paper", "p1")
        now = time.time()
        json.dump({"ts": now, "started": now - 300, "pid": 1, "mode": "paper", "env": "prod",
                   "feed": "websocket", "btc": 65100.0, "sigma_bps": 0.6, "venues": {"coinbase": 1.0},
                   "warmup_left": 0, "history": [[now - 600 + i * 5, 65000 + i] for i in range(120)],
                   "books": [{"ticker": "KXBTC15M-X", "horizon": "15m", "close_ts": now + 500,
                              "strike_type": "greater_or_equal", "floor": 65000.0, "cap": None,
                              "yes_bid": 54.0, "yes_ask": 55.0, "iv_bps": 0.6}],
                   "positions": [{"ticker": "KXBTC15M-X", "side": "yes", "qty": 10, "cost": 5.68}],
                   "realized_today": 0, "horizons": cfg.horizons, "min_edge": 3}, open(cfg.status_path, "w"))
        for width in (80, 140):
            buf = io.StringIO()
            Console(file=buf, width=width, height=40).print(render(cfg, width, 40))
            out = buf.getvalue()
            self.assertIn("RUNNING", out)
            self.assertIn("65,100.00", out)
            self.assertIn("BET +12.0", out)


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
