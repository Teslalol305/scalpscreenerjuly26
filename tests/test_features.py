"""Hand-computed tests for tapescreen.core.features via SymbolState + FeatureEngine.

Every expectation is derived from the documented formulas (module docstrings +
build spec) independently of the implementation; the arithmetic is shown in
comments. Events are constructed directly and driven through
SymbolState.on_event with the FeatureEngine attached BEFORE any event so the
1s/1m bar hooks fire. The pipeline clock is ts_recv: bars close only when a
LATER event advances event-time past the boundary, so every scenario ends with
a flush event (a Bbo with empty sides, or a Tick) beyond the last boundary.
"""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from tapescreen.core.events import BUY, SELL, Bbo, PerpCtx, Tick
from tapescreen.core.features import BarWindow, FeatureEngine, Wilder
from tapescreen.core.state import Bar, SymbolState

S = "TESTCOIN"


def tick(ts: float, px: float, sz: float = 1.0, side: str = BUY) -> Tick:
    return Tick(ts, ts, ts, S, px, sz, side, 0, "hl")


def flush(ts: float) -> Bbo:
    """Pure clock-advance event: both sides empty, so _on_bbo is a no-op."""
    return Bbo(ts, ts, ts, S, 0.0, 0.0, 0.0, 0.0)


def ctx(ts: float, oi: float = 0.0, funding: float = 0.0, mark: float = 100.0) -> PerpCtx:
    return PerpCtx(ts, ts, ts, S, mark, mark, mark, funding, oi, 0.0)


def engine(cfg) -> tuple[SymbolState, FeatureEngine]:
    st = SymbolState(S, cfg)
    fe = FeatureEngine(st)  # attaches on_bar_1s / on_bar_1m hooks before any event
    return st, fe


# --------------------------------------------------------------------- 1. BarWindow


def test_barwindow_sums_cvd_vwap_sd_and_eviction():
    w = BarWindow(3)
    assert w.vwap_sd() == (0.0, 0.0)  # empty window
    # Bar fields: (ts, open, high, low, close, volume, buy_volume, ntrades, pv, pv2)
    w.add(Bar(0.0, 100, 100, 100, 100, 2.0, 2.0, 1, 200.0, 20000.0))  # 2 @ 100, all buy
    w.add(Bar(1.0, 110, 110, 110, 110, 3.0, 0.0, 1, 330.0, 36300.0))  # 3 @ 110, all sell
    assert not w.full
    w.add(Bar(2.0, 120, 120, 120, 120, 5.0, 5.0, 2, 600.0, 72000.0))  # 5 @ 120, all buy
    assert w.full
    # volume = 2+3+5 = 10 ; buy = 2+0+5 = 7 ; CVD = sum(buy-sell) = 2*7-10 = 4 ; ntrades = 1+1+2 = 4
    assert w.volume == pytest.approx(10.0)
    assert w.buy_volume == pytest.approx(7.0)
    assert w.cvd == pytest.approx(4.0)
    assert w.ntrades == 4
    # vwap = sum(p*v)/sum(v) = (200+330+600)/10 = 113
    # var  = sum(p^2 v)/sum(v) - vwap^2 = (20000+36300+72000)/10 - 113^2 = 12830 - 12769 = 61
    vwap, sd = w.vwap_sd()
    assert vwap == pytest.approx(113.0)
    assert sd == pytest.approx(math.sqrt(61.0))
    # eviction: 4th bar drops the first (2 @ 100 buy)
    w.add(Bar(3.0, 130, 130, 130, 130, 1.0, 0.0, 1, 130.0, 16900.0))  # 1 @ 130, sell
    # volume = 3+5+1 = 9 ; buy = 0+5+0 = 5 ; cvd = 2*5-9 = 1 ; ntrades = 1+2+1 = 4
    # pv = 330+600+130 = 1060 ; pv2 = 36300+72000+16900 = 125200
    # vwap = 1060/9 ; var = 125200/9 - (1060/9)^2 = (1126800-1123600)/81 = 3200/81
    assert w.volume == pytest.approx(9.0)
    assert w.buy_volume == pytest.approx(5.0)
    assert w.cvd == pytest.approx(1.0)
    assert w.ntrades == 4
    vwap, sd = w.vwap_sd()
    assert vwap == pytest.approx(1060.0 / 9.0)
    assert sd == pytest.approx(math.sqrt(3200.0 / 81.0))


