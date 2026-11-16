"""Detects abnormal BTC moves and volume spikes from a trade stream.

Trades are bucketed into 1-second bars. "Normal" is learned from the last `baseline_s` seconds:
  * jump:   |log return over lookback| is >= ret_z standard deviations AND >= min_move_bps
  * volume: lookback volume is >= vol_z standard deviations above normal AND price moved
            >= vol_confirm_ret_z in the same direction as net taker flow
"""
from __future__ import annotations

import math
from collections import deque

from .config import SignalCfg
from .models import Signal


class Detector:
    def __init__(self, cfg: SignalCfg):
        self.cfg = cfg
        self.bars: deque[list] = deque(maxlen=cfg.baseline_s + int(cfg.lookback_s) + 5)  # [sec, close, vol, signed_vol]
        self._stats_sec = None
        self.sigma_1s = cfg.vol_floor_bps / 1e4
        self._vol_mean = 0.0
        self._vol_std = 0.0
        self._last_signal = {1: -1e18, -1: -1e18}
        self._first_sec = None

    def _roll(self, sec: int):
        if not self.bars:
            self.bars.append([sec, None, 0.0, 0.0])
            return
        last = self.bars[-1][0]
        if sec <= last:
            return
        close = self.bars[-1][1]
        for s in range(last + 1, min(sec, last + self.bars.maxlen) + 1):
            self.bars.append([s, close, 0.0, 0.0])
        if sec > self.bars[-1][0]:
            self.bars.append([sec, close, 0.0, 0.0])

    def _recompute_stats(self):
        """Baseline stats over completed bars, excluding the most recent lookback window."""
        L = int(math.ceil(self.cfg.lookback_s))
        hist = list(self.bars)[:-(L + 1)]
        closes = [b[1] for b in hist if b[1] is not None]
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
        if len(rets) > 10:
            m = sum(rets) / len(rets)
            var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
            self.sigma_1s = max(math.sqrt(var), self.cfg.vol_floor_bps / 1e4)
        vols = [b[2] for b in hist]
        if len(vols) > 10:
            m = sum(vols) / len(vols)
            self._vol_mean = m
            self._vol_std = math.sqrt(sum((v - m) ** 2 for v in vols) / (len(vols) - 1))

    def price_at(self, ts: float) -> float | None:
        sec = int(ts)
        for b in reversed(self.bars):
            if b[0] <= sec and b[1] is not None:
                return b[1]
        return None

    def on_trade(self, ts: float, price: float, size: float, taker_side: int) -> Signal | None:
        """taker_side: +1 aggressive buy, -1 aggressive sell, 0 unknown."""
        sec = int(ts)
        if self._first_sec is None:
            self._first_sec = sec
        self._roll(sec)
        bar = self.bars[-1]
        bar[1] = price
        bar[2] += size
        bar[3] += size * taker_side
        if self._stats_sec != sec:
            self._stats_sec = sec
            self._recompute_stats()
        cfg = self.cfg
        if sec - self._first_sec < cfg.warmup_s:
            return None

        before = self.price_at(ts - cfg.lookback_s)
        if not before:
            return None
        ret = math.log(price / before)
        base_sd = self.sigma_1s * math.sqrt(cfg.lookback_s)
        z_ret = ret / base_sd
        L = int(math.ceil(cfg.lookback_s))
        n = len(self.bars)
        window = [self.bars[n - i] for i in range(1, min(L, n) + 1)]
        wvol = sum(b[2] for b in window)
        wflow = sum(b[3] for b in window)
        exp_vol = self._vol_mean * L
        sd_vol = max(self._vol_std * math.sqrt(L), 1e-9)
        z_vol = (wvol - exp_vol) / sd_vol

        direction, reason = 0, ""
        if abs(z_ret) >= cfg.ret_z and abs(ret) * 1e4 >= cfg.min_move_bps:
            direction, reason = (1 if ret > 0 else -1), "jump"
        elif z_vol >= cfg.vol_z and abs(z_ret) >= cfg.vol_confirm_ret_z:
            d = 1 if ret > 0 else -1
            if wflow == 0 or (wflow > 0) == (d > 0):
                direction, reason = d, "volume"
        if not direction:
            return None
        if ts - self._last_signal[direction] < cfg.cooldown_s:
            return None
        self._last_signal[direction] = ts
        return Signal(ts=ts, direction=direction, reason=reason, price=price, price_before=before,
                      ret_bps=ret * 1e4, z_ret=z_ret, z_vol=z_vol, sigma_1s=self.sigma_1s)
