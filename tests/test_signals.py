"""Signal rules R1-R7 + composite scoring, tiers, and cooldowns.

Scenarios drive the full pipeline through Engine(cfg) (no db): Tick/Bbo/BookTop/
PerpCtx events with ts_recv in seconds; bars close only when a later event
advances event-time past a boundary, so tapes end with a flush (empty Bbo).
Baselines (10s activity buckets, ROC sigma windows) are primed directly on the
SymbolState for controlled thresholds instead of feeding 30 minutes of tape.
Composite decay/multiplier/cooldown math is tested against Composite directly
with hand-built FeatureSnapshots and hand-inserted Fired entries.
"""

from __future__ import annotations

import pytest

from tapescreen.config import Config, load_config
from tapescreen.core.engine import Engine
from tapescreen.core.events import BUY, SELL, Bbo, BookTop, PerpCtx, Tick
from tapescreen.core.features import FeatureSnapshot
from tapescreen.core.signals.composite import Composite, Fired
from tapescreen.core.state import SymbolState
from tests.conftest import REPO

SYM = "BTC"
T0 = 1_750_000_000.0  # % 10 == 0 (10s-bucket aligned); % 60 == 40
T0M = 1_750_000_020.0  # minute-aligned (% 60 == 0), for 1m-bar-driven scenarios

BASE_KEYS = {
    "price", "vwap_dist_sigma", "vol_z", "cvd_1m", "cvd_5m",
    "book_imbalance", "spread_bps", "doi_5m", "funding", "invalidation",
}


# ------------------------------------------------------------------ event builders


def tick(ts: float, price: float, size: float, side: str = BUY) -> Tick:
    return Tick(ts_exch=ts, ts_recv=ts, ts_mono=ts, symbol=SYM, price=price,
                size=size, side=side, trade_id=0, venue="hyperliquid")


def bbo(ts: float, bid_px: float, ask_px: float, sz: float = 1.0) -> Bbo:
    return Bbo(ts=ts, ts_recv=ts, ts_mono=ts, symbol=SYM,
               bid_px=bid_px, bid_sz=sz, ask_px=ask_px, ask_sz=sz)


def flush(ts: float) -> Bbo:
    """Empty Bbo: advances event-time (closing due bars) without touching the book."""
    return bbo(ts, 0.0, 0.0, 0.0)


def book(ts: float, imbalance: float) -> BookTop:
    """BookTop with empty levels: sets imbalance/persistence clocks only."""
    return BookTop(ts=ts, ts_recv=ts, ts_mono=ts, symbol=SYM, bids=[], asks=[],
                   spread_bps=1.0, imbalance=imbalance)


def perp(ts: float, funding: float, oi: float) -> PerpCtx:
    return PerpCtx(ts=ts, ts_recv=ts, ts_mono=ts, symbol=SYM, mark=100.0, oracle=100.0,
                   mid=100.0, funding_rate=funding, open_interest=oi, premium=0.0)


# ------------------------------------------------------------------ config / engine


def fresh_cfg(baseline_s: float = 60.0) -> Config:
    """Fresh mutable config per test (never the session fixture); BTC only.

    baseline_window_s=60 -> 6 ten-second buckets; warming clears once the bucket
    baseline has >= 6 samples (max(6, 6*2//3) = 6), i.e. immediately after priming.
    """
    cfg = load_config(REPO / "config.yaml")
    cfg.state.baseline_window_s = baseline_s
    cfg.symbols = {SYM: cfg.symbols[SYM]}
    return cfg


def prime_baselines(st: SymbolState) -> None:
    """Fill the 10s activity baselines: vol mean 1.5 / sd 0.5, trades mean 15 / sd 5."""
    for j in range(30):
        st.bucket_vol.add(1.0 if j % 2 == 0 else 2.0)
        st.bucket_trades.add(10.0 if j % 2 == 0 else 20.0)


def prime_roc30(st: SymbolState, scale: float = 1e-4) -> None:
    for j in range(60):
        st.roc30_stat.add(scale if j % 2 == 0 else -scale)


def prime_roc5m(st: SymbolState, scale: float = 1e-3) -> None:
    for j in range(60):
        st.roc5m_stat.add(scale if j % 2 == 0 else -scale)


