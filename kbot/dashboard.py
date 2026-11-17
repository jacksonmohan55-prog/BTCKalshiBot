"""Live terminal dashboard:  python -m kbot dashboard

Runs in its own window. It reads the bot's status.json snapshot (rewritten every 2 s) and the results
database, so opening or closing it never affects the bot."""
from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from datetime import datetime

from rich import box
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .config import Config
from .tracker import horizon_stats

SPARK = "▁▂▃▄▅▆▇█"
GREEN, RED, DIM, ACCENT = "green3", "red1", "grey58", "dark_orange"


def _t(ts: float, secs=False) -> str:
    return datetime.fromtimestamp(ts).strftime("%-I:%M:%S %p" if secs else "%-I:%M %p") \
        if os.name != "nt" else datetime.fromtimestamp(ts).strftime("%I:%M:%S %p" if secs else "%I:%M %p")


def _num(v, fmt="{:.1f}", dash="—"):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return dash
    return fmt.format(v + 0.0 if abs(v) >= 0.05 else 0.0)        # no "-0.0"


def _signed(v, fmt="{:+.2f}", unit=""):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return Text("—", style=DIM)
    if fmt.startswith("$"):                       # money: +$1.23 / -$1.23
        s = ("+" if v >= 0 else "-") + "$" + fmt[1:].replace("+", "").format(abs(v))
    else:
        s = fmt.format(v) + unit
    return Text(s, style=GREEN if v > 0 else RED if v < 0 else "")


def _ago(sec: float) -> str:
    if sec < 90:
        return f"{sec:.0f}s ago"
    if sec < 5400:
        return f"{sec / 60:.0f}m ago"
    return f"{sec / 3600:.1f}h ago"


def _sparkline(values: list[float], width: int) -> Text:
    if len(values) < 2:
        return Text("collecting price history…", style=DIM)
    step = max(1, math.ceil(len(values) / width))
    pts = [values[i] for i in range(0, len(values), step)][-width:]
    lo, hi = min(pts), max(pts)
    span = hi - lo or 1.0
    t = Text()
    for i, v in enumerate(pts):
        prev = pts[i - 1] if i else v
        t.append(SPARK[min(7, int((v - lo) / span * 7.999))], style=GREEN if v >= prev else RED)
    return t


def _market_label(b: dict) -> str:
    close = _t(b["close_ts"])
    if b["horizon"] == "15m" or b["ticker"].startswith("KXBTC15M"):
        return f"Up vs open · {close}"
    st, f, c = b["strike_type"], b["floor"], b["cap"]
    if st.startswith("greater") and f:
        return f"Above ${f:,.0f} · {close}"
    if st.startswith("less"):
        return f"Below ${(c or f):,.0f} · {close}"
    if st == "between" and f and c:
        return f"${f:,.0f}–{c:,.0f} · {close}"
    return f"{b['ticker']} · {close}"


# ------------------------------------------------------------------ data
def load(cfg: Config):
    status = None
    try:
        with open(cfg.status_path) as fh:
            status = json.load(fh)
    except (OSError, ValueError):
        pass
    db = None
    if os.path.exists(cfg.db_path):
        try:
            db = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True, timeout=2)
        except sqlite3.Error:
            db = None
    return status, db


# ------------------------------------------------------------------ panels
def header(status, now) -> Panel:
    t = Text()
    t.append(" kbot ", style=f"bold black on {ACCENT}")
    t.append("  BTC × Kalshi  ", style="bold")
    if status is None:
        t.append("● NOT RUNNING", style=f"bold {RED}")
        t.append("  start it: ./scripts/run_forever.sh", style=DIM)
    else:
        age = now - status["ts"]
        if age > 20:
            t.append(f"● STOPPED?  last update {_ago(age)}", style=f"bold {RED}")
        else:
            t.append("● RUNNING", style=f"bold {GREEN}")
            up = now - status["started"]
            t.append(f"  up {_ago(up).replace(' ago', '')}", style=DIM)
        mode = status["mode"].upper()
        t.append(f"   {mode}", style=f"bold {'red1' if mode == 'LIVE' else 'cyan'}")
        t.append(f" · {status['env']} · feed: {status['feed']}", style=DIM)
        if status.get("warmup_left", 0) > 0:
            t.append(f"   warming up {status['warmup_left']:.0f}s", style="yellow")
    t.append(f"   {_t(now, secs=True)}", style=DIM)
    return Panel(t, box=box.HEAVY, border_style=ACCENT, padding=(0, 1))


