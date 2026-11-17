"""Trade review:  python -m kbot review [--all] [--csv trades.csv]

Grades every paper/live bet, shows which kinds of bets win and lose, and checks whether skipped
signals would have paid. Reads the results database only."""
from __future__ import annotations

import csv
import math
import os
import re
import sqlite3
import statistics
from datetime import datetime

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .config import Config

GREEN, RED, DIM, ACCENT, YELLOW = "green3", "red1", "grey58", "dark_orange", "yellow"
MIN_GROUP = 10            # groups smaller than this are flagged as too small to judge

VERDICTS = {
    "good": (GREEN, "won, and the price moved our way right after"),
    "lucky": (YELLOW, "won, but the price moved against us right after"),
    "unlucky": (YELLOW, "lost, but the price moved our way right after (a fair bet that missed)"),
    "bad": (RED, "lost, and the price moved against us right away (the bot misjudged it)"),
    "win": (GREEN, "won (no 30 s price check recorded)"),
    "loss": (RED, "lost (no 30 s price check recorded)"),
    "open": (DIM, "not settled yet"),
}


def _when(ts: float) -> str:
    fmt = "%b %d %I:%M %p"
    return datetime.fromtimestamp(ts).strftime(fmt).replace(" 0", " ")


def _money(cents: float | None) -> Text:
    if cents is None:
        return Text("—", style=DIM)
    v = cents / 100
    return Text(("+" if v >= 0 else "-") + f"${abs(v):.2f}", style=GREEN if v > 0 else RED if v < 0 else "")


def _c(v: float | None, signed=True) -> Text:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return Text("—", style=DIM)
    s = f"{v:+.1f}¢" if signed else f"{v:.1f}¢"
    return Text(s, style=(GREEN if v > 0 else RED if v < 0 else "") if signed else "")


# ------------------------------------------------------------------ data
def load_trades(db) -> list[dict]:
    q = db.execute
    rows = q("""SELECT t.id, t.ts, t.horizon, t.ticker, t.side, t.qty, t.price, t.fee, t.result, t.pnl_cents,
                       o.id, o.edge, o.fair, o.stale, o.secs_to_close, s.reason, s.ret_bps, s.z_ret, s.z_vol
                FROM trades t JOIN observations o ON o.id = t.obs_id JOIN signals s ON s.id = o.signal_id
                ORDER BY t.ts""").fetchall()
    out = []
    for (tid, ts, h, tk, side, qty, price, fee, result, pnl, oid, edge, fair, stale, ttc,
         reason, ret, zr, zv) in rows:
        snaps = {off: bid for off, bid in q("SELECT offset, bid FROM snapshots WHERE obs_id=?", (oid,))}
        mk = {k: (snaps[k] - price if snaps.get(k) is not None else None) for k in (5, 30, 60)}
        if result is None:
            verdict = "open"
        else:
            won = pnl is not None and pnl > 0
            m30 = mk[30]
            if m30 is None:
                verdict = "win" if won else "loss"
            elif won:
                verdict = "good" if m30 >= 0 else "lucky"
            else:
                verdict = "unlucky" if m30 >= 0 else "bad"
        out.append({
            "id": tid, "ts": ts, "when": _when(ts), "horizon": h, "ticker": tk, "side": side, "qty": qty,
            "price": price, "fee": fee, "result": result, "pnl_cents": pnl,
            "pnl_per_contract": (pnl / qty if pnl is not None and qty else None),
            "edge": edge, "fair": fair * 100 if fair is not None else None, "stale": bool(stale),
            "secs_to_close": ttc, "reason": reason, "move_bps": abs(ret), "z_move": abs(zr), "z_volume": zv,
            "mk5": mk[5], "mk30": mk[30], "mk60": mk[60], "verdict": verdict,
        })
    return out


def bucket(v, edges, fmt):
    if v is None:
        return "unknown"
    for lo, hi in zip(edges, edges[1:]):
        if lo <= v < hi:
            return fmt(lo, hi)
    return fmt(edges[-1], None)


def _range(unit=""):
    def f(lo, hi):
        if hi is None:
            return f"{lo:g}{unit}+"
        return f"{lo:g}–{hi:g}{unit}"
    return f


def _mins(lo, hi):
    if hi is None:
        return f"{lo / 60:g}m+ left"
    return f"{lo / 60:g}–{hi / 60:g}m left"


GROUPINGS = [
    ("Window", lambda t: t["horizon"]),
    ("Signal type", lambda t: t["reason"]),
    ("Edge at entry", lambda t: bucket(t["edge"], [3, 5, 10, 20], _range("¢"))),
    ("Price paid", lambda t: bucket(t["price"], [0, 30, 50, 70, 85], _range("¢"))),
    ("BTC move size", lambda t: bucket(t["move_bps"], [0, 5, 10, 20], _range("bp"))),
    ("Time left", lambda t: bucket(t["secs_to_close"], [0, 120, 300, 600, 1800], _mins)),
    ("Kalshi stale at entry", lambda t: "yes" if t["stale"] else "no"),
    ("Bet side", lambda t: t["side"].upper()),
]


