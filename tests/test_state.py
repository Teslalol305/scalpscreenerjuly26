"""Unit tests for tapescreen.core.state: rings, rolling stats, bars, baselines, book state.

Every expectation is hand-computed from the documented formulas (module docstrings +
build spec), independently of the implementation; the arithmetic is shown in comments.
The pipeline clock is ts_recv: a bar closes only when a LATER event advances
event-time past its boundary, so tests flush bars with an empty Bbo event.
"""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from tapescreen.config import Config
from tapescreen.core.events import BUY, SELL, Bbo, BookTop, Tick
from tapescreen.core.state import Ring, RollingStat, SymbolState, percentile_rank

SYM = "BTC"


def tick(ts: float, price: float, size: float, side: str = BUY) -> Tick:
    return Tick(ts_exch=ts, ts_recv=ts, ts_mono=ts, symbol=SYM, price=price,
                size=size, side=side, trade_id=0, venue="hyperliquid")


def flush(ts: float) -> Bbo:
    """Empty Bbo: advances event-time (closing due bars) without touching book fields."""
    return Bbo(ts=ts, ts_recv=ts, ts_mono=ts, symbol=SYM,
               bid_px=0.0, bid_sz=0.0, ask_px=0.0, ask_sz=0.0)


def book(ts: float, bids: list[tuple[float, float]], asks: list[tuple[float, float]],
         imbalance: float = 0.5, spread_bps: float = 1.0) -> BookTop:
    return BookTop(ts=ts, ts_recv=ts, ts_mono=ts, symbol=SYM, bids=bids, asks=asks,
                   spread_bps=spread_bps, imbalance=imbalance)


# --------------------------------------------------------------------------- 1. Ring


def test_ring_empty_and_bounds() -> None:
    r = Ring(4)
    assert len(r) == 0
    with pytest.raises(IndexError):
        _ = r[-1]
    with pytest.raises(IndexError):
        _ = r[0]


def test_ring_append_index_last() -> None:
    r = Ring(4)
    r.append(1)
    r.append(2)
    r.append(3)
    assert len(r) == 3
    # [-1] newest, [-k] the k-th newest; non-negative indexes count from the oldest
    assert r[-1] == 3
    assert r[-2] == 2
    assert r[-3] == 1
    assert r[0] == 1
    assert r[2] == 3
    with pytest.raises(IndexError):
        _ = r[3]
    with pytest.raises(IndexError):
        _ = r[-4]
    # last(n): chronological (oldest -> newest) slice of the newest n
    assert r.last(2) == [2, 3]
    assert r.last(10) == [1, 2, 3]  # clamped to len


def test_ring_wraparound_past_cap() -> None:
    r = Ring(4)
    for v in (1, 2, 3, 4, 5, 6):
        r.append(v)
    # cap 4 -> keeps the newest 4: 3,4,5,6
    assert len(r) == 4
    assert r[-1] == 6
    assert r[-4] == 3
    assert r[0] == 3
    assert r[3] == 6
    assert r.last(4) == [3, 4, 5, 6]
    with pytest.raises(IndexError):
        _ = r[4]
    with pytest.raises(IndexError):
        _ = r[-5]


# --------------------------------------------------------------------- 2. RollingStat


def test_rollingstat_mean_std_zscore() -> None:
    rs = RollingStat(3)
    for v in (1.0, 2.0, 3.0):
        rs.add(v)
    # mean = (1+2+3)/3 = 2; population var = ((1-2)^2+(2-2)^2+(3-2)^2)/3 = 2/3
    assert rs.mean == pytest.approx(2.0)
    assert rs.std == pytest.approx(math.sqrt(2.0 / 3.0))
    # z(4) = (4-2)/sqrt(2/3) = 2*sqrt(3/2) = sqrt(6)
    assert rs.zscore(4.0) == pytest.approx(math.sqrt(6.0))


def test_rollingstat_eviction_past_cap() -> None:
    rs = RollingStat(3)
    for v in (1.0, 2.0, 3.0, 7.0):  # cap 3 -> 1.0 evicted, window = [2,3,7]
        rs.add(v)
    assert len(rs) == 3
    # mean = (2+3+7)/3 = 4; var = ((2-4)^2+(3-4)^2+(7-4)^2)/3 = (4+1+9)/3 = 14/3
    assert rs.mean == pytest.approx(4.0)
    assert rs.std == pytest.approx(math.sqrt(14.0 / 3.0))
    assert rs.zscore(4.0) == pytest.approx(0.0)