# ----------------------------------------------------------------------- 2. Wilder


def test_wilder_sma_seed_then_smoothing():
    w = Wilder(3)
    # seed (SMA) phase: running mean of the first n=3 samples
    assert w.add(5.0) == pytest.approx(5.0)  # mean(5) = 5
    assert w.add(3.0) == pytest.approx(4.0)  # mean(5,3) = 4
    assert w.add(4.0) == pytest.approx(4.0)  # mean(5,3,4) = 4  -> seed complete
    # smoothing phase: value = (prev*(n-1) + v)/n
    assert w.add(6.0) == pytest.approx(14.0 / 3.0)  # (4*2+6)/3 = 14/3
    assert w.add(2.0) == pytest.approx(34.0 / 9.0)  # ((14/3)*2+2)/3 = 34/9


def test_wilder_zero_led_seed_then_smoothing():
    # With zero-leading samples the seed phase survives (value stays 0.0), so the
    # documented behavior and the implementation agree; this pins the transition.
    w = Wilder(3)
    assert w.add(0.0) == pytest.approx(0.0)  # seed mean(0) = 0
    assert w.add(0.0) == pytest.approx(0.0)  # seed mean(0,0) = 0
    assert w.add(3.0) == pytest.approx(1.0)  # seed mean(0,0,3) = 1 -> seed complete
    assert w.ready
    assert w.add(6.0) == pytest.approx(8.0 / 3.0)  # (1*2+6)/3 = 8/3
    assert w.add(3.0) == pytest.approx(25.0 / 9.0)  # ((8/3)*2+3)/3 = 25/9


# ------------------------------------------------------- 3. ATR / RSI / EMA via 1m bars


def test_atr_rsi_ema_from_1m_closes(cfg):
    # One trade per minute -> deterministic 1m OHLC (O=H=L=C = tick price), except
    # minute 0 which gets two ticks (100 then 102) so its TR = H-L = 2 as well.
    # Closes: c0=102, then +2/min through c13=128, then c14=121 (-7), c15=125 (+4).
    # TR_m (pc = prev close; pc for bar 0 = its open):
    #   TR_0 = max(102-100, |102-100|, |100-100|) = 2 ; TR_1..13 = |Δclose| = 2
    #   TR_14 = 7 ; TR_15 = 4
    # The first 14 TR samples are ALL 2, so SMA-seed (spec) and the current
    # implementation agree exactly (value = 2 after 14 bars) -- this fixture is
    # deliberately insensitive to the Wilder seeding bug xfail'd above.
    # ATR_14bars = 2 ; ATR_15 = (2*13+7)/14 = 33/14 ; ATR_16 = ((33/14)*13+4)/14 = 485/196
    # RSI gains: 2 x14, then 0, then 4 ; losses: 0 x14, then 7, then 0
    #   avg_gain: 2 -> (2*13+0)/14 = 13/7 -> ((13/7)*13+4)/14 = 197/98
    #   avg_loss: 0 -> (0*13+7)/14 = 1/2  -> ((1/2)*13+0)/14 = 13/28
    #   RS = (197/98)/(13/28) = 394/91 ; RSI = 100 - 100/(1+RS) = 100*394/485 = 81.23711340..
    st, fe = engine(cfg)
    wilder_log: list[tuple[float, float, float]] = []
    st.on_bar_1m.append(lambda s, b: wilder_log.append((fe.atr.value, fe.rsi_gain.value, fe.rsi_loss.value)))

    t0 = 60000.0  # minute-aligned
    st.on_event(tick(t0 + 0.2, 100.0))
    st.on_event(tick(t0 + 0.7, 102.0))
    for m in range(1, 14):  # minutes 1..13: close = 102 + 2m (104..128)
        st.on_event(tick(t0 + 60.0 * m + 0.5, 102.0 + 2.0 * m))
    st.on_event(tick(t0 + 60.0 * 14 + 0.5, 121.0))  # minute 14: drop 7
    st.on_event(tick(t0 + 60.0 * 15 + 0.5, 125.0))  # minute 15: up 4
    st.on_event(flush(t0 + 60.0 * 16 + 1.0))  # closes 1s bar t0+960 -> closes 1m bar 15

    assert len(st.bars_1m) == 16
    b0 = st.bars_1m[0]
    assert (b0.open, b0.high, b0.low, b0.close) == (100.0, 102.0, 100.0, 102.0)

    assert len(wilder_log) == 16
    assert wilder_log[0][0] == pytest.approx(2.0)  # seed mean(2) = 2
    assert wilder_log[13] == pytest.approx((2.0, 2.0, 0.0))  # after 14 bars
    assert wilder_log[14] == pytest.approx((33.0 / 14.0, 13.0 / 7.0, 0.5))
    assert wilder_log[15] == pytest.approx((485.0 / 196.0, 197.0 / 98.0, 13.0 / 28.0))

    s = fe.snapshot
    assert s.atr_1m == pytest.approx(485.0 / 196.0)  # = 2.47448979..
    assert s.rsi_14 == pytest.approx(100.0 * 394.0 / 485.0)  # = 81.23711340..

    # EMA(n): seed = first close, k = 2/(n+1), e += (c-e)*k  (reference loop = the formula)
    closes = [102.0] + [102.0 + 2.0 * m for m in range(1, 14)] + [121.0, 125.0]

    def ema_ref(vals: list[float], n: int) -> float:
        k = 2.0 / (n + 1)
        e = vals[0]
        for c in vals[1:]:
            e += (c - e) * k
        return e

    # hand-check of the first EMA9 steps (k=0.2): 102 -> 102+.2*2=102.4 -> 102.4+.2*3.6=103.12
    # -> 103.12+.2*4.88=104.096 ; chain ends at 121.4414749767...
    assert s.ema_9 == pytest.approx(ema_ref(closes, 9))
    assert abs(s.ema_9 - 121.4414750) < 1e-6
    # EMA21 (k=1/11): 102 -> (10*102+104)/11 = 1124/11 = 102.181818.. ; ends near 115.407800
    assert s.ema_21 == pytest.approx(ema_ref(closes, 21))
    assert abs(s.ema_21 - 115.4078) < 1e-3

    assert s.last_1m_range == 0.0  # bar 15 traded a single price
    assert s.last_1m_dir == 1  # close >= open