def fired(eng: Engine, rule: str) -> list[dict]:
    return [row for row in eng.signal_feed if row["rule"] == rule]


def assert_base_snapshot(snap: dict) -> None:
    missing = BASE_KEYS - snap.keys()
    assert not missing, f"snapshot missing base keys: {missing}"


# =====================================================================
# R1 momentum_ignition
# =====================================================================


def _momentum_tape(spread_ask: float) -> Engine:
    """35s quiet tape at 100 with a tight/wide book, then a 1% buy burst."""
    eng = Engine(fresh_cfg())
    st = eng.states[SYM]
    eng.on_event(bbo(T0 + 0.05, 100.0, spread_ask))
    for i in range(35):  # alternating sides, tiny size -> flat cvd, flat price
        eng.on_event(tick(T0 + i, 100.0, 0.05, BUY if i % 2 == 0 else SELL))
    prime_baselines(st)
    prime_roc30(st)  # sigma ~1e-4 before the burst sample joins the window
    for j in range(4):  # burst: +1% on 8.0 of taker-buy volume inside one second
        eng.on_event(tick(T0 + 35.2 + 0.2 * j, 101.0, 2.0, BUY))
    eng.on_event(flush(T0 + 36.0))  # closes the burst bar -> rules run
    return eng


def test_momentum_ignition_fires() -> None:
    eng = _momentum_tape(spread_ask=100.02)  # ~2 bps spread
    evs = fired(eng, "momentum_ignition")
    assert len(evs) == 1
    ev = evs[0]
    assert ev["side"] == "long"  # ROC sign = +
    assert 0.0 < ev["strength"] <= 1.0
    # solo rule: score = 30 * strength <= 30 < watch(60) -> INFO tier, still logged
    assert ev["tier"] == "INFO"
    assert ev["score"] <= 30.0
    assert_base_snapshot(ev["snapshot"])
    assert ev["snapshot"]["price"] == pytest.approx(101.0)
    # invalidation = session VWAP (below the burst price)
    assert 0.0 < ev["snapshot"]["invalidation"] < 101.0


def test_momentum_ignition_wide_spread_blocked() -> None:
    # identical tape but ~50 bps spread > max_spread_bps (8) -> gate blocks the fire
    eng = _momentum_tape(spread_ask=100.5)
    assert fired(eng, "momentum_ignition") == []


# =====================================================================
# R2 sweep_reclaim
# =====================================================================


def _sweep_tape(reclaim_side: str, reclaim_at: float = 42.3) -> Engine:
    """Prior-low sweep on a sell print, then a reclaim print back above the level."""
    from tapescreen.core.state import Bar

    eng = Engine(fresh_cfg())
    st = eng.states[SYM]
    # seed ATR(1m) ~= 1.0 through the official 1m warm-up path (15 bars, range 1)
    seed = [Bar(T0 - 900.0 + 60.0 * i, 100.0, 100.5, 99.5, 100.0, 10.0, 5.0, 10, 1000.0, 100000.0)
            for i in range(15)]
    st.seed_bars_1m(seed)
    for i in range(40):  # quiet tape at 100: prior 15m low = 100, agg imbalance = 0.5
        eng.on_event(tick(T0 + i, 100.0, 0.5, BUY if i % 2 == 0 else SELL))
    prime_baselines(st)
    # sweep: 98.5 <= prior_low(100) - 0.25*ATR on a large SELL -> imbalance < 0.5
    eng.on_event(tick(T0 + 40.3, 98.5, 5.0, SELL))
    eng.on_event(flush(T0 + 41.0))  # sweep bar closes; rule arms LONG
    # reclaim print back above the swept level; BUY flips 60s imbalance through 0.5
    eng.on_event(tick(T0 + reclaim_at, 100.4, 8.0, reclaim_side))
    eng.on_event(flush(T0 + reclaim_at + 0.7))
    return eng


def test_sweep_reclaim_fires() -> None:
    eng = _sweep_tape(reclaim_side=BUY)
    evs = fired(eng, "sweep_reclaim")
    assert len(evs) == 1
    ev = evs[0]
    assert ev["side"] == "long"
    assert 0.0 < ev["strength"] <= 1.0
    assert_base_snapshot(ev["snapshot"])
    # invalidation = worst print during the sweep
    assert ev["snapshot"]["invalidation"] == pytest.approx(98.5)