def test_rollingstat_degenerate() -> None:
    rs = RollingStat(3)
    assert rs.mean == 0.0
    assert rs.std == 0.0
    rs.add(5.0)
    # single sample: mean = sample, sd undefined -> 0, zscore guarded -> 0
    assert rs.mean == pytest.approx(5.0)
    assert rs.std == 0.0
    assert rs.zscore(10.0) == 0.0


# ----------------------------------------------------------------- 3. percentile_rank


def test_percentile_rank_hand_cases() -> None:
    assert percentile_rank([], 1.0) == 50.0  # documented: 50.0 when no samples
    s = [1.0, 2.0, 3.0, 4.0]
    assert percentile_rank(s, 2.0) == pytest.approx(50.0)  # 2 of 4 <= 2
    assert percentile_rank(s, 0.5) == pytest.approx(0.0)  # 0 of 4
    assert percentile_rank(s, 4.0) == pytest.approx(100.0)  # 4 of 4 (<= is inclusive)
    assert percentile_rank(s, 3.5) == pytest.approx(75.0)  # 3 of 4
    assert percentile_rank([1.0, 1.0, 1.0], 1.0) == pytest.approx(100.0)  # ties all count


# ----------------------------------------------------------------- 4. 1s bar building


def test_1s_bar_ohlcv_and_boundary_close(cfg: Config) -> None:
    st = SymbolState(SYM, cfg)
    closed: list = []
    st.on_bar_1s.append(lambda _s, b: closed.append(b))

    st.on_event(tick(1000.1, 100.0, 2.0, side=BUY))
    assert st.cur_1s is not None
    assert len(st.bars_1s) == 0  # bar still open
    st.on_event(tick(1000.5, 102.0, 1.0, side=SELL))
    st.on_event(tick(1000.9, 99.0, 3.0, side=BUY))

    st.on_event(flush(1000.95))  # event-time has not crossed 1001 -> no close
    assert len(st.bars_1s) == 0
    assert closed == []

    st.on_event(flush(1001.0))  # boundary crossed -> bar [1000,1001) closes
    assert len(st.bars_1s) == 1
    assert len(closed) == 1
    b = st.bars_1s[-1]
    assert b is closed[0]
    assert b.ts == 1000.0
    # OHLC from prints 100 -> 102 -> 99
    assert (b.open, b.high, b.low, b.close) == (100.0, 102.0, 99.0, 99.0)
    assert b.volume == pytest.approx(6.0)  # 2+1+3
    assert b.buy_volume == pytest.approx(5.0)  # 2+3 taker-buy
    assert b.sell_volume == pytest.approx(1.0)  # 6-5
    assert b.delta == pytest.approx(4.0)  # 2*5-6
    assert b.ntrades == 3
    assert b.pv == pytest.approx(599.0)  # 100*2 + 102*1 + 99*3 = 200+102+297
    assert b.pv2 == pytest.approx(59807.0)  # 100^2*2 + 102^2*1 + 99^2*3 = 20000+10404+29403


def test_1s_gap_inserts_carry_forward_bars(cfg: Config) -> None:
    st = SymbolState(SYM, cfg)
    closed: list = []
    st.on_bar_1s.append(lambda _s, b: closed.append(b))

    st.on_event(tick(2000.4, 50.0, 1.0))
    st.on_event(tick(2005.4, 55.0, 2.0))  # 5s later -> closes 2000 + 4 empty carries
    # closed so far: real bar 2000 plus carry bars 2001..2004 (N=5s gap -> N-1=4 carries)
    assert len(closed) == 5
    assert [b.ts for b in closed] == [2000.0, 2001.0, 2002.0, 2003.0, 2004.0]
    real = closed[0]
    assert (real.open, real.high, real.low, real.close) == (50.0, 50.0, 50.0, 50.0)
    assert real.volume == pytest.approx(1.0)
    for carry in closed[1:]:
        # carry-forward: OHLC pinned to prior close, no volume/trades
        assert (carry.open, carry.high, carry.low, carry.close) == (50.0, 50.0, 50.0, 50.0)
        assert carry.volume == 0.0
        assert carry.buy_volume == 0.0
        assert carry.ntrades == 0
        assert carry.pv == 0.0
    # the 2005 bar is open and took the new print
    assert st.cur_1s is not None
    assert st.cur_1s.ts == 2005.0
    assert st.cur_1s.close == 55.0


# --------------------------------------------------------------------- 5. 1m roll-up


