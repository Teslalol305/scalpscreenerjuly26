"""Incremental feature calculators: O(1)-amortized per event, no pandas.

Use: fe = FeatureEngine(state); state hooks call fe automatically; fe.snapshot
holds the latest FeatureSnapshot (rebuilt on every closed 1s bar).
Depends on: core.state. All formulas:
  ROC(w)      = last_price / close_{w seconds ago} - 1
  vol_z       = (vol last 10s - mean 10s-bucket vol) / sd    [trailing 30 min]
  CVD(w)      = sum of (buy - sell) volume over w seconds
  VWAP sd     = sqrt(sum(p^2 v)/sum(v) - vwap^2)             [volume-weighted]
  ATR14/RSI14 = Wilder smoothing on 1m bars; EMA seeded with first close
  BB(20,2) width = 4*sd(close,20)/mid ; percentile vs session widths
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from tapescreen.core.events import BUY
from tapescreen.core.state import Bar, SymbolState, percentile_rank


class BarWindow:
    """Running sums of the last N closed 1s bars (volume/buy/pv/pv2/ntrades)."""

    __slots__ = ("_q", "buy_volume", "ntrades", "pv", "pv2", "volume")

    def __init__(self, n: int) -> None:
        self._q: deque[tuple[float, float, float, float, int]] = deque(maxlen=n)
        self.volume = 0.0
        self.buy_volume = 0.0
        self.pv = 0.0
        self.pv2 = 0.0
        self.ntrades = 0

    def add(self, b: Bar) -> None:
        if len(self._q) == self._q.maxlen:
            v, bv, pv, pv2, nt = self._q[0]
            self.volume -= v
            self.buy_volume -= bv
            self.pv -= pv
            self.pv2 -= pv2
            self.ntrades -= nt
        row = (b.volume, b.buy_volume, b.pv, b.pv2, b.ntrades)
        self._q.append(row)
        self.volume += b.volume
        self.buy_volume += b.buy_volume
        self.pv += b.pv
        self.pv2 += b.pv2
        self.ntrades += b.ntrades

    @property
    def cvd(self) -> float:
        return 2.0 * self.buy_volume - self.volume

    @property
    def full(self) -> bool:
        return len(self._q) == self._q.maxlen

    def vwap_sd(self) -> tuple[float, float]:
        if self.volume <= 0:
            return 0.0, 0.0
        vwap = self.pv / self.volume
        var = max(0.0, self.pv2 / self.volume - vwap * vwap)
        return vwap, math.sqrt(var)


class Wilder:
    """Wilder-smoothed average (ATR/RSI building block): seeds with SMA of first n."""

    __slots__ = ("_seed", "n", "value")

    def __init__(self, n: int) -> None:
        self.n = n
        self.value = 0.0
        self._seed: list[float] = []

    @property
    def ready(self) -> bool:
        return len(self._seed) >= self.n

    def add(self, v: float) -> float:
        if len(self._seed) < self.n:  # SMA warm-up for the first n samples
            self._seed.append(v)
            self.value = sum(self._seed) / len(self._seed)
        else:
            self.value = (self.value * (self.n - 1) + v) / self.n
        return self.value


@dataclass(slots=True)
class FeatureSnapshot:
    """Everything the rules and the UI grid read; one per symbol per 1s close."""

    ts: float = 0.0
    symbol: str = ""
    price: float = 0.0
    warming: bool = True
    new_1m_bar: bool = False
    # tape
    roc_10s: float = 0.0
    roc_30s: float = 0.0
    roc_60s: float = 0.0
    roc_5m: float = 0.0
    roc_15m: float = 0.0
    roc30_sigma: float = 0.0
    roc5m_sigma: float = 0.0
    vol_10s: float = 0.0
    vol_z: float = 0.0
    tps_z: float = 0.0
    cvd_session: float = 0.0
    cvd_1m: float = 0.0
    cvd_5m: float = 0.0
    cvd_slope_1m: float = 0.0  # base units / second over the last 60s
    cvd_slope_1m_prev: float = 0.0  # same, over [-120s, -60s)
    agg_imbalance_60s: float = 0.5
    last_large_print_ts: float = 0.0
    last_large_print_side: str = ""
    burst_side: str = ""
    # book
    spread_bps: float = 0.0
    book_imbalance: float = 0.5
    imb_high_dur_s: float = 0.0
    imb_low_dur_s: float = 0.0
    wall_bid: float = 0.0  # largest bid-wall size (0 = none)
    wall_ask: float = 0.0
    last_wall_pull_ts: float = 0.0
    last_wall_pull_side: str = ""
    # perp ctx
    funding: float = 0.0
    funding_abs_p95_7d: float = 0.0
    funding_pctl_7d: float = 50.0
    doi_1m: float = 0.0
    doi_5m: float = 0.0
    doi5_session_p95: float = 0.0
    basis_bps: float = 0.0
    # bar-derived
    vwap_session: float = 0.0
    vwap_session_sd: float = 0.0
    vwap_dist_sigma: float = 0.0  # (price - session vwap) / sd
    vwap_30m: float = 0.0
    vwap_30m_sd: float = 0.0
    atr_1m: float = 0.0
    rsi_14: float = 50.0
    ema_9: float = 0.0
    ema_21: float = 0.0
    bb_width: float = 0.0
    bb_width_pctl: float = 50.0
    squeeze_run_min: int = 0
    last_1m_range: float = 0.0
    last_1m_dir: int = 0  # +1 up bar, -1 down bar
    prior_15m_high: float = 0.0
    prior_15m_low: float = 0.0
    session_high: float = 0.0
    session_low: float = 0.0
    vol_z_5m_peak: float = 0.0
    vol_z_prev: float = 0.0
    extras: dict = field(default_factory=dict)


class FeatureEngine:
    """Attaches to one SymbolState; keeps 1m indicators and builds snapshots."""

    def __init__(self, state: SymbolState) -> None:
        self.state = state
        cfgf = state.cfg.rules
        self.w10 = BarWindow(10)
        self.w60 = BarWindow(60)
        self.w300 = BarWindow(300)
        self.w1800 = BarWindow(1800)
        self.prev_w60_cvd = deque([0.0] * 60, maxlen=60)  # cvd_1m as of 60s ago
        # 1m indicators
        self.atr = Wilder(14)
        self.rsi_gain = Wilder(14)
        self.rsi_loss = Wilder(14)
        self.ema9 = 0.0
        self.ema21 = 0.0
        self.bb_closes: deque[float] = deque(maxlen=20)
        self._bb_sum = 0.0
        self._bb_sumsq = 0.0
        self.bb_width = 0.0
        self.bb_width_pctl = 50.0
        self.squeeze_run_min = 0
        self.squeeze_pctl_max = float(cfgf["squeeze_release"]["bb_width_pctl_max"])
        self.funding_pctl_q = float(cfgf["funding_extremity"]["pctl"]) / 100.0
        self.doi_pctl_q = float(cfgf["oi_compression"]["doi_pctl"]) / 100.0
        self.last_1m_range = 0.0
        self.last_1m_dir = 0
        self._prev_1m_close = 0.0
        self._funding_cache_ts = -1e18
        self._funding_p95 = 0.0
        self._vol_z_hist: deque[float] = deque(maxlen=300)  # per-1s vol_z, 5 min
        self._new_1m = False
        self.snapshot = FeatureSnapshot(symbol=state.symbol)
        state.on_bar_1s.append(self._on_1s)
        state.on_bar_1m.append(self._on_1m)

    # ------------------------------------------------------------- 1m closes

    def _on_1m(self, st: SymbolState, b: Bar) -> None:
        self._new_1m = True
        prev_close = self._prev_1m_close if self._prev_1m_close else b.open
        tr = max(b.high - b.low, abs(b.high - prev_close), abs(b.low - prev_close))
        self.atr.add(tr)
        change = b.close - prev_close
        self.rsi_gain.add(max(0.0, change))
        self.rsi_loss.add(max(0.0, -change))
        self._prev_1m_close = b.close
        self.ema9 = b.close if self.ema9 == 0.0 else self.ema9 + (b.close - self.ema9) * (2 / 10)
        self.ema21 = b.close if self.ema21 == 0.0 else self.ema21 + (b.close - self.ema21) * (2 / 22)

        if len(self.bb_closes) == self.bb_closes.maxlen:
            old = self.bb_closes[0]
            self._bb_sum -= old
            self._bb_sumsq -= old * old
        self.bb_closes.append(b.close)
        self._bb_sum += b.close
        self._bb_sumsq += b.close * b.close
        n = len(self.bb_closes)
        if n >= 2:
            mean = self._bb_sum / n
            var = max(0.0, self._bb_sumsq / n - mean * mean)
            sd = math.sqrt(var)
            self.bb_width = (4.0 * sd / mean) if mean > 0 else 0.0
            samples = st.bbw_samples
            samples.append(self.bb_width)
            if len(samples) > 480:
                del samples[0]
            self.bb_width_pctl = percentile_rank(samples, self.bb_width)
            if n == self.bb_closes.maxlen and self.bb_width_pctl <= self.squeeze_pctl_max:
                self.squeeze_run_min += 1
            else:
                self.squeeze_run_min = 0
        self.last_1m_range = b.high - b.low
        self.last_1m_dir = 1 if b.close >= b.open else -1

        if st.oi_hist:  # signed dOI; skip when no OI data (e.g. candle-seeded warmup bars)
            st.doi5_samples.append(self._doi(300.0))
            if len(st.doi5_samples) > 480:
                del st.doi5_samples[0]

    # ------------------------------------------------------------- 1s closes

    def _on_1s(self, st: SymbolState, b: Bar) -> None:
        self.prev_w60_cvd.append(self.w60.cvd)
        self.w10.add(b)
        self.w60.add(b)
        self.w300.add(b)
        self.w1800.add(b)
        self._rebuild(st, b)

    def _doi(self, window_s: float) -> float:
        hist = self.state.oi_hist
        if not hist:
            return 0.0
        now_ts, now_oi = hist[-1]
        target = now_ts - window_s
        if hist[0][0] > target + 15.0:  # not enough history to cover the window yet
            return 0.0
        past_oi = None
        for ts, oi in hist:
            if ts >= target:
                past_oi = oi
                break
        return now_oi - past_oi if past_oi is not None else 0.0

    def _funding_p95_7d(self, now: float) -> float:
        """Trailing 7-day |funding| percentile threshold; 0.0 (= rule disabled)
        until at least 24h of history exists, so a cold start can't self-flag."""
        if now - self._funding_cache_ts >= 60.0:
            self._funding_cache_ts = now
            hist = self.state.funding_hist
            if len(hist) >= 2 and hist[-1][0] - hist[0][0] >= 86_400.0:
                rates = sorted(abs(r) for _, r in hist)
                idx = min(len(rates) - 1, int(math.ceil(self.funding_pctl_q * len(rates))) - 1)
                self._funding_p95 = rates[max(0, idx)]
            else:
                self._funding_p95 = 0.0
        return self._funding_p95

    def _rebuild(self, st: SymbolState, closed: Bar) -> None:
        s = self.snapshot
        now = st.now
        price = st.last_price if st.last_price else closed.close
        s.ts = now
        s.symbol = st.symbol
        s.price = price
        s.new_1m_bar = self._new_1m
        self._new_1m = False

        def roc(sec: int) -> float:
            past = st.close_price_ago(sec)
            return price / past - 1.0 if past > 0 else 0.0

        s.roc_10s = roc(10)
        s.roc_30s = roc(30)
        s.roc_60s = roc(60)
        s.roc_5m = roc(300)
        s.roc_15m = roc(900)
        st.roc30_stat.add(s.roc_30s)
        st.roc5m_stat.add(s.roc_5m)
        s.roc30_sigma = st.roc30_stat.std
        s.roc5m_sigma = st.roc5m_stat.std

        s.vol_10s = self.w10.volume
        s.vol_z_prev = s.vol_z
        s.vol_z = st.bucket_vol.zscore(self.w10.volume)
        s.tps_z = st.bucket_trades.zscore(float(self.w10.ntrades))
        self._vol_z_hist.append(s.vol_z)
        s.vol_z_5m_peak = max(self._vol_z_hist) if self._vol_z_hist else 0.0

        s.cvd_session = st.session_cvd
        s.cvd_1m = self.w60.cvd
        s.cvd_5m = self.w300.cvd
        s.cvd_slope_1m = s.cvd_1m / 60.0
        s.cvd_slope_1m_prev = self.prev_w60_cvd[0] / 60.0
        tot60 = self.w60.volume
        s.agg_imbalance_60s = self.w60.buy_volume / tot60 if tot60 > 0 else 0.5
        s.last_large_print_ts = st.last_large_print_ts
        s.last_large_print_side = st.last_large_print_side
        s.burst_side = self._burst(st, now)

        s.spread_bps = st.spread_bps
        s.book_imbalance = st.book_imbalance
        s.imb_high_dur_s = (now - st.imb_high_since) if st.imb_high_since else 0.0
        s.imb_low_dur_s = (now - st.imb_low_since) if st.imb_low_since else 0.0
        s.wall_bid = max((w.size for w in st.walls if w.side == "bid"), default=0.0)
        s.wall_ask = max((w.size for w in st.walls if w.side == "ask"), default=0.0)
        s.last_wall_pull_ts = st.last_wall_pull_ts
        s.last_wall_pull_side = st.last_wall_pull_side

        ctx = st.ctx
        if ctx is not None:
            s.funding = ctx.funding_rate
            s.funding_abs_p95_7d = self._funding_p95_7d(now)
            s.funding_pctl_7d = percentile_rank(
                [abs(r) for _, r in st.funding_hist], abs(ctx.funding_rate)
            ) if st.funding_hist else 50.0
            s.doi_1m = self._doi(60.0)
            s.doi_5m = self._doi(300.0)
            samples = st.doi5_samples
            if samples:
                srt = sorted(samples)
                idx = min(len(srt) - 1, max(0, int(math.ceil(self.doi_pctl_q * len(srt))) - 1))
                s.doi5_session_p95 = srt[idx]
            mid = (st.bid_px + st.ask_px) / 2.0 if st.bid_px and st.ask_px else 0.0
            s.basis_bps = (ctx.mark - mid) / mid * 1e4 if mid > 0 else 0.0

        if st.session_vol > 0:
            vwap = st.session_pv / st.session_vol
            var = max(0.0, st.session_pv2 / st.session_vol - vwap * vwap)
            sd = math.sqrt(var)
            s.vwap_session = vwap
            s.vwap_session_sd = sd
            s.vwap_dist_sigma = (price - vwap) / sd if sd > 1e-12 else 0.0
        s.vwap_30m, s.vwap_30m_sd = self.w1800.vwap_sd()

        s.atr_1m = self.atr.value
        avg_loss = self.rsi_loss.value
        avg_gain = self.rsi_gain.value
        if avg_loss <= 1e-18:
            s.rsi_14 = 100.0 if avg_gain > 0 else 50.0
        else:
            s.rsi_14 = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
        s.ema_9 = self.ema9
        s.ema_21 = self.ema21
        s.bb_width = self.bb_width
        s.bb_width_pctl = self.bb_width_pctl
        s.squeeze_run_min = self.squeeze_run_min
        s.last_1m_range = self.last_1m_range
        s.last_1m_dir = self.last_1m_dir

        hi, lo = st.prior_15m_extremes(exclude_s=1.0)
        s.prior_15m_high = hi if hi > -math.inf else 0.0
        s.prior_15m_low = lo if lo < math.inf else 0.0
        s.session_high = st.session_high if st.session_high > -math.inf else 0.0
        s.session_low = st.session_low if st.session_low < math.inf else 0.0

        # warming: need most of the 30-min activity baseline before z-scores mean much
        s.warming = len(st.bucket_vol) < max(6, (st.bucket_vol._q.maxlen or 1) * 2 // 3)

    def _burst(self, st: SymbolState, now: float) -> str:
        k = st.cfg.features.burst_k
        window = st.cfg.features.burst_window_s
        buys = sells = 0
        for t in reversed(st.ticks):
            if now - t.ts_recv > window:
                break
            if t.side == BUY:
                buys += 1
            else:
                sells += 1
        if buys >= k:
            return "buy"
        if sells >= k:
            return "sell"
        return ""