def test_sweep_reclaim_without_imbalance_flip_blocked() -> None:
    # price reclaims the level but the reclaim is SELL flow: imbalance stays < 0.5
    eng = _sweep_tape(reclaim_side=SELL)
    assert fired(eng, "sweep_reclaim") == []


def test_sweep_reclaim_expires_after_window() -> None:
    # reclaim print lands 50s after the sweep > reclaim_window_s (45) -> expired
    eng = _sweep_tape(reclaim_side=BUY, reclaim_at=91.0)
    assert fired(eng, "sweep_reclaim") == []


# =====================================================================
# R3 vwap_fade
# =====================================================================


def _fade_tape(flow_side: str) -> Engine:
    """VWAP 100/sd ~0.9 tape, a volume spike, then a stretch to 103 (~3 sigma)."""
    eng = Engine(fresh_cfg())
    st = eng.states[SYM]
    for i in range(30):  # 99/101 alternation: session vwap = 100, sd = 1
        eng.on_event(tick(T0 + i, 99.0 if i % 2 == 0 else 101.0, 1.0,
                          BUY if i % 2 == 0 else SELL))
    prime_baselines(st)
    # volume spike establishes the 5-min vol_z peak; its side sets cvd_slope sign
    eng.on_event(tick(T0 + 30.5, 100.0, 6.0, flow_side))
    eng.on_event(tick(T0 + 31.0, 100.0, 0.01, flow_side))
    for i in (32, 33, 34):  # stretched ~3 sigma above vwap on fading volume
        eng.on_event(tick(T0 + float(i), 103.0, 0.02, flow_side))
    eng.on_event(flush(T0 + 35.0))
    return eng


def test_vwap_fade_fires_short() -> None:
    # SELL flow: cvd_slope_1m < 0 = prev slope -> decelerating against the up-stretch
    eng = _fade_tape(flow_side=SELL)
    evs = fired(eng, "vwap_fade")
    assert len(evs) == 1  # later qualifying closes are inside the 90s rule cooldown
    ev = evs[0]
    assert ev["side"] == "short"  # fade the upside extension
    assert 0.0 < ev["strength"] <= 1.0
    assert ev["snapshot"]["counter_trend"] is True
    assert_base_snapshot(ev["snapshot"])
    assert ev["snapshot"]["invalidation"] == pytest.approx(103.0)  # extension extreme


def test_vwap_fade_accelerating_cvd_blocked() -> None:
    # BUY flow: cvd_slope_1m > 0 >= prev -> still accelerating with the move -> no fade
    eng = _fade_tape(flow_side=BUY)
    assert fired(eng, "vwap_fade") == []


# =====================================================================
# R4 squeeze_release
# =====================================================================


def _squeeze_tape(expansion_minute: int) -> Engine:
    """10 min of wide alternation, then converging closes (BB width pctl -> low),
    then one wide (range 6 >> 2*ATR14) up-bar on primed-baseline burst volume."""
    eng = Engine(fresh_cfg())
    st = eng.states[SYM]
    for m in range(10):  # wide phase: 1m closes alternate 99/101
        eng.on_event(tick(T0M + 60.0 * m, 101.0 if m % 2 else 99.0, 0.5, BUY))
    for m in range(10, expansion_minute):  # converging closes: width strictly shrinks
        eng.on_event(tick(T0M + 60.0 * m, 100.0 + 0.85 ** (m - 10), 0.5, BUY))
    t_x = T0M + 60.0 * expansion_minute
    eng.on_event(tick(t_x, 100.0, 0.5, BUY))  # expansion bar opens at 100
    prime_baselines(st)
    eng.on_event(tick(t_x + 55.0, 106.0, 6.0, BUY))  # burst: range 6, loud volume
    eng.on_event(flush(t_x + 61.5))  # closes the expansion 1m bar -> rules run
    return eng