def group_stats(trades, key):
    groups: dict[str, list[dict]] = {}
    for t in trades:
        groups.setdefault(key(t), []).append(t)
    out = []
    for name, ts in groups.items():
        settled = [t for t in ts if t["result"] is not None]
        ctr = sum(t["qty"] for t in settled)
        pnl = sum(t["pnl_cents"] for t in settled)
        mk30 = [t["mk30"] for t in ts if t["mk30"] is not None]
        out.append({"name": name, "n": len(ts), "settled": len(settled),
                    "win": (sum(1 for t in settled if t["pnl_cents"] > 0) / len(settled) * 100) if settled else None,
                    "per_ctr": pnl / ctr if ctr else None, "pnl": pnl,
                    "mk30": statistics.mean(mk30) if mk30 else None})
    return sorted(out, key=lambda g: _order(g["name"]))


def _order(name: str):
    """Sort numeric buckets by their lower bound ('3–5¢' before '10–20¢'); 'unknown' last."""
    if name == "unknown":
        return (2, 0.0, name)
    if name.startswith("below"):
        return (0, -1e18, name)
    m = re.match(r"[≥]?([\d.]+)", name)
    return (0, float(m.group(1)), name) if m else (1, 0.0, name)


def skipped_stats(db, min_edge: float) -> list[dict]:
    """Signals the bot looked at but didn't bet on: what a bet at the ask would have been worth 30 s later."""
    rows = db.execute("""SELECT o.id, o.edge, o.ask0, o.skip_reason FROM observations o
                         WHERE o.traded = 0 AND o.ask0 IS NOT NULL""").fetchall()
    groups: dict[str, list[float]] = {}
    for oid, edge, ask0, skip in rows:
        r = db.execute("SELECT bid FROM snapshots WHERE obs_id=? AND offset=30", (oid,)).fetchone()
        if not r or r[0] is None or edge is None:
            continue
        if edge >= min_edge:
            name = f"≥{min_edge:g}¢, skipped: {skip or 'order did not fill'}"
        else:
            name = bucket(edge, [-1e9, 0, 1, 2, min_edge], lambda lo, hi: "below 0¢" if lo < -1e8 else
                          (f"{lo:g}–{hi:g}¢" if hi is not None else f"{lo:g}¢+"))
        groups.setdefault(name, []).append(r[0] - ask0)
    return sorted(({"name": k, "n": len(v), "mk30": statistics.mean(v)} for k, v in groups.items()),
                  key=lambda r: _order(r["name"]))


