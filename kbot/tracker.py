"""Persists every signal, every per-horizon look at Kalshi, quote snapshots after the signal,
and every trade, then scores horizons against each other."""
from __future__ import annotations

import sqlite3
import statistics
from dataclasses import asdict

from .models import Signal

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY, ts REAL, direction INT, reason TEXT, price REAL, price_before REAL,
  ret_bps REAL, z_ret REAL, z_vol REAL, sigma_1s REAL, run TEXT);
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY, signal_id INT, horizon TEXT, ticker TEXT, side TEXT,
  ask0 REAL, bid0 REAL, fair REAL, edge REAL, stale INT, secs_to_close REAL, traded INT DEFAULT 0,
  skip_reason TEXT);
CREATE TABLE IF NOT EXISTS snapshots (obs_id INT, offset REAL, bid REAL, ask REAL);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY, obs_id INT, ts REAL, ticker TEXT, horizon TEXT, side TEXT,
  qty INT, price REAL, fee REAL, mode TEXT, order_id TEXT, result TEXT, pnl_cents REAL);
"""


class Tracker:
    def __init__(self, path: str, run: str = ""):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self.run = run

    def record_signal(self, s: Signal) -> int:
        d = asdict(s)
        cur = self.db.execute(
            "INSERT INTO signals(ts,direction,reason,price,price_before,ret_bps,z_ret,z_vol,sigma_1s,run)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (d["ts"], d["direction"], d["reason"], d["price"], d["price_before"], d["ret_bps"],
             d["z_ret"], d["z_vol"], d["sigma_1s"], self.run))
        self.db.commit()
        return cur.lastrowid

    def record_obs(self, signal_id, horizon, ticker, side, ask0, bid0, fair, edge, stale,
                   secs_to_close, skip_reason) -> int:
        cur = self.db.execute(
            "INSERT INTO observations(signal_id,horizon,ticker,side,ask0,bid0,fair,edge,stale,"
            "secs_to_close,skip_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (signal_id, horizon, ticker, side, ask0, bid0, fair, edge, int(stale), secs_to_close,
             skip_reason))
        self.db.commit()
        return cur.lastrowid

    def record_snapshot(self, obs_id, offset, bid, ask):
        self.db.execute("INSERT INTO snapshots VALUES(?,?,?,?)", (obs_id, offset, bid, ask))
        self.db.commit()

    def record_trade(self, obs_id, ts, ticker, horizon, side, qty, price, fee, mode, order_id) -> int:
        cur = self.db.execute(
            "INSERT INTO trades(obs_id,ts,ticker,horizon,side,qty,price,fee,mode,order_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (obs_id, ts, ticker, horizon, side, qty, price, fee, mode, order_id))
        self.db.execute("UPDATE observations SET traded=1 WHERE id=?", (obs_id,))
        self.db.commit()
        return cur.lastrowid

    def settle(self, ticker: str, result: str) -> float:
        """Mark all trades in a market settled. Returns realized P&L in cents."""
        total = 0.0
        rows = self.db.execute(
            "SELECT id, side, qty, price, fee FROM trades WHERE ticker=? AND result IS NULL",
            (ticker,)).fetchall()
        for tid, side, qty, price, fee in rows:
            payout = 100.0 * qty if side == result else 0.0
            pnl = payout - price * qty - fee
            total += pnl
            self.db.execute("UPDATE trades SET result=?, pnl_cents=? WHERE id=?", (result, pnl, tid))
        self.db.commit()
        return total

    # ------------------------------------------------------------------ reporting
    def report(self, offsets: list[float], min_edge: float) -> str:
        rows = horizon_stats(self.db, offsets, min_edge)
        n_sig = self.db.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        lines = [f"signals: {n_sig}", ""]
        hdr = (f"{'horizon':<8}{'looks':>6}{'stale%':>7}{'edge>=min':>10}{'avgEdge':>8}"
               f"{'lag_s':>7}{'trades':>7}{'ctr':>6}{'mk5s':>7}{'mk30s':>7}"
               f"{'settled':>8}{'win%':>6}{'PnL$':>9}{'ROI%':>7}")
        lines += [hdr, "-" * len(hdr)]
        for r in rows:
            lines.append(
                f"{r['horizon']:<8}{r['looks']:>6}{r['stale_pct']:>7.0f}{r['bettable']:>10}{r['avg_edge']:>8.1f}"
                f"{r['lag_s']:>7.1f}{r['trades']:>7}{r['contracts']:>6.0f}{r['mk5s']:>7.1f}{r['mk30s']:>7.1f}"
                f"{r['settled']:>8}{r['win_pct']:>6.0f}{r['pnl']:>9.2f}{r['roi_pct']:>7.1f}")
        lines += ["",
                  "looks=signals where a quotable market existed; stale%=Kalshi ask unchanged since the move began;",
                  "avgEdge=fair-ask-fee (cents) when tradable; lag_s=median secs for Kalshi to close half the gap;",
                  "mk5s/mk30s=avg (bid at +N s - entry price) cents/contract; win%/PnL on settled trades."]
        return "\n".join(lines)


def horizon_stats(db, offsets: list[float], min_edge: float) -> list[dict]:
    """Per-horizon scoreboard. `db` is any sqlite3 connection to the tracker database."""
    nan = float("nan")
    q = db.execute
    out = []
    for (h,) in q("SELECT DISTINCT horizon FROM observations ORDER BY horizon").fetchall():
        obs = q("SELECT id, ask0, fair, edge, stale FROM observations WHERE horizon=? AND ask0 IS NOT NULL",
                (h,)).fetchall()
        looks = len(obs)
        good = [o for o in obs if o[3] is not None and o[3] >= min_edge]
        # repricing lag: first snapshot where the ask closed half the gap to fair value
        lags = []
        for oid, ask0, fair, edge, _ in good:
            gap = fair * 100 - ask0
            for off, ask in q("SELECT offset, ask FROM snapshots WHERE obs_id=? ORDER BY offset", (oid,)):
                if ask is not None and ask - ask0 >= gap / 2:
                    lags.append(off)
                    break
            else:
                lags.append(offsets[-1] if offsets else nan)
        tr = q("SELECT obs_id, qty, price, fee, result, pnl_cents FROM trades WHERE horizon=?", (h,)).fetchall()

        def markout(offset):
            vals = []
            for obs_id, qty, price, fee, *_ in tr:
                r = q("SELECT bid FROM snapshots WHERE obs_id=? AND offset=?", (obs_id, offset)).fetchone()
                if r and r[0] is not None:
                    vals.append(r[0] - price)
            return statistics.mean(vals) if vals else nan

        settled = [t for t in tr if t[4] is not None]
        wins = sum(1 for t in settled if t[5] > 0)
        pnl = sum(t[5] for t in settled) / 100
        cost = sum(t[1] * t[2] + t[3] for t in settled) / 100
        out.append({
            "horizon": h, "looks": looks,
            "stale_pct": sum(o[4] for o in obs) / looks * 100 if looks else 0.0,
            "bettable": len(good),
            "avg_edge": statistics.mean(o[3] for o in good) if good else nan,
            "lag_s": statistics.median(lags) if lags else nan,
            "trades": len(tr), "contracts": sum(t[1] for t in tr),
            "mk5s": markout(5), "mk30s": markout(30),
            "settled": len(settled), "wins": wins,
            "win_pct": wins / len(settled) * 100 if settled else nan,
            "pnl": pnl, "roi_pct": pnl / cost * 100 if cost else nan,
            "open_trades": len(tr) - len(settled),
        })
    return out