def btc_panel(status, now, width) -> Panel:
    if not status or not status.get("btc"):
        return Panel(Text("waiting for BTC trades…", style=DIM), title="Bitcoin", border_style="grey35")
    px = status["btc"]
    hist = status["history"]
    big = Text(f"${px:,.2f}", style="bold white")
    chg = Text()
    for label, secs in (("1m", 60), ("5m", 300), ("15m", 900)):
        past = [p for t, p in hist if t <= now - secs]
        chg.append(f"  {label} ")
        if past:
            bps = (px / past[-1] - 1) * 1e4
            chg.append(f"{bps:+.1f}bp", style=GREEN if bps > 0 else RED if bps < 0 else "")
        else:
            chg.append("—", style=DIM)
    venues = Text()
    for v, age in sorted(status["venues"].items()):
        venues.append("● ", style=GREEN if age < 10 else RED)
        venues.append(f"{v}  ", style=DIM)
    vol = Text(f"vol {status['sigma_bps']:.2f} bp/√s", style=DIM)
    spark = _sparkline([p for _, p in hist], max(10, width - 6))
    return Panel(Group(Text.assemble(big, chg), spark, Text.assemble(venues, vol)),
                 title="Bitcoin · last 30 min", border_style="grey35")


def markets_panel(status, now) -> Panel:
    tbl = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False, header_style="bold")
    tbl.add_column("Bet", no_wrap=True)
    tbl.add_column("Left", justify="right", no_wrap=True)
    tbl.add_column("Bid/Ask", justify="right", no_wrap=True)
    tbl.add_column("Odds", justify="right", no_wrap=True)
    books = sorted((status or {}).get("books", []), key=lambda b: (b["horizon"], b["close_ts"], b["floor"] or 0))
    if not books:
        return Panel(Text("no Kalshi quotes yet…", style=DIM), title="Kalshi markets watched",
                     border_style="grey35")
    last_h = None
    for b in books:
        if b["horizon"] != last_h:
            tbl.add_row(Text(b["horizon"], style=f"bold {ACCENT}"), "", "", "")
            last_h = b["horizon"]
        left = b["close_ts"] - now
        mm = f"{int(left // 60)}:{int(left % 60):02d}" if left > 0 else "closed"
        yb, ya = b["yes_bid"], b["yes_ask"]
        mid = (yb + ya) / 2 if yb is not None and ya is not None else None
        tbl.add_row("  " + _market_label(b), mm,
                    f"{_num(yb, '{:.0f}')}/{_num(ya, '{:.0f}')}¢",
                    Text(_num(mid, "{:.0f}%"), style="bold"))
    return Panel(tbl, title="Kalshi markets watched", border_style="grey35")


def score_panel(db, cfg, status) -> Panel:
    tbl = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False, header_style="bold")
    for col, j in (("Window", "left"), ("Signals", "right"), ("Stale", "right"), ("Bettable", "right"),
                   ("Bets", "right"), ("30s ¢", "right"), ("Win", "right"), ("P&L", "right")):
        tbl.add_column(col, justify=j)
    rows = horizon_stats(db, cfg.markout_offsets_s, cfg.trade.min_edge_cents) if db else []
    total = 0.0
    if not rows:
        horizons = (status or {}).get("horizons") or cfg.horizons
        for h in horizons:
            tbl.add_row(Text(h, style=f"bold {ACCENT}"), "0", "—", "0", "0", "—", "—", "$0.00")
    for r in rows:
        total += r["pnl"]
        tbl.add_row(Text(r["horizon"], style=f"bold {ACCENT}"), str(r["looks"]),
                    _num(r["stale_pct"], "{:.0f}%") if r["looks"] else "—",
                    str(r["bettable"]), str(r["trades"]) + (f" ({r['open_trades']} open)" if r["open_trades"] else ""),
                    _signed(r["mk30s"], "{:+.1f}"), _num(r["win_pct"], "{:.0f}%"), _signed(r["pnl"], "${:+.2f}"))
    foot = Text.assemble(("Total paper P&L ", DIM), _signed(total, "${:+.2f}"),
                         ("   30s ¢ = profit/contract 30 s after a bet", DIM))
    return Panel(Group(tbl, foot), title="Scoreboard · which window works best", border_style="grey35")