def test_1m_rollup_and_hook(cfg: Config) -> None:
    st = SymbolState(SYM, cfg)
    closed_1m: list = []
    st.on_bar_1m.append(lambda _s, b: closed_1m.append(b))

    st.on_event(tick(60.2, 10.0, 1.0, side=BUY))
    st.on_event(tick(61.2, 12.0, 2.0, side=SELL))
    st.on_event(tick(119.2, 11.0, 1.0, side=BUY))

    # closes 1s bar 119 but the minute is not done until a 1s bar of minute 120 closes
    st.on_event(flush(120.5))
    assert closed_1m == []

    st.on_event(flush(121.0))  # closes 1s bar 120 (carry) -> rolls minute 60 closed
    assert len(closed_1m) == 1
    m = closed_1m[0]
    assert st.bars_1m[-1] is m
    assert m.ts == 60.0
    # 1s bars in minute 60: ts60 (O/C 10, v1 buy), ts61 (12, v2 sell),
    # ts62..118 carries (close 12, v0), ts119 (11, v1 buy)
    # -> O=10 H=max(10,12,11)=12 L=min=10 C=11
    assert (m.open, m.high, m.low, m.close) == (10.0, 12.0, 10.0, 11.0)
    assert m.volume == pytest.approx(4.0)  # 1+2+1
    assert m.buy_volume == pytest.approx(2.0)  # 1+1
    assert m.sell_volume == pytest.approx(2.0)
    assert m.ntrades == 3
    assert m.pv == pytest.approx(45.0)  # 10*1 + 12*2 + 11*1
    assert m.pv2 == pytest.approx(509.0)  # 100*1 + 144*2 + 121*1
    # bars 60..120 inclusive closed as 1s bars; new 1m bar opened at ts 120
    assert len(st.bars_1s) == 61
    assert st.cur_1m is not None
    assert st.cur_1m.ts == 120.0


# --------------------------- 6. tick-window eviction + 10s bucket baselines


def test_tick_window_eviction(cfg: Config) -> None:
    cfg5 = replace(cfg, state=replace(cfg.state, tick_window_s=5.0))
    st = SymbolState(SYM, cfg5)
    for ts in (100.0, 102.0, 104.0):
        st.on_event(tick(ts, 10.0, 1.0))
    assert [t.ts_recv for t in st.ticks] == [100.0, 102.0, 104.0]  # cut 99 keeps all
    st.on_event(tick(106.0, 10.0, 1.0))  # cut = 101 -> evicts the 100.0 trade
    assert [t.ts_recv for t in st.ticks] == [102.0, 104.0, 106.0]
    st.on_event(tick(107.0, 10.0, 1.0))  # cut = 102: trade AT the cut is kept (strict <)
    assert [t.ts_recv for t in st.ticks] == [102.0, 104.0, 106.0, 107.0]
    st.on_event(tick(107.5, 10.0, 1.0))  # cut = 102.5 -> 102.0 evicted
    assert [t.ts_recv for t in st.ticks] == [104.0, 106.0, 107.0, 107.5]


def test_10s_bucket_baselines(cfg: Config) -> None:
    st = SymbolState(SYM, cfg)
    st.on_event(tick(100.0, 10.0, 2.0))  # bucket [100,110): vol 2, 1 trade
    st.on_event(tick(105.0, 10.0, 3.0))  # same bucket: vol 5, 2 trades
    st.on_event(tick(110.0, 10.0, 7.0))  # new bucket [110,120) -> pushes (5.0, 2)
    st.on_event(tick(121.0, 10.0, 1.0))  # new bucket [120,130) -> pushes (7.0, 1)
    # baseline samples: vol [5,7], trades [2,1]; the live [120,130) bucket is not pushed yet
    assert len(st.bucket_vol) == 2
    assert st.bucket_vol.mean == pytest.approx(6.0)  # (5+7)/2
    assert st.bucket_vol.std == pytest.approx(1.0)  # sqrt(((5-6)^2+(7-6)^2)/2) = 1
    assert st.bucket_vol.zscore(8.0) == pytest.approx(2.0)  # (8-6)/1
    assert st.bucket_trades.mean == pytest.approx(1.5)  # (2+1)/2
    assert st.bucket_trades.std == pytest.approx(0.5)  # sqrt(((2-1.5)^2+(1-1.5)^2)/2)


# ------------------------------------------------------------- 7. large-print p99