# ------------------------------------------------- 4. Bollinger width / pctl / squeeze


def test_bb_width_percentile_and_squeeze_run(cfg):
    # Drive 1m closes via seed_bars_1m (fires the same on_bar_1m hook chain).
    # Bars 1..20 alternate 100,110 ; bars 21..35 close at 105.
    st, fe = engine(cfg)

    def bar1m(i: int, c: float) -> Bar:
        return Bar(i * 60.0, c, c, c, c, 1.0, 1.0, 1, c, c * c)

    closes = [100.0, 110.0] * 10  # bars 1..20
    st.seed_bars_1m([bar1m(i, c) for i, c in enumerate(closes)])
    # Full window at bar 20: 10x100 + 10x110 -> mean 105, var = (10*100^2+10*110^2)/20 - 105^2
    #   = 11050 - 11025 = 25 -> sd 5 -> width = 4*5/105 = 4/21 = 0.19047619..
    assert fe.bb_width == pytest.approx(4.0 / 21.0)
    # Session samples so far: bars 2..20 -> 19 widths. Even-length alternating windows
    # all give width exactly 4/21; odd windows of n=2m+1 give 4*sqrt(m(m+1))/(21m+10),
    # which is < 4/21 for m<=4 (bars 3,5,7,9) and > 4/21 for m>=5 (bars 11,13,15,17,19).
    # count(<= 4/21) = 10 evens + 4 small odds = 14 -> pctl = 100*14/19 = 73.684..
    assert fe.bb_width_pctl == pytest.approx(1400.0 / 19.0)
    assert fe.squeeze_run_min == 0  # pctl 73.7 > threshold 10

    st.seed_bars_1m([bar1m(20 + k, 105.0) for k in range(1, 16)])  # bars 21..35
    # Window at bar 35 = closes 16..35 = [110,100,110,100,110] + 15x105:
    #   sum = 330+200+1575 = 2105 -> mean 105.25
    #   sumsq = 3*12100 + 2*10000 + 15*11025 = 221675
    #   var = 221675/20 - 105.25^2 = 11083.75 - 11077.5625 = 6.1875
    #   width = 4*sqrt(6.1875)/105.25 = 0.0945358..
    assert fe.bb_width == pytest.approx(4.0 * math.sqrt(6.1875) / 105.25)
    # Tail widths (bars 21..35) are strictly decreasing (var = 25-1.25k even / 24.9375-1.25k odd),
    # and all alternating-phase widths are >= 0.1824 > final 0.0945, so the final width is the
    # unique session minimum: pctl = 100 * 1/34 = 2.941..  (34 samples = bars 2..35)
    assert len(st.bbw_samples) == 34
    assert fe.bb_width_pctl == pytest.approx(100.0 / 34.0)
    # Every bar 21..35 had the (then) unique-minimum width -> pctl = 100/(19+k):
    # k=1 -> 10.0 (bar 3's width 0.18248 <= 0.184966 also counts: 100*2/20 = 10.0 <= 10, edge),
    # k>=2 -> < 10, window full throughout -> squeeze_run_min increments 15 times.
    assert fe.squeeze_run_min == 15

    # snapshot picks the values up on the next 1s close
    st.on_event(tick(50000.2, 105.0))
    st.on_event(flush(50001.0))
    s = fe.snapshot
    assert s.bb_width == pytest.approx(4.0 * math.sqrt(6.1875) / 105.25)
    assert s.bb_width_pctl == pytest.approx(100.0 / 34.0)
    assert s.squeeze_run_min == 15


