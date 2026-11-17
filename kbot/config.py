"""Configuration. Loaded from a TOML file (see config.example.toml); every field has a default."""
from __future__ import annotations

import sys

if sys.version_info >= (3, 11):
    import tomllib
else:  # Python 3.9 / 3.10
    import tomli as tomllib
from dataclasses import dataclass, field, fields, is_dataclass


@dataclass
class KalshiCfg:
    env: str = "demo"                      # "demo" or "prod"
    key_id: str = ""                       # API key id (or env KALSHI_KEY_ID)
    private_key_path: str = ""             # RSA private key PEM (or env KALSHI_PRIVATE_KEY_PATH)
    market_refresh_s: float = 15.0         # how often to re-list open markets (they roll over)
    poll_s: float = 1.0                    # no-key mode: seconds between orderbook polls
    poll_markets_per_series: int = 3       # no-key mode: markets polled per horizon (nearest the money)

    @property
    def rest_base(self) -> str:
        return ("https://api.elections.kalshi.com" if self.env == "prod"
                else "https://demo-api.kalshi.co") + "/trade-api/v2"

    @property
    def ws_url(self) -> str:
        return ("wss://api.elections.kalshi.com" if self.env == "prod"
                else "wss://demo-api.kalshi.co") + "/trade-api/ws/v2"


@dataclass
class FeedCfg:
    coinbase: bool = True
    kraken: bool = True
    venue_stale_s: float = 10.0            # drop a venue from the composite price if silent this long


@dataclass
class SignalCfg:
    lookback_s: float = 5.0                # window the jump is measured over
    baseline_s: int = 900                  # history used for "normal" volatility / volume
    warmup_s: int = 120                    # no signals until this much history exists
    ret_z: float = 4.0                     # move must be this many std-devs of a normal lookback move
    min_move_bps: float = 8.0              # ...and at least this big in absolute terms
    vol_z: float = 5.0                     # volume-spike trigger (z-score of lookback volume)
    vol_confirm_ret_z: float = 2.0         # a volume spike also needs a move of this z in the same direction
    cooldown_s: float = 15.0               # min gap between signals in the same direction
    vol_floor_bps: float = 0.3             # floor on the realized 1-second volatility estimate (bps)
    min_pricing_vol_bps: float = 0.5       # floor on the volatility used to price a move (bps per sqrt s)


@dataclass
class TradeCfg:
    require_edge: bool = True              # only bet if Kalshi's ask is still below model fair value
    min_edge_cents: float = 3.0            # required edge per contract after fees
    contracts_per_order: int = 10
    max_contracts_per_market: int = 50
    max_total_exposure_usd: float = 200.0  # sum of cost basis of open positions
    daily_loss_limit_usd: float = 100.0    # stop opening positions after this realized loss
    slippage_cents: int = 1                # IOC limit = ask + slippage
    min_seconds_to_close: float = 30.0
    max_book_age_s: float = 120.0          # ignore markets whose book we haven't heard about recently
    market_cooldown_s: float = 20.0        # min gap between orders in the same market
    max_spread_cents: float = 10.0         # wider Kalshi spreads aren't trusted for implied volatility
    fee_rate: float = 0.07                 # Kalshi taker fee = ceil(rate * C * P * (1-P))
    paper_latency_ms: float = 150.0        # simulated order round-trip in paper mode


@dataclass
class Config:
    # horizon label -> Kalshi series ticker. Run `python -m kbot discover` to see real series.
    # As of Nov 2026 Kalshi's shortest BTC markets are 15-minute (KXBTC15M) and hourly (KXBTCD).
    horizons: dict[str, str] = field(default_factory=lambda: {
        "15m": "KXBTC15M",
        "60m": "KXBTCD",
    })
    kalshi: KalshiCfg = field(default_factory=KalshiCfg)
    feeds: FeedCfg = field(default_factory=FeedCfg)
    signal: SignalCfg = field(default_factory=SignalCfg)
    trade: TradeCfg = field(default_factory=TradeCfg)
    db_path: str = "kbot.sqlite"
    status_path: str = "status.json"       # live snapshot the dashboard reads (rewritten every 2 s)
    markout_offsets_s: list[float] = field(default_factory=lambda: [1, 3, 5, 10, 30, 60])


def _apply(obj, data: dict):
    for f in fields(obj):
        if f.name not in data:
            continue
        cur = getattr(obj, f.name)
        if is_dataclass(cur):
            _apply(cur, data[f.name])
        else:
            setattr(obj, f.name, data[f.name])


def load_config(path: str | None) -> Config:
    cfg = Config()
    if path:
        with open(path, "rb") as fh:
            _apply(cfg, tomllib.load(fh))
    return cfg