def test_large_print_p99_threshold_and_cache(cfg: Config) -> None:
    st = SymbolState(SYM, cfg)
    # 110 prints, sizes 1..110, all inside the 5s threshold-cache window that opens
    # with the first tick (stamp = 100.0, n = 1 < 100 -> threshold = inf). Even the
    # size-110 print is NOT flagged: the cached inf is reused until ts >= 105.
    for i in range(110):
        st.on_event(tick(100.0 + 0.04 * i, 100.0, float(i + 1)))
    assert st.last_large_print_ts == 0.0
    assert st._p99_size == math.inf

    # ts 106 (cache expired): a small print triggers the refresh on tick arrival.
    # n = 111 sizes {1..110, 5}; p99 rank = ceil(0.99*111) = 110th smallest = 109
    # (sorted asc = 1,2,3,4,5,5,6..110 -> index 109 zero-based = 109.0)
    st.on_event(tick(106.0, 100.0, 5.0, side=SELL))
    assert st._p99_size == pytest.approx(109.0)
    assert st.last_large_print_ts == 0.0  # 5 < 109: the refresher itself is not large

    # ts 107 (< 106+5): threshold still the cached 109; 200 >= 109 with n >= 100 -> flagged
    st.on_event(tick(107.0, 100.0, 200.0, side=SELL))
    assert st._p99_stamp == 106.0  # cache NOT refreshed by the 107 tick
    assert st.last_large_print_ts == 107.0
    assert st.last_large_print_side == SELL


def test_large_print_requires_100_sizes(cfg: Config) -> None:
    st = SymbolState(SYM, cfg)
    for i in range(50):
        st.on_event(tick(200.0 + i, 10.0, 1.0))
    st.on_event(tick(260.0, 10.0, 1000.0))  # refresh fires but n = 51 < 100 -> inf
    assert st._p99_size == math.inf
    assert st.last_large_print_ts == 0.0
    assert st.last_large_print_side == ""


# ------------------------------------------- 8. walls, wall-pull, imbalance clocks


def _levels(sizes: list[float], best: float, step: float) -> list[tuple[float, float]]:
    return [(best + i * step, sz) for i, sz in enumerate(sizes)]


def test_wall_detection_median_hand_computed(cfg: Config) -> None:
    st = SymbolState(SYM, cfg)
    st.on_event(tick(300.0, 100.0, 1.0))  # last_price = 100
    # 20 mirrored sizes: eighteen 2.0, one 9.9 (bid), one 11.0 (ask @105)
    # sorted asc = [2]*18, 9.9, 11 -> median = (srt[9]+srt[10])/2 = 2.0
    # threshold = wall_mult(5.0) * 2.0 = 10.0 -> 9.9 is NOT a wall, 11.0 IS
    bid_sizes = [2.0, 2.0, 2.0, 2.0, 2.0, 9.9, 2.0, 2.0, 2.0, 2.0]
    ask_sizes = [2.0, 2.0, 2.0, 2.0, 11.0, 2.0, 2.0, 2.0, 2.0, 2.0]
    bids = _levels(bid_sizes, best=100.0, step=-1.0)
    asks = _levels(ask_sizes, best=101.0, step=1.0)  # the 11.0 sits at price 105
    st.on_event(book(300.5, bids, asks, imbalance=0.5, spread_bps=3.5))
    assert len(st.walls) == 1
    w = st.walls[0]
    assert (w.side, w.price, w.size) == ("ask", 105.0, 11.0)
    # top-of-book mirrored from the snapshot
    assert (st.bid_px, st.bid_sz) == (100.0, 2.0)
    assert (st.ask_px, st.ask_sz) == (101.0, 2.0)
    assert st.spread_bps == 3.5
    assert st.book_imbalance == 0.5


def test_wall_pull_only_without_trade_through(cfg: Config) -> None:
    st = SymbolState(SYM, cfg)
    st.on_event(tick(300.0, 100.0, 1.0))
    flat = [2.0] * 10
    walled = [2.0, 2.0, 2.0, 2.0, 11.0, 2.0, 2.0, 2.0, 2.0, 2.0]  # wall at ask price 105
    bids = _levels(flat, best=100.0, step=-1.0)
    st.on_event(book(300.5, bids, _levels(walled, best=101.0, step=1.0)))
    assert [w.price for w in st.walls] == [105.0]

    # wall vanishes while last_price (100) never reached 105 -> wall-pull recorded
    st.on_event(book(301.0, bids, _levels(flat, best=101.0, step=1.0)))
    assert st.walls == []
    assert st.last_wall_pull_ts == 301.0
    assert st.last_wall_pull_side == "ask"

    # wall reappears, price trades THROUGH it (106 >= 105), then it vanishes:
    # that is consumption, not a pull -> timestamp unchanged
    st.on_event(book(302.0, bids, _levels(walled, best=101.0, step=1.0)))
    st.on_event(tick(303.0, 106.0, 1.0))
    st.on_event(book(304.0, bids, _levels(flat, best=101.0, step=1.0)))
    assert st.last_wall_pull_ts == 301.0
    assert st.last_wall_pull_side == "ask"


