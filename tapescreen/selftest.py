"""Boot-time logic self-test: re-verifies core math against known constants.

Use: errors = run_selftest(); called from main before the feed starts (and via
`python -m tapescreen --selftest`). Depends on: core.* only - no network, no
disk, deterministic, <100ms. A broken install or bad edit fails loudly here
instead of running the screener on silently wrong math.
"""

from __future__ import annotations

import math

from tapescreen.core.events import BUY, SELL, Bbo, Tick
from tapescreen.core.features import BarWindow, Wilder
from tapescreen.core.normalize import Normalizer
from tapescreen.core.signals.base import SignalEvent
from tapescreen.core.signals.models import K, OnlineLogistic
from tapescreen.core.state import Bar, RollingStat, percentile_rank

T0 = 1_700_000_000.0


def _approx(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def run_selftest() -> list[str]:
    """Returns a list of failure descriptions; empty means every check passed."""
    errs: list[str] = []

    def check(name: str, ok: bool) -> None:
        if not ok:
            errs.append(name)

    # RollingStat: mean/population-sd of [1,2,3] -> 2, sqrt(2/3); z(4) = 2/sd
    rs = RollingStat(10)
    for v in (1.0, 2.0, 3.0):
        rs.add(v)
    check("rollingstat.mean", _approx(rs.mean, 2.0))
    check("rollingstat.std", _approx(rs.std, math.sqrt(2.0 / 3.0)))
    check("rollingstat.z", _approx(rs.zscore(4.0), 2.0 / math.sqrt(2.0 / 3.0)))

    # percentile_rank: share of samples <= v
    check("percentile_rank", _approx(percentile_rank([1, 2, 3, 4], 3.0), 75.0))

    # Wilder: SMA seed then smoothing - (5,3,4) seeds mean 4; then (4*2+6)/3
    w = Wilder(3)
    for v in (5.0, 3.0, 4.0):
        w.add(v)
    check("wilder.seed", _approx(w.value, 4.0))
    w.add(6.0)
    check("wilder.smooth", _approx(w.value, 14.0 / 3.0))

    # BarWindow VWAP + sd: two bars, price 100 x2 + 102 x1 -> vwap 100.6667
    bw = BarWindow(10)
    bw.add(Bar(T0, 100, 100, 100, 100, 2.0, 1.0, 2, 200.0, 20000.0))
    bw.add(Bar(T0 + 1, 102, 102, 102, 102, 1.0, 1.0, 1, 102.0, 10404.0))
    vwap, sd = bw.vwap_sd()
    check("barwindow.vwap", _approx(vwap, 302.0 / 3.0))
    check("barwindow.sd", _approx(sd, math.sqrt(30404.0 / 3.0 - (302.0 / 3.0) ** 2), 1e-6))
    check("barwindow.cvd", _approx(bw.cvd, 2.0 * 2.0 - 3.0))

    # Normalizer: spread/imbalance math on a hand-built book
    n = Normalizer({"BTC": "BTC"})
    msg = {"channel": "l2Book", "data": {"coin": "BTC", "time": 0, "levels": [
        [{"px": "2000.0", "sz": "3.0", "n": 1}], [{"px": "2001.0", "sz": "1.0", "n": 1}]]}}
    (book,) = n.normalize(msg, 0.0, 0.0)
    check("normalize.spread", _approx(book.spread_bps, 1.0 / 2000.5 * 1e4))
    check("normalize.imbalance", _approx(book.imbalance, 0.75))

    # Ledger: ladder construction + TP1/BE + trail exit R accounting, on a FIXED
    # synthetic config so the constants below hold regardless of the user's yaml
    from types import SimpleNamespace

    from tapescreen.config import LearningConfig
    from tapescreen.core.features import FeatureSnapshot
    from tapescreen.core.signals.ledger import SignalLedger

    cfg = SimpleNamespace(
        learning=LearningConfig(
            enabled=True, prior_wins=3.0, prior_losses=3.0, min_bucket_n=20,
            weight_mult_min=0.6, weight_mult_max=1.4, weight_min_n=10,
            stop_atr_min=1.0, stop_atr_max=4.0, one_per_side=True,
            entry_levels=3, entry_step_r=0.5, entry_window_s=900.0,
            tp1_r=1.0, tp1_fraction=1.0 / 3.0, trail_start_r=1.5, trail_dist_r=1.0,
            max_hold_s=7200.0, model_enabled=True, model_lr=0.05, model_l2=0.001,
            model_min_n=30, refit_epochs=3,
        ),
        stats=SimpleNamespace(spread_haircut_bps=2.0, taker_fee_bps=4.5),
    )
    led = SignalLedger(cfg, None)  # type: ignore[arg-type]
    snap = FeatureSnapshot(symbol="BTC")
    snap.atr_1m = 1.0
    snap.price = 100.0
    ev = SignalEvent(T0, "BTC", "long", "momentum_ignition", 0.8, tier="INFO",
                     snapshot={"price": 100.0, "invalidation": 98.0})
    t = led.on_rule_fire(ev, snap)
    check("ledger.ladder", t is not None
          and [lv["px"] for lv in t.levels] == [100.0, 99.0, 98.0]
          and _approx(t.risk_unit, 2.0))

    def tick(ts: float, px: float) -> Tick:
        return Tick(ts, ts, ts, "BTC", px, 1.0, BUY, 1, "hyperliquid")

    led.on_tick(tick(T0 + 60, 103.0))  # +1.5R: TP1 banked, BE, trail armed at 101
    check("ledger.tp1_be_trail", t.tp1_done and t.state == "TRAIL"
          and _approx(t.stop, 101.0) and _approx(t.realized, cfg.learning.tp1_fraction))
    (done,) = led.on_tick(tick(T0 + 120, 100.9))
    f = cfg.learning.tp1_fraction
    check("ledger.trail_exit_r", done.exit_reason == "TRAIL"
          and _approx(done.total_r, f * 1.0 + (1 - f) * 0.5))
    check("ledger.win", done.status == "win")

    # Online model: first update moves only bias by lr*(y - 0.5)
    m = OnlineLogistic(lr=0.5, l2=0.0)
    m.update([1.0] * K, True)
    check("model.first_update", _approx(m.b, 0.25) and all(v == 0.0 for v in m.w))
    check("model.predict_range", 0.0 < m.predict([0.5] * K) < 1.0)
    # Extended model must accept base-width vectors (open trades survive promotions)
    mx = OnlineLogistic(lr=0.5, l2=0.0, names=[*["f"] * 0, *map(str, range(K + 2))])
    check("model.width_pad", 0.0 < mx.predict([0.5] * K) < 1.0 and mx.k == K + 2)

    # Research desk: point-biserial vs hand computation.
    # x = [1,2,3,4], wins = [F,F,T,T]: m1=3.5, m0=1.5, std=sqrt(1.25), p=q=0.5
    # r = (3.5-1.5)/sqrt(1.25)*0.5 = 1/sqrt(1.25)
    from tapescreen.core.research import point_biserial
    check("research.point_biserial",
          _approx(point_biserial([1, 2, 3, 4], [False, False, True, True]),
                  1.0 / math.sqrt(1.25)))
    check("research.point_biserial_flat", point_biserial([2, 2, 2], [True, False, True]) == 0.0)

    # Tick side semantics survive normalize
    tmsg = {"channel": "trades", "data": [
        {"coin": "BTC", "side": "A", "px": "1", "sz": "1", "time": 0, "tid": 1}]}
    (tk,) = Normalizer({"BTC": "BTC"}).normalize(tmsg, 0.0, 0.0)
    check("normalize.side", tk.side == SELL)
    bmsg = {"channel": "bbo", "data": {"coin": "BTC", "time": 0, "bbo": [
        {"px": "9", "sz": "1", "n": 1}, {"px": "11", "sz": "1", "n": 1}]}}
    (bb,) = Normalizer({"BTC": "BTC"}).normalize(bmsg, 0.0, 0.0)
    check("normalize.bbo", isinstance(bb, Bbo) and bb.bid_px == 9.0 and bb.ask_px == 11.0)

    return errs