def signals_panel(db, limit) -> Panel:
    if not db:
        return Panel(Text("no signals yet", style=DIM), title="Recent signals", border_style="grey35")
    tbl = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False, header_style="bold")
    tbl.add_column("Time")
    tbl.add_column("Move", justify="right")
    tbl.add_column("Why")
    tbl.add_column("Decision", ratio=3)
    sigs = db.execute("SELECT id, ts, direction, reason, ret_bps FROM signals ORDER BY id DESC LIMIT ?",
                      (limit,)).fetchall()
    if not sigs:
        return Panel(Text("no signals yet. BTC has been calm (that's normal)", style=DIM),
                     title="Recent signals", border_style="grey35")
    for sid, ts, d, reason, ret in sigs:
        dec = Text()
        for h, traded, skip, edge in db.execute(
                "SELECT horizon, traded, skip_reason, edge FROM observations WHERE signal_id=? ORDER BY horizon",
                (sid,)):
            dec.append(f"{h} ", style=f"bold {ACCENT}")
            if traded:
                dec.append(f"BET +{edge:.1f}¢  ", style=f"bold {GREEN}")
            elif skip and skip.startswith("edge"):
                dec.append(f"skip, edge {_num(edge)}¢  ", style=DIM)
            else:
                dec.append(f"skip, {skip or 'no market'}  ", style=DIM)
        arrow = Text(("▲ " if d > 0 else "▼ ") + f"{abs(ret):.1f}bp", style=GREEN if d > 0 else RED)
        tbl.add_row(_t(ts, secs=True), arrow, "volume" if reason == "volume" else "jump", dec)
    return Panel(tbl, title="Recent signals", border_style="grey35")


def trades_panel(db, limit) -> Panel:
    rows = db.execute("SELECT ts, horizon, ticker, side, qty, price, result, pnl_cents FROM trades "
                      "ORDER BY id DESC LIMIT ?", (limit,)).fetchall() if db else []
    if not rows:
        return Panel(Text("no paper bets yet", style=DIM), title="Recent bets", border_style="grey35")
    tbl = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False, header_style="bold")
    for c in ("Time", "Window", "Bet", "Size", "Price", "Result"):
        tbl.add_column(c, justify="right" if c in ("Size", "Price", "Result") else "left")
    for ts, h, tk, side, qty, price, result, pnl in rows:
        res = Text("open", style="yellow") if result is None else _signed(pnl / 100, "${:+.2f}")
        tbl.add_row(_t(ts), h, Text(side.upper(), style=GREEN if side == "yes" else RED),
                    f"{qty:g}", f"{price:.1f}¢", res)
    return Panel(tbl, title="Recent bets", border_style="grey35")


def positions_panel(status, db) -> Panel:
    pos = (status or {}).get("positions") or []
    if not pos:
        return Panel(Text("no open bets. When the bot bets, they show here until the market settles",
                          style=DIM), title="Open bets", border_style="grey35")
    tbl = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False, header_style="bold")
    for c in ("Market", "Side", "Contracts", "Cost"):
        tbl.add_column(c, justify="left" if c == "Market" else "right", no_wrap=True)
    for p in pos:
        tbl.add_row(p["ticker"], Text(p["side"].upper(), style=GREEN if p["side"] == "yes" else RED),
                    f"{p['qty']:g}", f"${p['cost']:.2f}")
    return Panel(tbl, title="Open bets", border_style="grey35")


def render(cfg: Config, width: int, height: int):
    status, db = load(cfg)
    now = time.time()
    try:
        n_books = len((status or {}).get("books", []))
        mk_h = min(max(6, n_books + len(cfg.horizons) + 6), 18)
        lay = Layout()
        if width >= 130:
            lay.split_column(Layout(header(status, now), size=3), Layout(name="body"))
            left, right = Layout(name="l", ratio=4), Layout(name="r", ratio=5)
            lay["body"].split_row(left, right)
            left.split_column(Layout(btc_panel(status, now, width * 4 // 9), size=5),
                              Layout(markets_panel(status, now), size=mk_h),
                              Layout(positions_panel(status, db)))
            sig_rows = max(3, height - 3 - 11 - 9)
            right.split_column(Layout(score_panel(db, cfg, status), size=4 + max(2, len(cfg.horizons)) + 3),
                               Layout(signals_panel(db, sig_rows)), Layout(trades_panel(db, 5), size=9))
        else:
            lay.split_column(Layout(header(status, now), size=3),
                             Layout(btc_panel(status, now, width), size=5),
                             Layout(score_panel(db, cfg, status), size=4 + max(2, len(cfg.horizons)) + 3),
                             Layout(markets_panel(status, now), size=mk_h),
                             Layout(signals_panel(db, max(3, height - 3 - 5 - 9 - mk_h - 4))),
                             )
        return lay
    finally:
        if db:
            db.close()


def run(cfg: Config, once: bool = False, refresh: float = 2.0, svg: str | None = None):
    console = Console(record=bool(svg))
    if once:
        w, h = console.size
        console.print(render(cfg, w, h), height=h)
        if svg:
            console.save_svg(svg, title="kbot dashboard")
        return
    try:
        with Live(render(cfg, *console.size), console=console, screen=True, auto_refresh=False) as live:
            while True:
                time.sleep(refresh)
                live.update(render(cfg, *console.size), refresh=True)
    except KeyboardInterrupt:
        pass