def test_imbalance_persistence_clocks(cfg: Config) -> None:
    # config.yaml rules.book_imbalance: imbalance_high 0.65 / imbalance_low 0.35
    st = SymbolState(SYM, cfg)
    st.on_event(book(200.0, [], [], imbalance=0.65))  # >= high (inclusive) -> clock starts
    assert st.imb_high_since == 200.0
    assert st.imb_low_since == 0.0
    st.on_event(book(205.0, [], [], imbalance=0.90))  # still high -> start time preserved
    assert st.imb_high_since == 200.0
    st.on_event(book(210.0, [], [], imbalance=0.64))  # dips below -> reset
    assert st.imb_high_since == 0.0
    st.on_event(book(212.0, [], [], imbalance=0.35))  # <= low (inclusive) -> low clock starts
    assert st.imb_low_since == 212.0
    st.on_event(book(215.0, [], [], imbalance=0.20))
    assert st.imb_low_since == 212.0
    st.on_event(book(218.0, [], [], imbalance=0.40))  # back above low -> reset
    assert st.imb_low_since == 0.0
    assert st.book_imbalance == 0.40


# ------------------------------------------------------- 9. prior_15m_extremes


def test_prior_15m_extremes_exclude_s(cfg: Config) -> None:
    st = SymbolState(SYM, cfg)
    assert st.prior_15m_extremes() == (-math.inf, math.inf)  # no bars yet
    st.on_event(tick(1000.5, 100.0, 1.0))
    st.on_event(tick(1001.5, 110.0, 1.0))
    st.on_event(tick(1002.5, 90.0, 1.0))
    st.on_event(tick(1003.5, 120.0, 1.0))
    st.on_event(flush(1004.0))  # closes bars 1000..1003
    assert len(st.bars_1s) == 4
    # highs/lows per closed bar: 1000:100, 1001:110, 1002:90, 1003:120
    assert st.prior_15m_extremes() == (120.0, 90.0)
    # exclude_s=1 drops bar 1003 (the fresh 120 sweep) -> hi 110, lo 90
    assert st.prior_15m_extremes(exclude_s=1.0) == (110.0, 90.0)
    # exclude_s=2 drops bars 1003+1002 -> hi 110, lo 100
    assert st.prior_15m_extremes(exclude_s=2.0) == (110.0, 100.0)


def test_prior_15m_carry_bars_mark_presence(cfg: Config) -> None:
    st = SymbolState(SYM, cfg)
    st.on_event(tick(2000.4, 50.0, 1.0))
    st.on_event(tick(2003.4, 55.0, 1.0))
    st.on_event(flush(2004.0))  # bars: 2000 real(50), 2001/2002 carries(close 50), 2003 real(55)
    assert st.prior_15m_extremes() == (55.0, 50.0)
    # excluding the 55 print leaves only the 50 bar + carries; carries count via close
    assert st.prior_15m_extremes(exclude_s=1.0) == (50.0, 50.0)


# ------------------------------------------------------ 10. session accumulators


def test_session_accumulators(cfg: Config) -> None:
    st = SymbolState(SYM, cfg)
    st.on_event(tick(100.0, 100.0, 2.0, side=BUY))
    st.on_event(tick(100.5, 105.0, 1.0, side=SELL))
    st.on_event(tick(101.2, 95.0, 4.0, side=BUY))
    assert st.session_open == 100.0  # first print
    assert st.session_high == 105.0
    assert st.session_low == 95.0
    assert st.last_price == 95.0
    assert st.last_tick_ts == 101.2
    # CVD = +2 - 1 + 4 = 5 (signed by aggressor side)
    assert st.session_cvd == pytest.approx(5.0)
    assert st.session_vol == pytest.approx(7.0)  # 2+1+4
    # pv = 100*2 + 105*1 + 95*4 = 200+105+380 = 685
    assert st.session_pv == pytest.approx(685.0)
    # pv2 = 100^2*2 + 105^2*1 + 95^2*4 = 20000+11025+36100 = 67125
    assert st.session_pv2 == pytest.approx(67125.0)