# -------------------------------------------------------------------------- 5. ROC


def test_roc_staircase(cfg):
    # One tick per second: price 100+i at t0+i+0.5 for i = 0..70.
    # The final rebuild fires while tick 70 advances the clock, i.e. when the 1s bar
    # for second 69 closes, BEFORE tick 70 is applied: last_price = 100+69 = 169 and
    # closed bars are seconds 0..69.
    st, fe = engine(cfg)
    t0 = 2000.0
    for i in range(71):
        st.on_event(tick(t0 + i + 0.5, 100.0 + i))
    s = fe.snapshot
    assert s.price == pytest.approx(169.0)
    # ROC(w) = last_price / close_{w sec ago} - 1 ; at now ~= 2070.5:
    #   10s ago = 2060.5 -> in bar [2060,2061) -> close 160 -> 169/160-1 = 9/160
    #   30s ago -> bar 2040 close 140 -> 29/140 ; 60s ago -> bar 2010 close 110 -> 59/110
    assert s.roc_10s == pytest.approx(9.0 / 160.0)
    assert s.roc_30s == pytest.approx(29.0 / 140.0)
    assert s.roc_60s == pytest.approx(59.0 / 110.0)
    # only 70 closed 1s bars: close_price_ago is documented to return 0.0 until the
    # full lookback exists ("early-session ROCs read 0"), so ROC(300) reads 0.0
    assert s.roc_5m == 0.0
    # roc30_stat receives one sample per 1s close -> 70 samples, non-degenerate sigma
    assert len(st.roc30_stat) == 70
    assert s.roc30_sigma > 0.0
    assert s.roc30_sigma == pytest.approx(st.roc30_stat.std)


# ------------------------------------------------------------------ 6. vol_z / tps_z


