# kbot: BTC move → stale Kalshi quote bot

Watches live BTC/USD trades (Coinbase + Kraken) and the Kalshi orderbooks for BTC contracts at several
horizons. When BTC makes an abnormal jump or volume spike, it reprices every open Kalshi market and buys
the side the move favors, but only while Kalshi's ask still lags the new fair value by more than fees plus a
minimum edge. Every signal is logged per horizon, along with what Kalshi did in the 60 s afterwards, so you
can see which horizon reprices slowest and pays best.

## How a trade happens

1. **Detect** (`kbot/signals.py`). Trades go into 1 s bars. A signal fires when the 5 s log return is ≥ 4σ of a
   normal 5 s move *and* ≥ 8 bps, or when 5 s volume is ≥ 5σ above normal and price moved ≥ 2σ in the direction of
   net taker flow.
2. **Price** (`kbot/pricing.py`). For each open market:
   `P(yes) = Φ(ln(S/K) / (σ₁ₛ·√seconds_left))` (with range and "less than" variants). σ is the volatility
   *implied by Kalshi's own quote* at the pre-move BTC price, so the model agrees with Kalshi until BTC moves
   and the only edge it can find is the move Kalshi hasn't priced in yet. The side whose probability rose is the bet.
3. **Edge gate**. `edge = fair − ask − taker fee` (fee = ⌈0.07·C·P·(1−P)⌉). The market with the best edge in each
   horizon is chosen. It's bought only if edge ≥ `min_edge_cents`. If Kalshi has already repriced, there's no edge
   and no bet.
4. **Risk** (`kbot/engine.py`). Per-market cooldown and size cap, total exposure cap, daily loss stop, never holds
   both sides, nothing within 30 s of close.
5. **Execute** (`kbot/broker.py`). Live mode sends an immediate-or-cancel limit at ask + 1¢. Paper mode fills
   against the real Kalshi book after a simulated 150 ms delay.
6. **Track** (`kbot/tracker.py`, SQLite). Signals, per-horizon observations, Kalshi quotes at +1/3/5/10/30/60 s,
   fills, and settlement using Kalshi's official result (polled through REST).

## Paper mode without an API key

With no key configured, `python -m kbot run` polls Kalshi's public orderbooks over REST (once a second,
for the 3 markets per horizon nearest the money) instead of using the authenticated websocket. It can't
trade, so `env = "prod"` is safe and gives real liquidity. A key gives the faster websocket feed and is
required for `--live`.

## Setup

Needs Python 3.9 or newer.

```bash
pip install -r requirements.txt
cp config.example.toml config.toml
export KALSHI_KEY_ID=...                       # Kalshi → Account → API keys
export KALSHI_PRIVATE_KEY_PATH=~/kalshi.pem
```

Start with `env = "demo"` (fake money). Use separate API keys for demo and prod.

## Commands

```bash
python -m kbot discover          # list Kalshi BTC series + open market counts; put the right tickers in [horizons]
python -m kbot run               # paper trading on live data (default)
python -m kbot report            # per-horizon scoreboard
python -m kbot sim --hours 6     # offline simulation, no network or keys needed
KBOT_CONFIRM_LIVE=yes python -m kbot run --live   # real orders
```

## Running for days

`scripts/run_forever.sh` (Mac/Linux) and `scripts/run_forever.bat` (Windows) start paper mode, log to
`kbot.log`, and restart it if it ever exits. Results accumulate in `kbot.sqlite` across restarts.

- **Your own computer:** turn sleep off while it runs (Windows: Settings → System → Power → Sleep → Never;
  Mac: run it as `caffeinate -i ./scripts/run_forever.sh`). Check in with `python -m kbot report`.
- **A Linux server (always on):** use `scripts/kbot.service` with systemd; instructions are in the file.
  A server in the US East region is close to Kalshi and Coinbase.

With an API key the bot uses Kalshi's websocket feed. If that feed sends no orderbook data for 60 s
(for example because the key is rejected), it falls back to public REST polling and logs an ERROR line.

## Reading the scoreboard

| column | meaning |
|---|---|
| looks | signals where that horizon had a quotable market |
| stale% | Kalshi's ask hadn't moved since the BTC move started |
| edge>=min / avgEdge | how often (and by how many ¢) the quote was still mispriced after fees |
| lag_s | median seconds until Kalshi closed half the gap to fair value |
| mk5s / mk30s | bid N seconds after entry minus entry price (¢/contract). The fastest honest P&L signal |
| win% / PnL$ / ROI% | on settled trades |

The best horizon has a high stale%, a long lag, and positive markouts, with enough trades to mean something.
Run paper mode for days before trusting any of it.

## Caveats

- **No 1-minute or 5-minute markets.** As of Nov 2026, Kalshi's shortest BTC markets are `KXBTC15M` (15 minutes)
  and the hourly `KXBTCD` (above/below a strike). Both are the defaults. Run `discover` to recheck; the bot warns
  on startup about any series with no open markets.
- **Prices.** Kalshi quotes some markets in $0.001 ticks with fractional sizes; the bot keeps prices as
  fractional cents.
- **Settlement basis.** Kalshi BTC contracts settle on the CF Benchmarks index (a 60 s average for these
  contracts). The model uses spot, so it's slightly off in the last minute. That's one reason for
  `min_seconds_to_close`.
- **Up/down strike.** If the API doesn't give `floor_strike` for an up/down market, the bot uses the BTC price
  at `open_time` from its own feed. This only works if the bot was running at that moment; otherwise the market
  is skipped.
- **Competition.** Kalshi market makers watch the same feeds. Run this from a low-latency host near the exchanges.
  The simulator's lag numbers are made up: sim P&L proves the plumbing works, not that the strategy makes money.
- **Response fields.** Order and fill fields have changed across Kalshi API versions. `LiveBroker` handles both the
  cents and `_dollars` formats. Check the first few live fills against the Kalshi UI.