def test_squeeze_release_fires_long() -> None:
    eng = _squeeze_tape(expansion_minute=35)
    evs = fired(eng, "squeeze_release")
    assert len(evs) == 1
    ev = evs[0]
    assert ev["side"] == "long"  # up expansion bar
    assert 0.0 < ev["strength"] <= 1.0
    assert ev["snapshot"]["squeeze_run_min"] >= 10  # run as of before the expansion bar
    assert_base_snapshot(ev["snapshot"])
    # invalidation = expansion bar base: price - range = 106 - 6
    assert ev["snapshot"]["invalidation"] == pytest.approx(100.0)


def test_squeeze_release_without_prior_run_blocked() -> None:
    # same expansion bar (wide + loud) but only ~2 squeeze minutes accumulated
    eng = _squeeze_tape(expansion_minute=22)
    assert fired(eng, "squeeze_release") == []


# =====================================================================
# R5 book_imbalance
# =====================================================================


def _imbalance_tape(spike_high: bool) -> Engine:
    """Top-10 imbalance pinned at 0.72-0.8 for 21s while price sits at the 15m high
    (or 200 bps below it when spike_high seeds an earlier 102 print)."""
    eng = Engine(fresh_cfg())
    st = eng.states[SYM]
    if spike_high:
        eng.on_event(tick(T0 + 0.2, 102.0, 0.1, BUY))  # prior 15m high -> 102
    for i in range(50):
        eng.on_event(tick(T0 + i, 100.0, 0.5, BUY))
    eng.on_event(book(T0 + 30.0, 0.72))  # >= imbalance_high 0.65: clock starts
    eng.on_event(book(T0 + 40.0, 0.70))
    eng.on_event(book(T0 + 50.2, 0.80))
    prime_baselines(st)
    eng.on_event(flush(T0 + 51.0))  # imb_high_dur = 21s >= sustain_s (20)
    return eng


def test_book_imbalance_fires_long() -> None:
    eng = _imbalance_tape(spike_high=False)
    evs = fired(eng, "book_imbalance")
    assert len(evs) == 1
    ev = evs[0]
    assert ev["side"] == "long"
    assert 0.0 < ev["strength"] <= 1.0
    assert ev["snapshot"]["imb_duration_s"] >= 20.0
    assert_base_snapshot(ev["snapshot"])
    assert ev["snapshot"]["book_imbalance"] == pytest.approx(0.80)


def test_book_imbalance_far_from_range_high_blocked() -> None:
    # same sustained imbalance, but price is 200 bps below the 15m high (> 10 bps)
    eng = _imbalance_tape(spike_high=True)
    assert fired(eng, "book_imbalance") == []


# =====================================================================
# R6 oi_compression (context flag)
# =====================================================================


def _oi_tape(price_jump: bool) -> Engine:
    """Six 1m closes build |dOI 5m| session samples on a slow OI drift, then a
    +500 OI jump with price flat (flag) or +1.5% (roc gate blocks)."""
    eng = Engine(fresh_cfg())
    st = eng.states[SYM]
    for m in range(6):
        base = T0M + 60.0 * m
        for j in range(12):
            eng.on_event(tick(base + 5.0 * j, 100.0, 0.5, BUY))
        eng.on_event(perp(base + 2.0, 0.0, 1000.0 + 0.01 * (base + 2.0 - T0M)))
        eng.on_event(perp(base + 32.0, 0.0, 1000.0 + 0.01 * (base + 32.0 - T0M)))
    eng.on_event(flush(T0M + 361.0))  # closes minute 5 -> 6th doi5 sample
    eng.on_event(perp(T0M + 366.0, 0.0, 2000.0))  # dOI(5m) ~ +500 >> session p95
    if price_jump:
        eng.on_event(tick(T0M + 366.5, 101.5, 0.5, BUY))  # |ROC5m| = 1.5% >> 0.3*sigma
    # prime only now: while sigma was 0 every earlier close was guarded, so the flag
    # can only come from the post-jump closes under the |ROC5m| <= 0.3*sigma gate
    prime_roc5m(st)  # sigma ~1e-3 > 0 makes the roc gate decidable
    eng.on_event(tick(T0M + 367.0, 101.5 if price_jump else 100.0, 0.5, BUY))
    eng.on_event(flush(T0M + 368.0))
    return eng