def test_vol_z_and_tps_z_from_10s_buckets(cfg):
    # Non-overlapping aligned 10s buckets; a bucket is pushed into the baseline when
    # the first tick of a LATER bucket arrives.
    st, fe = engine(cfg)
    st.on_event(tick(10000.5, 100.0, 4.0))  # bucket [10000,10010): vol 10, 2 trades
    st.on_event(tick(10003.5, 100.0, 6.0))
    for i in range(4):  # bucket [10010,10020): vol 20, 4 trades
        st.on_event(tick(10010.5 + 2.0 * i, 100.0, 5.0))
    for i in range(6):  # bucket [10020,10030): vol 30, 6 trades
        st.on_event(tick(10020.5 + i, 100.0, 5.0))
    st.on_event(tick(10030.5, 100.0, 40.0))  # pushes bucket 3; current bucket open
    st.on_event(flush(10040.0))  # closes 1s bars 10030..10039

    # Baseline buckets: vols [10,20,30] -> mean 20, population sd = sqrt((100+0+100)/3)
    #   = sqrt(200/3) ; trades [2,4,6] -> mean 4, sd = sqrt((4+0+4)/3) = sqrt(8/3)
    # Final rebuild is the close of bar 10039: the last-10s window is bars 10030..10039,
    # which contains only the size-40 print -> vol_10s = 40, ntrades = 1.
    # vol_z = (40-20)/sqrt(200/3) = sqrt(6) = 2.449489.. ; tps_z = (1-4)/sqrt(8/3) = -1.837117..
    s = fe.snapshot
    assert len(st.bucket_vol) == 3
    assert s.vol_10s == pytest.approx(40.0)
    assert s.vol_z == pytest.approx(math.sqrt(6.0))
    assert s.tps_z == pytest.approx(-3.0 / math.sqrt(8.0 / 3.0))
    # previous rebuild (bar 10038) had the same window contents -> same z
    assert s.vol_z_prev == pytest.approx(math.sqrt(6.0))
    assert s.vol_z_5m_peak >= s.vol_z
    assert s.warming  # 3 buckets << 2/3 of the 180-bucket baseline


# --------------------------------------------------------------- 7. CVD and slopes


def test_cvd_windows_and_slopes(cfg):
    # Seconds 0..59: BUY 2 each -> per-bar delta +2. Seconds 60..119: even BUY 3 /
    # odd SELL 1 -> per-pair delta +2.
    st, fe = engine(cfg)
    t0 = 90000.0
    for i in range(60):
        st.on_event(tick(t0 + i + 0.5, 100.0, 2.0, BUY))
    for i in range(60, 120):
        if i % 2 == 0:
            st.on_event(tick(t0 + i + 0.5, 100.0, 3.0, BUY))
        else:
            st.on_event(tick(t0 + i + 0.5, 100.0, 1.0, SELL))
    st.on_event(flush(t0 + 120.0))  # closes bars ..119

    s = fe.snapshot
    # w60 = bars 60..119: 30 buys of 3 and 30 sells of 1 -> cvd = 90-30 = 60
    assert s.cvd_1m == pytest.approx(60.0)
    assert s.cvd_slope_1m == pytest.approx(1.0)  # 60/60
    # w60 as of 60s earlier (after bars 0..59, all +2) was +120 -> prev slope 120/60 = 2
    assert s.cvd_slope_1m_prev == pytest.approx(2.0)
    # 5m window and session hold everything: 120 + 60 = 180
    assert s.cvd_5m == pytest.approx(180.0)
    assert s.cvd_session == pytest.approx(180.0)


# --------------------------------------------------------------- 8. agg imbalance


def test_agg_imbalance_60s(cfg):
    # 20 seconds: even seconds BUY 1, odd seconds SELL 3 -> buy 10 of total 40.
    st, fe = engine(cfg)
    t0 = 11000.0
    for i in range(20):
        side, sz = (BUY, 1.0) if i % 2 == 0 else (SELL, 3.0)
        st.on_event(tick(t0 + i + 0.5, 100.0, sz, side))
    st.on_event(flush(t0 + 20.0))
    s = fe.snapshot
    # agg_imbalance_60s = buy_volume/volume over the (partial) 60s window = 10/40 = 0.25
    assert s.agg_imbalance_60s == pytest.approx(0.25)
    assert s.cvd_1m == pytest.approx(2.0 * 10.0 - 40.0)  # -20


# ------------------------------------------------------------------ 9. burst detector


def test_burst_fires_on_k_same_side_within_window(cfg):
    assert cfg.features.burst_k == 6 and cfg.features.burst_window_s == 2.0
    st, fe = engine(cfg)
    for j in range(6):  # 6 BUYS spanning 1.5s (3000.1 .. 3001.6)
        st.on_event(tick(3000.1 + 0.3 * j, 100.0, 1.0, BUY))
    st.on_event(flush(3002.0))  # rebuild at now=3002: oldest tick is 1.9s old -> all 6 count
    assert fe.snapshot.burst_side == "buy"


