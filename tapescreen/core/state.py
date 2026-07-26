"""Per-symbol rolling state: 1s/1m bars, tick/book mirrors, trailing baselines.

Use: st = SymbolState(sym, cfg); st.on_event(ev) per canonical event; closed 1s
bars fire ``st.on_bar_1s`` hooks (features/signals attach there).
Depends on: core.events. Pipeline clock is ts_recv (recorded -> deterministic
replay); ts_exch is used only for latency metrics upstream.

Memory bounds (hard caps): 1s bars ring = state.bars_1s_cap (default 3600),
1m bars ring = bars_1m_cap (480), tick window = tick_window_s seconds of trades,
10s baseline buckets = baseline_window_s/10 entries, OI history = 330s,
funding history = 7 days sampled per minute (<= 10080 points).
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from tapescreen.config import Config
from tapescreen.core.events import BUY, Bbo, BookTop, Event, PerpCtx, Tick


@dataclass(slots=True)
class Bar:
    ts: float  # open time, aligned to the bar size
    open: float
    high: float
    low: float
    close: float
    volume: float  # base units
    buy_volume: float  # taker-buy base units
    ntrades: int
    pv: float  # sum(price*size) for VWAP math
    pv2: float  # sum(price^2*size) for VWAP variance

    @property
    def sell_volume(self) -> float:
        return self.volume - self.buy_volume

    @property
    def delta(self) -> float:
        """Signed taker flow (buy - sell volume)."""
        return 2.0 * self.buy_volume - self.volume


class Ring:
    """Fixed-capacity ring with O(1) append and O(1) access from the newest end.

    ring[-1] is the newest item, ring[-k] the k-th newest; len() <= cap.
    """

    __slots__ = ("_buf", "_cap", "_head", "_len")

    def __init__(self, cap: int) -> None:
        self._cap = cap
        self._buf: list[Any] = [None] * cap
        self._head = 0  # index where the NEXT item will be written
        self._len = 0

    def append(self, item: Any) -> None:
        self._buf[self._head] = item
        self._head = (self._head + 1) % self._cap
        if self._len < self._cap:
            self._len += 1

    def __len__(self) -> int:
        return self._len

    def __getitem__(self, idx: int) -> Any:
        if idx >= 0:
            if idx >= self._len:
                raise IndexError(idx)
            idx -= self._len  # normalize to negative
        elif idx < -self._len:
            raise IndexError(idx)
        return self._buf[(self._head + idx) % self._cap]

    def last(self, n: int) -> list[Any]:
        n = min(n, self._len)
        return [self[-i] for i in range(n, 0, -1)]


class RollingStat:
    """Windowed mean/σ over the last ``cap`` appended samples, O(1) per update."""

    __slots__ = ("_q", "_sum", "_sumsq")

    def __init__(self, cap: int) -> None:
        self._q: deque[float] = deque(maxlen=cap)
        self._sum = 0.0
        self._sumsq = 0.0

    def add(self, v: float) -> None:
        if len(self._q) == self._q.maxlen:
            old = self._q[0]
            self._sum -= old
            self._sumsq -= old * old
        self._q.append(v)
        self._sum += v
        self._sumsq += v * v

    def __len__(self) -> int:
        return len(self._q)

    @property
    def mean(self) -> float:
        n = len(self._q)
        return self._sum / n if n else 0.0

    @property
    def std(self) -> float:
        n = len(self._q)
        if n < 2:
            return 0.0
        var = max(0.0, self._sumsq / n - (self._sum / n) ** 2)
        return math.sqrt(var)

    def zscore(self, v: float) -> float:
        s = self.std
        return (v - self.mean) / s if s > 1e-12 else 0.0


def percentile_rank(samples: list[float], v: float) -> float:
    """Share of samples <= v, in [0, 100]; 50.0 when no samples."""
    if not samples:
        return 50.0
    return 100.0 * sum(1 for s in samples if s <= v) / len(samples)


@dataclass(slots=True)
class Wall:
    side: str  # "bid" | "ask"
    price: float
    size: float


class SymbolState:
    """All rolling state for one symbol. Event-time driven; no wall-clock reads."""

    def __init__(self, symbol: str, cfg: Config) -> None:
        self.symbol = symbol
        self.cfg = cfg
        self.now = 0.0  # event-time (ts_recv of the latest event seen)

        # bars
        self.bars_1s = Ring(cfg.state.bars_1s_cap)
        self.bars_1m = Ring(cfg.state.bars_1m_cap)
        self.cur_1s: Bar | None = None
        self.cur_1m: Bar | None = None
        self.on_bar_1s: list[Callable[[SymbolState, Bar], None]] = []
        self.on_bar_1m: list[Callable[[SymbolState, Bar], None]] = []

        # tape
        self.last_price = 0.0
        self.last_tick_ts = 0.0
        self.session_open = 0.0
        self.session_high = -math.inf
        self.session_low = math.inf
        self.ticks: deque[Tick] = deque()  # trailing tick_window_s of trades
        self.session_cvd = 0.0
        self.session_pv = 0.0
        self.session_pv2 = 0.0
        self.session_vol = 0.0

        # trailing 30-min baselines, non-overlapping 10s buckets
        nbuckets = max(6, int(cfg.state.baseline_window_s // 10))
        self.bucket_vol = RollingStat(nbuckets)
        self.bucket_trades = RollingStat(nbuckets)
        self._cur_bucket_ts = -1.0
        self._cur_bucket_vol = 0.0
        self._cur_bucket_trades = 0

        # trade sizes for the large-print p99 (lazy threshold refresh)
        self.trade_sizes: deque[tuple[float, float]] = deque()
        self._p99_size = math.inf
        self._p99_stamp = -1e18
        self.last_large_print_ts = 0.0
        self.last_large_print_side = ""

        # ROC sigma baselines, sampled once per 1s close
        self.roc30_stat = RollingStat(int(cfg.state.baseline_window_s))
        self.roc5m_stat = RollingStat(int(cfg.state.baseline_window_s))

        # book
        self.book: BookTop | None = None
        self.bid_px = 0.0
        self.bid_sz = 0.0
        self.ask_px = 0.0
        self.ask_sz = 0.0
        self.spread_bps = 0.0
        self.book_imbalance = 0.5
        self.walls: list[Wall] = []
        self.last_wall_pull_ts = 0.0
        self.last_wall_pull_side = ""
        self.imb_high_since = 0.0  # imbalance >= high threshold continuously since
        self.imb_low_since = 0.0

        # perp ctx
        self.ctx: PerpCtx | None = None
        self.oi_hist: deque[tuple[float, float]] = deque()  # (ts, oi), trailing ~5.5m
        self.funding_hist: deque[tuple[float, float]] = deque()  # (ts, rate), 7 days/1min
        self._funding_sample_ts = 0.0

        # session distributions (sampled on 1m closes)
        self.doi5_samples: list[float] = []  # session |dOI 5m| samples, cap 480
        self.bbw_samples: list[float] = []  # session BB-width samples, cap 480

        self.warming = True  # cleared once baselines have >= 2/3 of their window

    # ------------------------------------------------------------------ events

    def on_event(self, ev: Event) -> None:
        if isinstance(ev, Tick):
            self._advance(ev.ts_recv)
            self._on_tick(ev)
        elif isinstance(ev, Bbo):
            self._advance(ev.ts_recv)
            self._on_bbo(ev)
        elif isinstance(ev, BookTop):
            self._advance(ev.ts_recv)
            self._on_book(ev)
        elif isinstance(ev, PerpCtx):
            self._advance(ev.ts_recv)
            self._on_ctx(ev)

    def _on_tick(self, t: Tick) -> None:
        px, sz = t.price, t.size
        self.last_price = px
        self.last_tick_ts = t.ts_recv
        if self.session_open == 0.0:
            self.session_open = px
        self.session_high = max(self.session_high, px)
        self.session_low = min(self.session_low, px)

        signed = sz if t.side == BUY else -sz
        self.session_cvd += signed
        self.session_pv += px * sz
        self.session_pv2 += px * px * sz
        self.session_vol += sz

        self.ticks.append(t)
        cut = t.ts_recv - self.cfg.state.tick_window_s
        while self.ticks and self.ticks[0].ts_recv < cut:
            self.ticks.popleft()

        # 10s activity bucket (non-overlapping, aligned)
        bts = t.ts_recv - (t.ts_recv % 10.0)
        if bts != self._cur_bucket_ts:
            if self._cur_bucket_ts >= 0:
                self.bucket_vol.add(self._cur_bucket_vol)
                self.bucket_trades.add(float(self._cur_bucket_trades))
            self._cur_bucket_ts = bts
            self._cur_bucket_vol = 0.0
            self._cur_bucket_trades = 0
        self._cur_bucket_vol += sz
        self._cur_bucket_trades += 1

        # large print vs trailing p99
        self.trade_sizes.append((t.ts_recv, sz))
        scut = t.ts_recv - self.cfg.state.baseline_window_s
        while self.trade_sizes and self.trade_sizes[0][0] < scut:
            self.trade_sizes.popleft()
        if sz >= self._p99_threshold(t.ts_recv) and len(self.trade_sizes) >= 100:
            self.last_large_print_ts = t.ts_recv
            self.last_large_print_side = t.side

        # bars
        self._bar_apply(t)

    def _p99_threshold(self, now: float) -> float:
        if now - self._p99_stamp >= 5.0:  # refresh at most every 5s
            self._p99_stamp = now
            n = len(self.trade_sizes)
            if n >= 100:
                import heapq

                k = max(1, n - int(math.ceil(n * self.cfg.features.large_print_pctl / 100.0)) + 1)
                self._p99_size = min(heapq.nlargest(k, (s for _, s in self.trade_sizes)))
            else:
                self._p99_size = math.inf
        return self._p99_size

    def _on_bbo(self, b: Bbo) -> None:
        if b.bid_px > 0:
            self.bid_px, self.bid_sz = b.bid_px, b.bid_sz
        if b.ask_px > 0:
            self.ask_px, self.ask_sz = b.ask_px, b.ask_sz
        if self.bid_px > 0 and self.ask_px > 0:
            mid = (self.bid_px + self.ask_px) / 2.0
            self.spread_bps = (self.ask_px - self.bid_px) / mid * 1e4 if mid > 0 else 0.0

    def _on_book(self, book: BookTop) -> None:
        prev_walls = self.walls
        self.book = book
        self.book_imbalance = book.imbalance
        if book.bids and book.asks:
            self.bid_px, self.bid_sz = book.bids[0]
            self.ask_px, self.ask_sz = book.asks[0]
            self.spread_bps = book.spread_bps

        # wall detection: any level >= wall_mult x median size of the mirrored levels
        sizes = [sz for _, sz in book.bids] + [sz for _, sz in book.asks]
        walls: list[Wall] = []
        if len(sizes) >= 4:
            srt = sorted(sizes)
            m = len(srt) // 2
            median = srt[m] if len(srt) % 2 else 0.5 * (srt[m - 1] + srt[m])
            if median > 0:
                thresh = self.cfg.features.wall_mult * median
                walls += [Wall("bid", px, sz) for px, sz in book.bids if sz >= thresh]
                walls += [Wall("ask", px, sz) for px, sz in book.asks if sz >= thresh]
        # wall-pull: a previous wall vanished without price trading through it
        cur_px = {(w.side, w.price) for w in walls}
        for w in prev_walls:
            if (w.side, w.price) in cur_px:
                continue
            traded_through = (
                (w.side == "bid" and self.last_price <= w.price)
                or (w.side == "ask" and self.last_price >= w.price)
            )
            if not traded_through:
                self.last_wall_pull_ts = book.ts_recv
                self.last_wall_pull_side = w.side
        self.walls = walls

        # imbalance persistence clocks
        f = self.cfg.rules["book_imbalance"]
        hi, lo = float(f["imbalance_high"]), float(f["imbalance_low"])
        ts = book.ts_recv
        if book.imbalance >= hi:
            self.imb_high_since = self.imb_high_since or ts
        else:
            self.imb_high_since = 0.0
        if book.imbalance <= lo:
            self.imb_low_since = self.imb_low_since or ts
        else:
            self.imb_low_since = 0.0

    def _on_ctx(self, ctx: PerpCtx) -> None:
        self.ctx = ctx
        ts = ctx.ts_recv
        self.oi_hist.append((ts, ctx.open_interest))
        cut = ts - 330.0
        while self.oi_hist and self.oi_hist[0][0] < cut:
            self.oi_hist.popleft()
        if ts - self._funding_sample_ts >= 60.0:
            self._funding_sample_ts = ts
            self.funding_hist.append((ts, ctx.funding_rate))
            fcut = ts - 7 * 86400.0
            while self.funding_hist and self.funding_hist[0][0] < fcut:
                self.funding_hist.popleft()

    def seed_funding(self, samples: list[tuple[float, float]]) -> None:
        """Warm-up: preload (ts, rate) history (e.g. from REST fundingHistory)."""
        for ts, rate in samples:
            self.funding_hist.append((ts, rate))
        if samples:
            self._funding_sample_ts = samples[-1][0]

    def seed_bars_1m(self, bars: list[Bar]) -> None:
        """Warm-up: preload closed 1m bars (e.g. from REST candleSnapshot)."""
        for b in bars:
            self.bars_1m.append(b)
            for cb in self.on_bar_1m:
                cb(self, b)

    # ------------------------------------------------------------------ bars

    def _advance(self, ts: float) -> None:
        """Advance event-time; close any 1s/1m bars the new timestamp passed."""
        if ts <= self.now:
            self.now = max(self.now, ts)
            return
        self.now = ts
        if self.cur_1s is not None:
            # close elapsed 1s bars, inserting empty carry-forward bars for gaps
            steps = 0
            while self.cur_1s.ts + 1.0 <= ts and steps < self.bars_1s._cap:
                closed = self.cur_1s
                self.bars_1s.append(closed)
                self._roll_1m(closed)
                for cb in self.on_bar_1s:
                    cb(self, closed)
                c = closed.close
                self.cur_1s = Bar(closed.ts + 1.0, c, c, c, c, 0.0, 0.0, 0, 0.0, 0.0)
                steps += 1

    def _bar_apply(self, t: Tick) -> None:
        px, sz = t.price, t.size
        bts = t.ts_recv - (t.ts_recv % 1.0)
        if self.cur_1s is None:
            self.cur_1s = Bar(bts, px, px, px, px, 0.0, 0.0, 0, 0.0, 0.0)
        b = self.cur_1s
        if b.ntrades == 0 and b.volume == 0.0:
            b.open = b.high = b.low = b.close = px
        b.high = max(b.high, px)
        b.low = min(b.low, px)
        b.close = px
        b.volume += sz
        if t.side == BUY:
            b.buy_volume += sz
        b.ntrades += 1
        b.pv += px * sz
        b.pv2 += px * px * sz

    def _roll_1m(self, one_s: Bar) -> None:
        mts = one_s.ts - (one_s.ts % 60.0)
        if self.cur_1m is None:
            self.cur_1m = Bar(mts, one_s.open, one_s.high, one_s.low, one_s.close,
                              0.0, 0.0, 0, 0.0, 0.0)
        elif mts > self.cur_1m.ts:
            closed = self.cur_1m
            self.bars_1m.append(closed)
            for cb in self.on_bar_1m:
                cb(self, closed)
            self.cur_1m = Bar(mts, one_s.open, one_s.high, one_s.low, one_s.close,
                              0.0, 0.0, 0, 0.0, 0.0)
        m = self.cur_1m
        m.high = max(m.high, one_s.high)
        m.low = min(m.low, one_s.low)
        m.close = one_s.close
        m.volume += one_s.volume
        m.buy_volume += one_s.buy_volume
        m.ntrades += one_s.ntrades
        m.pv += one_s.pv
        m.pv2 += one_s.pv2

    # ------------------------------------------------------------------ views

    def close_price_ago(self, seconds: float) -> float:
        """Close of the 1s bar ``seconds`` back; 0.0 until that much history exists
        (so early-session ROCs read 0 instead of silently shrinking their window)."""
        n = len(self.bars_1s)
        k = max(1, int(seconds))
        if n < k:
            return 0.0
        return self.bars_1s[-k].close

    def rolling_1s_sum(self, n: int, field: str) -> float:
        total = 0.0
        for i in range(1, min(n, len(self.bars_1s)) + 1):
            total += getattr(self.bars_1s[-i], field)
        return total

    def prior_15m_extremes(self, exclude_s: float = 0.0) -> tuple[float, float]:
        """(high, low) of the prior 15 minutes of 1s bars, optionally excluding
        the most recent ``exclude_s`` seconds (so a fresh sweep isn't its own prior)."""
        hi, lo = -math.inf, math.inf
        skip = int(exclude_s)
        n = min(900 + skip, len(self.bars_1s))
        for i in range(skip + 1, n + 1):
            b = self.bars_1s[-i]
            if b.ntrades or b.volume:
                hi = max(hi, b.high)
                lo = min(lo, b.low)
            else:  # carry-forward bar still marks price presence
                hi = max(hi, b.close)
                lo = min(lo, b.close)
        return hi, lo