def test_oi_compression_sets_flag_not_event() -> None:
    eng = _oi_tape(price_jump=False)
    assert eng.flags[SYM]["oi_compression"] is True
    # context rules never produce loggable events
    assert fired(eng, "oi_compression") == []


def test_oi_compression_roc_gate_blocks_flag() -> None:
    eng = _oi_tape(price_jump=True)
    assert eng.flags[SYM]["oi_compression"] is False


# =====================================================================
# R7 funding_extremity (context flag)
# =====================================================================


def _funding_engine(rate: float) -> Engine:
    eng = Engine(fresh_cfg())
    st = eng.states[SYM]
    # 7-day-ish history: 100 one-minute samples at |rate| = 1e-4 -> p95 = 1e-4
    st.seed_funding([(T0 - 60.0 * (100 - i), 1e-4) for i in range(100)])
    eng.on_event(perp(T0 + 0.5, rate, 1000.0))
    eng.on_event(tick(T0 + 0.6, 100.0, 1.0, BUY))
    eng.on_event(flush(T0 + 1.5))
    return eng


@pytest.mark.parametrize(("rate", "bias"), [(8e-4, "short"), (-8e-4, "long")])
def test_funding_extremity_sets_bias_flag(rate: float, bias: str) -> None:
    eng = _funding_engine(rate)
    assert eng.flags[SYM]["funding_bias"] == bias  # crowded side gets faded
    assert fired(eng, "funding_extremity") == []  # context: flag only, no event
    assert eng.signals_total == 0


def test_funding_below_p95_no_bias() -> None:
    eng = _funding_engine(5e-5)  # |funding| < trailing p95 (1e-4)
    assert eng.flags[SYM]["funding_bias"] == ""


# =====================================================================
# Composite: decay / multipliers / cap (direct _rescore math)
# =====================================================================


def test_composite_decay_linear_over_cooldown() -> None:
    comp = Composite(fresh_cfg(), SYM)
    ts = T0
    comp.active["momentum_ignition"] = Fired(ts, 1.0, 30.0, "long")
    comp._rescore(ts)
    assert comp.scores == {"long": 30.0, "short": 0.0}  # decay 1.0 at fire time
    comp._rescore(ts + 45.0)  # halfway through rule_cooldown_s (90)
    assert comp.scores["long"] == pytest.approx(15.0)
    assert comp.scores["short"] == 0.0
    comp._rescore(ts + 90.0)  # fully decayed: contribution removed
    assert comp.scores["long"] == 0.0
    assert "momentum_ignition" not in comp.active


def test_composite_context_multipliers() -> None:
    comp = Composite(fresh_cfg(), SYM)
    ts = T0
    comp.active["momentum_ignition"] = Fired(ts, 1.0, 30.0, "long")
    comp.active["vwap_fade"] = Fired(ts, 1.0, 20.0, "short")
    comp.ctx_active["oi_compression"] = (ts, "")
    comp._rescore(ts)  # oi_compression multiplies BOTH sides by 1.10
    assert comp.scores["long"] == pytest.approx(33.0)
    assert comp.scores["short"] == pytest.approx(22.0)
    comp.ctx_active["funding_extremity"] = (ts, "long")
    comp._rescore(ts)  # aligned x1.10, against x0.90, stacked on the OI mult
    assert comp.scores["long"] == pytest.approx(30.0 * 1.10 * 1.10)
    assert comp.scores["short"] == pytest.approx(20.0 * 1.10 * 0.90)


def test_composite_score_capped_at_100() -> None:
    comp = Composite(fresh_cfg(), SYM)
    comp.active["momentum_ignition"] = Fired(T0, 1.0, 500.0, "long")
    comp._rescore(T0)
    assert comp.scores["long"] == 100.0


# =====================================================================
# Cooldowns and tiers (Composite.evaluate with hand-built snapshots)
# =====================================================================


def momentum_snap(ts: float) -> FeatureSnapshot:
    """Snapshot satisfying only momentum_ignition (5x sigma ROC30, loud, tight)."""
    return FeatureSnapshot(ts=ts, symbol=SYM, price=100.0, warming=False,
                           roc_30s=0.01, roc30_sigma=0.002, vol_z=5.0,
                           cvd_slope_1m=1.0, spread_bps=1.0)