def test_burst_mixed_sides_does_not_fire(cfg):
    st, fe = engine(cfg)
    for j in range(6):  # alternate buy/sell: 3 + 3, neither reaches k=6
        st.on_event(tick(4000.1 + 0.3 * j, 100.0, 1.0, BUY if j % 2 == 0 else SELL))
    st.on_event(flush(4002.0))
    assert fe.snapshot.burst_side == ""


def test_burst_five_same_side_does_not_fire(cfg):
    st, fe = engine(cfg)
    for j in range(5):  # one short of k=6
        st.on_event(tick(4100.1 + 0.3 * j, 100.0, 1.0, BUY))
    st.on_event(flush(4102.0))
    assert fe.snapshot.burst_side == ""


# ----------------------------------------------------------------- 10. session VWAP


def test_session_vwap_sd_and_dist_sigma(cfg):
    st, fe = engine(cfg)
    st.on_event(tick(5000.2, 100.0, 1.0, BUY))
    st.on_event(tick(5000.5, 110.0, 2.0, BUY))
    st.on_event(tick(5000.8, 120.0, 1.0, SELL))
    st.on_event(flush(5001.0))  # closes bar 5000 -> rebuild with last_price 120
    s = fe.snapshot
    # vwap = (100*1 + 110*2 + 120*1)/4 = 440/4 = 110
    # var  = (100^2*1 + 110^2*2 + 120^2*1)/4 - 110^2 = 48600/4 - 12100 = 50 -> sd = sqrt(50)
    # dist = (120 - 110)/sqrt(50) = 10/sqrt(50) = sqrt(2)
    assert s.vwap_session == pytest.approx(110.0)
    assert s.vwap_session_sd == pytest.approx(math.sqrt(50.0))
    assert s.vwap_dist_sigma == pytest.approx(math.sqrt(2.0))
    # 30m bar-window VWAP sees the same single-bar tape
    assert s.vwap_30m == pytest.approx(110.0)
    assert s.vwap_30m_sd == pytest.approx(math.sqrt(50.0))
    assert s.cvd_session == pytest.approx(1.0 + 2.0 - 1.0)


# ------------------------------------------------------ 11. dOI + funding percentile


def test_doi_and_funding_percentiles(cfg):
    st, fe = engine(cfg)
    # 7-day funding seed: 19 one-minute samples with |rate| = 1e-4 .. 19e-4, alternating sign.
    st.seed_funding([(6000.0 + 60.0 * i, ((-1.0) ** i) * (i + 1) * 1e-4) for i in range(19)])

    # OI path: +10 every 30s from 7200 (OI 1000) to 7410 (OI 1120), funding 0.002 throughout.
    oi_path = [
        (7200.0, 1000.0), (7230.0, 1010.0), (7260.0, 1030.0), (7290.0, 1040.0),
        (7320.0, 1060.0), (7350.0, 1090.0), (7380.0, 1100.0), (7410.0, 1120.0),
    ]
    st.on_event(ctx(7200.0, oi=1000.0, funding=0.002))
    st.on_event(tick(7200.5, 100.0))  # start the 1s bar clock
    for ts, oi in oi_path[1:]:
        st.on_event(ctx(ts, oi=oi, funding=0.002))
    st.on_event(tick(7411.5, 100.0))  # closes bar 7410 -> final rebuild sees full OI history

    s = fe.snapshot
    # OI history trim is trailing 330s: at ts 7410 the cut is 7080, so even the first
    # sample (7200,1000) is retained. dOI(w) = OI_now - OI at first sample >= now-w,
    # with now = last sample ts (7410, OI 1120):
    #   dOI(60):  target 7350 -> first >= 7350 is (7350,1090) -> 1120-1090 = 30
    #   dOI(300): target 7110 -> first >= 7110 is (7200,1000) -> 1120-1000 = 120
    assert s.doi_1m == pytest.approx(30.0)
    assert s.doi_5m == pytest.approx(120.0)
    # 1m closes fired when 1s bars 7260/7320/7380 closed (during the advances of the
    # ctx events at 7290/7350/7410, i.e. BEFORE those samples were appended):
    #   close 1: last OI sample (7260,1030), target 6960 -> first is (7200,1000) -> |dOI5| = 30
    #   close 2: last (7320,1060), target 7020 -> (7200,1000) -> 60
    #   close 3: last (7380,1100), target 7080 -> (7200,1000) -> 100
    assert st.doi5_samples == pytest.approx([30.0, 60.0, 100.0])
    # p95 of [30,60,100]: idx = ceil(0.95*3)-1 = 2 -> 100
    assert s.doi5_session_p95 == pytest.approx(100.0)

    # funding: ctx sampled at most once per 60s -> appended at 7200/7260/7320/7380 only
    # (7230/7290/7350/7410 are 30s after the previous sample) -> 19 seeds + 4x 0.002 = 23.
    assert len(st.funding_hist) == 23
    assert s.funding == pytest.approx(0.002)
    # percentile_rank of |0.002| vs the 23 |rates| (all <= 0.002) = 100.0
    assert s.funding_pctl_7d == pytest.approx(100.0)
    # p95: sorted abs rates = [1e-4..19e-4, 0.002 x4]; idx = min(22, ceil(0.95*23)-1) = 21 -> 0.002
    assert s.funding_abs_p95_7d == pytest.approx(0.002)