# ------------------------------------------------------------------ output
def review(cfg: Config, show_all: bool = False, csv_path: str | None = None, console: Console | None = None):
    console = console or Console()
    if not os.path.exists(cfg.db_path):
        console.print(f"No results database at {cfg.db_path} yet. Let the bot run first.")
        return
    db = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True, timeout=5)
    trades = load_trades(db)
    if csv_path:
        cols = ["when", "horizon", "ticker", "side", "qty", "price", "fee", "edge", "fair", "stale",
                "secs_to_close", "reason", "move_bps", "z_move", "z_volume", "mk5", "mk30", "mk60",
                "result", "pnl_cents", "pnl_per_contract", "verdict"]
        with open(csv_path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows({k: (round(v, 2) if isinstance(v, float) else v) for k, v in t.items()} for t in trades)
        console.print(f"Saved {len(trades)} trades to {csv_path} (open it in Numbers or Excel).\n")
    if not trades:
        console.print("No bets yet. Run this again once the bot has made some.")
        _skipped_panel(console, db, cfg)
        return

    settled = [t for t in trades if t["result"] is not None]
    pnl = sum(t["pnl_cents"] for t in settled)
    counts = {v: sum(1 for t in trades if t["verdict"] == v) for v in VERDICTS}

    # --- summary
    s = Text()
    s.append(f"{len(trades)} bets", style="bold")
    s.append(f" · {len(settled)} settled · ")
    s.append_text(_money(pnl))
    s.append(" total")
    if settled:
        s.append(f" · {sum(1 for t in settled if t['pnl_cents'] > 0) / len(settled) * 100:.0f}% won\n\n")
    else:
        s.append("\n\n")
    for v in ("good", "lucky", "unlucky", "bad", "win", "loss", "open"):
        if counts[v]:
            style, desc = VERDICTS[v]
            s.append(f"{counts[v]:>4} ", style="bold")
            s.append(f"{v:<8}", style=f"bold {style}")
            s.append(f" {desc}\n", style=DIM)
    s.append("\nOnly 'bad' bets are real mistakes; 'unlucky' bets were reasonable and just missed.", style=DIM)
    console.print(Panel(s, title="Trade review", border_style=ACCENT))

    # --- every trade
    shown = trades if show_all else trades[-25:]
    tbl = Table(box=box.SIMPLE_HEAD, header_style="bold", pad_edge=False,
                title=None if show_all or len(trades) <= 25 else f"latest 25 of {len(trades)} (use --all for every bet)",
                title_style=DIM)
    wide = console.width >= 100
    cols = [("When", "left"), ("Win", "left")] + ([("Signal", "left")] if wide else []) + \
        [("Bet", "left"), ("Paid", "right"), ("Edge", "right"), ("30s", "right"), ("Result", "right"),
         ("Grade", "left")]
    for col, j in cols:
        tbl.add_column(col, justify=j, no_wrap=True)
    for t in reversed(shown):
        style = VERDICTS[t["verdict"]][0]
        when = t["when"] if wide else datetime.fromtimestamp(t["ts"]).strftime("%m/%d %H:%M")
        cells = [when, t["horizon"]]
        if wide:
            cells.append(Text(f"{t['reason']} {t['move_bps']:.0f}bp"))
        cells += [Text(f"{t['side'].upper()}" + (f" ×{t['qty']:g}" if wide else ""),
                       style=GREEN if t["side"] == "yes" else RED),
                  f"{t['price']:.0f}¢", _c(t["edge"], signed=False), _c(t["mk30"]),
                  _money(t["pnl_cents"]) if t["result"] else Text("open", style=DIM),
                  Text(t["verdict"], style=f"bold {style}")]
        tbl.add_row(*cells)
    console.print(Panel(tbl, title="Bets (newest first)", border_style="grey35"))

    # --- best / worst
    if len(settled) >= 2:
        ranked = sorted(settled, key=lambda t: t["pnl_cents"])
        lines = Text()
        for label, t in (("Best ", ranked[-1]), ("Worst", ranked[0])):
            lines.append(f"{label} ", style="bold")
            lines.append_text(_money(t["pnl_cents"]))
            lines.append(f"  {t['when']} · {t['horizon']} {t['side'].upper()} at {t['price']:.0f}¢ "
                         f"after a {t['move_bps']:.0f}bp {t['reason']} signal, edge {t['edge']:.1f}¢, "
                         f"{(t['secs_to_close'] or 0) / 60:.0f} min left\n")
        console.print(Panel(lines, title="Best and worst bet", border_style="grey35"))

    # --- what separates winners from losers
    tbl = Table(box=box.SIMPLE_HEAD, header_style="bold", pad_edge=False)
    for col, j in (("Group", "left"), ("Bets", "right"), ("Won", "right"), ("Per contract", "right"),
                   ("Total", "right"), ("30s check", "right")):
        tbl.add_column(col, justify=j, no_wrap=True)
    findings = []
    for title, key in GROUPINGS:
        gs = group_stats(trades, key)
        if len(gs) < 2:
            continue
        tbl.add_row(Text(title, style=f"bold {ACCENT}"), "", "", "", "", "")
        for g in gs:
            small = g["settled"] < MIN_GROUP
            row_style = DIM if small else None
            tbl.add_row("  " + g["name"] + (" (few)" if small else ""), str(g["n"]),
                        f"{g['win']:.0f}%" if g["win"] is not None else "—",
                        _c(g["per_ctr"]), _money(g["pnl"]), _c(g["mk30"]), style=row_style)
            if not small and g["per_ctr"] is not None:
                findings.append((g["per_ctr"], title, g))
    console.print(Panel(tbl, title="What separates winners from losers",
                        subtitle="per contract = average profit per $1 contract · (few) = under "
                                 f"{MIN_GROUP} settled bets, too few to judge", border_style="grey35"))

    # --- takeaways
    t = Text()
    if findings:
        findings.sort(key=lambda f: f[0])
        worst, best = findings[0], findings[-1]
        t.append("Strongest group: ", style="bold")
        t.append(f"{best[1]} = {best[2]['name']}  ({_c(best[0]).plain} per contract over {best[2]['settled']} bets)\n")
        t.append("Weakest group:   ", style="bold")
        t.append(f"{worst[1]} = {worst[2]['name']}  ({_c(worst[0]).plain} per contract over {worst[2]['settled']} bets)\n")
        if worst[0] < 0:
            t.append("\nIf the weakest group stays negative as more bets come in, it's a candidate to filter out.",
                     style=DIM)
    else:
        t.append(f"Not enough settled bets yet: every group has fewer than {MIN_GROUP}. "
                 "Patterns here are mostly luck for now.", style=DIM)
    console.print(Panel(t, title="Takeaways", border_style="grey35"))
    _skipped_panel(console, db, cfg)


def _skipped_panel(console, db, cfg):
    rows = skipped_stats(db, cfg.trade.min_edge_cents)
    if not rows:
        return
    tbl = Table(box=box.SIMPLE_HEAD, header_style="bold", pad_edge=False)
    tbl.add_column("Edge when skipped")
    tbl.add_column("Signals", justify="right")
    tbl.add_column("Value 30s later", justify="right")
    for r in rows:
        tbl.add_row(r["name"], str(r["n"]), _c(r["mk30"]), style=DIM if r["n"] < MIN_GROUP else None)
    console.print(Panel(tbl, title="Skipped signals: would they have paid?",
                        subtitle="value 30s later = price you could sell at 30 s later minus the price "
                                 "you'd have paid (before fees)", border_style="grey35"))