def vwap_snap(ts: float) -> FeatureSnapshot:
    """Snapshot satisfying only vwap_fade, LONG side (stretched 3 sigma below)."""
    return FeatureSnapshot(ts=ts, symbol=SYM, price=95.0, warming=False,
                           vwap_session_sd=1.0, vwap_dist_sigma=-3.0,
                           cvd_slope_1m=1.0, cvd_slope_1m_prev=0.0,
                           vol_z=1.0, vol_z_5m_peak=5.0)


def book_snap(ts: float) -> FeatureSnapshot:
    """Snapshot satisfying only book_imbalance, LONG side (pinned 25s at the high)."""
    return FeatureSnapshot(ts=ts, symbol=SYM, price=100.0, warming=False,
                           book_imbalance=0.8, imb_high_dur_s=25.0,
                           prior_15m_high=100.0, prior_15m_low=99.0)


def test_rule_cooldown_90s() -> None:
    comp = Composite(fresh_cfg(), SYM)
    evs, _ = comp.evaluate(momentum_snap(T0))
    assert [e.rule for e in evs] == ["momentum_ignition"]
    assert_base_snapshot(evs[0].snapshot)
    # second qualifying trigger 45s later: inside the per-(symbol,rule) 90s cooldown
    evs, _ = comp.evaluate(momentum_snap(T0 + 45.0))
    assert evs == []
    # after the cooldown has elapsed it must fire again
    evs, _ = comp.evaluate(momentum_snap(T0 + 91.0))
    assert [e.rule for e in evs] == ["momentum_ignition"]


def test_tier_info_below_watch() -> None:
    comp = Composite(fresh_cfg(), SYM)  # defaults: watch 60 / alert 80
    evs, _ = comp.evaluate(momentum_snap(T0))
    assert evs[0].score == pytest.approx(30.0)  # weight 30 * strength 1.0
    assert evs[0].tier == "INFO"  # logged even below WATCH confluence


def test_tier_watch_between_thresholds() -> None:
    cfg = fresh_cfg()
    cfg.composite.watch_score = 10.0
    cfg.composite.alert_score = 1000.0
    comp = Composite(cfg, SYM)
    evs, _ = comp.evaluate(momentum_snap(T0))
    assert evs[0].tier == "WATCH"


def test_alert_cooldown_demotes_second_alert_to_watch() -> None:
    cfg = fresh_cfg()  # lowered tiers so a solo rule reaches ALERT
    cfg.composite.watch_score = 10.0
    cfg.composite.alert_score = 20.0
    comp = Composite(cfg, SYM)

    evs, _ = comp.evaluate(momentum_snap(T0))
    assert evs[0].tier == "ALERT"  # score 30 >= 20

    # different rule, same side, 30s later: score above alert_score again but the
    # per-(symbol,side) 60s alert cooldown demotes it to WATCH
    evs, _ = comp.evaluate(vwap_snap(T0 + 30.0))
    assert [e.rule for e in evs] == ["vwap_fade"]
    assert evs[0].side == "long"
    assert evs[0].score >= 20.0
    assert evs[0].tier == "WATCH"

    # 70s after the first ALERT (> 60s cooldown): the next long fire alerts again
    evs, _ = comp.evaluate(book_snap(T0 + 70.0))
    assert [e.rule for e in evs] == ["book_imbalance"]
    assert evs[0].score >= 20.0
    assert evs[0].tier == "ALERT"


# =====================================================================
# Every logged event carries the full base snapshot
# =====================================================================


def test_all_engine_events_have_full_base_snapshot() -> None:
    engines = [
        _momentum_tape(spread_ask=100.02),
        _sweep_tape(reclaim_side=BUY),
        _fade_tape(flow_side=SELL),
        _imbalance_tape(spike_high=False),
    ]
    rows = [row for eng in engines for row in eng.signal_feed]
    assert rows, "expected signal events across the positive scenarios"
    for row in rows:
        assert_base_snapshot(row["snapshot"])
        assert row["tier"] in ("INFO", "WATCH", "ALERT")
        assert row["side"] in ("long", "short")
        assert 0.0 < row["strength"] <= 1.0