# ----------------------------------------------------------------------- 12. basis


def test_basis_bps_mark_vs_bbo_mid(cfg):
    st, fe = engine(cfg)
    st.on_event(tick(6000.2, 100.0))
    st.on_event(Bbo(6000.4, 6000.4, 6000.4, S, 99.0, 5.0, 101.0, 5.0))
    st.on_event(ctx(6000.6, oi=1000.0, funding=1e-4, mark=100.5))
    st.on_event(tick(6001.5, 100.0))  # closes bar 6000 -> rebuild
    s = fe.snapshot
    # mid = (99+101)/2 = 100 ; basis = (100.5-100)/100 * 1e4 = 50 bps
    assert s.basis_bps == pytest.approx(50.0)
    # spread = (101-99)/100 * 1e4 = 200 bps
    assert s.spread_bps == pytest.approx(200.0)
    assert s.funding == pytest.approx(1e-4)


# --------------------------------------------------------- 13. new_1m_bar + warming


def test_new_1m_bar_flag_exactly_on_first_snapshot_after_close(cfg):
    st, fe = engine(cfg)
    records: list[tuple[float, bool]] = []
    st.on_bar_1s.append(lambda s, b: records.append((b.ts, fe.snapshot.new_1m_bar)))
    st.on_event(tick(60000.5, 100.0))
    st.on_event(flush(60062.0))  # closes 1s bars 60000..60061
    # The 1m bar [60000,60060) closes while the 1s bar at 60060 closes; the snapshot
    # rebuilt for that 1s close (and only that one) carries new_1m_bar=True.
    assert len(records) == 62
    assert [ts for ts, is_new in records if is_new] == [60060.0]
    assert records[-1] == (60061.0, False)  # flag resets on the very next snapshot


def test_warming_clears_at_two_thirds_of_bucket_baseline(cfg):
    # Shrink the baseline so the threshold is small: 90s -> 9 buckets, 2/3 = 6.
    small = replace(cfg, state=replace(cfg.state, baseline_window_s=90.0))
    st, fe = engine(small)
    assert st.bucket_vol._q.maxlen == 9
    records: list[tuple[float, bool]] = []
    st.on_bar_1s.append(lambda s, b: records.append((b.ts, fe.snapshot.warming)))
    for i in range(62):  # one tick per second from 20000 (10s-aligned)
        st.on_event(tick(20000.5 + i, 100.0, 1.0))
    flags = dict(records)
    # Bucket k is pushed by the first tick of bucket k+1: the 6th push (bucket
    # [20050,20060)) happens at tick 20060.5, AFTER the rebuild for bar 20059
    # (5 buckets -> warming) and before the rebuild for bar 20060 (6 >= 6 -> warm).
    assert flags[20059.0] is True
    assert flags[20060.0] is False


# ------------------------------------------------------------------ sanity: config


def test_config_thresholds_used_by_fixtures(cfg):
    # the hand computations above assume these repo-config values
    assert cfg.features.burst_k == 6
    assert cfg.features.burst_window_s == 2.0
    assert cfg.state.baseline_window_s == 1800.0
    assert float(cfg.rules["squeeze_release"]["bb_width_pctl_max"]) == 10.0
