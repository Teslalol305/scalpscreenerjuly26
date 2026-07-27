"""SignalLedger tests: entry/stop/target math, exits, learning posteriors, DB."""

from __future__ import annotations

import time

import pytest

from tapescreen.config import load_config
from tapescreen.core.events import Tick
from tapescreen.core.features import FeatureSnapshot
from tapescreen.core.signals.base import SignalEvent
from tapescreen.core.signals.ledger import Bucket, SignalLedger
from tapescreen.store.db import Db
from tests.conftest import REPO

T0 = 1_750_000_000.0


def fresh_cfg():
    return load_config(REPO / "config.yaml")


def snap(atr: float = 1.0, price: float = 100.0) -> FeatureSnapshot:
    s = FeatureSnapshot(symbol="BTC")
    s.atr_1m = atr
    s.price = price
    return s


def fire(side: str = "long", price: float = 100.0, inv: float = 99.5,
         rule: str = "momentum_ignition", ts: float = T0) -> SignalEvent:
    return SignalEvent(ts, "BTC", side, rule, 0.8, tier="INFO",
                       snapshot={"price": price, "invalidation": inv})


def tick(ts: float, px: float) -> Tick:
    return Tick(ts, ts, ts, "BTC", px, 1.0, "buy", int(ts * 10) % 10**9, "hyperliquid")


def test_entry_levels_from_invalidation_clamped_by_atr() -> None:
    led = SignalLedger(fresh_cfg(), None)
    # invalidation 99.5 -> dist 0.5, within [0.35, 1.5] x ATR(1.0) -> stop 99.5, target 100.5
    t = led.on_rule_fire(fire(), snap(atr=1.0))
    assert t is not None
    assert t.entry == 100.0 and t.stop == pytest.approx(99.5) and t.target == pytest.approx(100.5)
    # confidence at prior with no history: (0+3)/(0+6) = 50%
    assert t.confidence == pytest.approx(0.5)
    assert t.conf_n == 0


def test_stop_distance_clamps_and_wrong_side_fallback() -> None:
    cfg = fresh_cfg()
    led = SignalLedger(cfg, None)
    # invalidation too far (dist 5 > 1.5 x ATR 1.0) -> clamp to 1.5
    t = led.on_rule_fire(fire(price=100.0, inv=95.0), snap(atr=1.0))
    assert t.stop == pytest.approx(98.5)
    led2 = SignalLedger(cfg, None)
    # invalidation on the wrong side of a long -> fallback to 1 x ATR
    t2 = led2.on_rule_fire(fire(price=100.0, inv=101.0), snap(atr=0.8))
    assert t2.stop == pytest.approx(99.2)
    led3 = SignalLedger(cfg, None)
    # too-tight invalidation (0.1 < 0.35 x ATR 1.0) -> clamp up to 0.35
    t3 = led3.on_rule_fire(fire(price=100.0, inv=99.9), snap(atr=1.0))
    assert t3.stop == pytest.approx(99.65)


def test_one_per_side_dedup() -> None:
    led = SignalLedger(fresh_cfg(), None)
    assert led.on_rule_fire(fire(), snap()) is not None
    assert led.on_rule_fire(fire(rule="vwap_fade"), snap()) is None  # same side open
    assert led.on_rule_fire(fire(side="short", inv=100.5), snap()) is not None


def test_stop_hit_resolves_loss_minus_one_r() -> None:
    led = SignalLedger(fresh_cfg(), None)
    led.on_rule_fire(fire(), snap())  # long 100, stop 99.5
    out = led.on_tick(tick(T0 + 10, 99.4))
    assert len(out) == 1
    t = out[0]
    assert t.status == "loss" and t.exit_reason == "STOP"
    assert t.exit_price == pytest.approx(99.5)  # resolved at the stop level
    assert t.r_result == pytest.approx(-1.0)
    assert led.active() == []


def test_target_hit_resolves_win_plus_one_r() -> None:
    led = SignalLedger(fresh_cfg(), None)
    led.on_rule_fire(fire(), snap())  # long 100, target 100.5 -> +0.5% >> 2bp haircut
    (t,) = led.on_tick(tick(T0 + 10, 100.6))
    assert t.status == "win" and t.exit_reason == "TARGET"
    assert t.r_result == pytest.approx(1.0)


def test_time_exit_win_only_if_beats_haircut() -> None:
    cfg = fresh_cfg()  # haircut 2bp
    led = SignalLedger(cfg, None)
    led.on_rule_fire(fire(), snap())  # long 100, risk 0.5
    led.on_tick(tick(T0 + 100, 100.05))  # +5bp, inside stop/target -> stays open
    assert led.active()[0]["status"] == "open"
    (t,) = led.on_tick(tick(T0 + 300, 100.05))
    assert t.exit_reason == "TIME"
    assert t.status == "win"  # +5bp > 2bp haircut
    assert t.r_result == pytest.approx(0.05 / 0.5)

    led2 = SignalLedger(cfg, None)
    led2.on_rule_fire(fire(), snap())
    (t2,) = led2.on_tick(tick(T0 + 300, 100.01))  # +1bp <= 2bp haircut -> loss
    assert t2.status == "loss" and t2.exit_reason == "TIME"


def test_short_side_signs() -> None:
    led = SignalLedger(fresh_cfg(), None)
    led.on_rule_fire(fire(side="short", price=100.0, inv=100.5), snap())
    # short: stop 100.5 above, target 99.5 below
    (t,) = led.on_tick(tick(T0 + 5, 99.4))
    assert t.status == "win" and t.exit_reason == "TARGET" and t.r_result == pytest.approx(1.0)


def test_confidence_posterior_and_bucket_backoff() -> None:
    cfg = fresh_cfg()  # priors 3/3, min_bucket_n 20
    led = SignalLedger(cfg, None)
    led.rule_buckets["momentum_ignition"] = Bucket(wins=7, n=10)
    # pair bucket below min_bucket_n -> rule bucket: (7+3)/(10+6) = 0.625
    led.pair_buckets[("momentum_ignition", "BTC")] = Bucket(wins=0, n=5)
    p, n = led.confidence("momentum_ignition", "BTC")
    assert p == pytest.approx(0.625) and n == 10
    # pair bucket at min_bucket_n takes precedence: (10+3)/(20+6) = 0.5
    led.pair_buckets[("momentum_ignition", "BTC")] = Bucket(wins=10, n=20)
    p2, n2 = led.confidence("momentum_ignition", "BTC")
    assert p2 == pytest.approx(0.5) and n2 == 20


def test_weight_multiplier_gate_and_clamps() -> None:
    led = SignalLedger(fresh_cfg(), None)  # weight_min_n 10, clamp [0.6, 1.4]
    assert led.rule_multiplier("momentum_ignition") == 1.0  # no history
    led.rule_buckets["a"] = Bucket(wins=9, n=9)
    assert led.rule_multiplier("a") == 1.0  # below weight_min_n
    led.rule_buckets["b"] = Bucket(wins=9, n=10)  # P=(9+3)/16=0.75 -> 1.5 -> clamp 1.4
    assert led.rule_multiplier("b") == pytest.approx(1.4)
    led.rule_buckets["c"] = Bucket(wins=1, n=10)  # P=(1+3)/16=0.25 -> 0.5 -> clamp 0.6
    assert led.rule_multiplier("c") == pytest.approx(0.6)
    led.rule_buckets["d"] = Bucket(wins=5, n=10)  # P=0.5 -> exactly 1.0
    assert led.rule_multiplier("d") == pytest.approx(1.0)


def test_resolution_updates_buckets_and_learning_snapshot() -> None:
    led = SignalLedger(fresh_cfg(), None)
    led.on_rule_fire(fire(), snap())
    led.on_tick(tick(T0 + 10, 100.6))  # win
    b = led.rule_buckets["momentum_ignition"]
    assert (b.wins, b.n) == (1.0, 1.0)
    ls = led.learning_snapshot()
    assert ls[0]["rule"] == "momentum_ignition" and ls[0]["n"] == 1
    assert ls[0]["win_rate"] == 100.0
    assert ls[0]["confidence"] == pytest.approx(57.1, abs=0.1)  # (1+3)/(1+6)


def test_db_persistence_and_legacy_bootstrap(tmp_path) -> None:
    cfg = fresh_cfg()
    db = Db(tmp_path / "l.db")
    # legacy history: two resolved outcomes (one win vs 2bp haircut, one loss)
    sid1 = db.insert_signal(SignalEvent(T0, "BTC", "long", "vwap_fade", 0.5, tier="INFO",
                                        snapshot={"price": 100.0}))
    sid2 = db.insert_signal(SignalEvent(T0 + 1, "BTC", "short", "vwap_fade", 0.5, tier="INFO",
                                        snapshot={"price": 100.0}))
    db.upsert_outcome(sid1, {"ret_5m": 0.01, "completed_at": T0 + 300})
    db.upsert_outcome(sid2, {"ret_5m": -0.01, "completed_at": T0 + 301})

    led = SignalLedger(cfg, db)
    led.on_rule_fire(fire(rule="vwap_fade"), snap())
    led.on_tick(tick(T0 + 10, 100.6))  # win -> persisted
    deadline = time.time() + 5
    rows = []
    while time.time() < deadline:  # async writer thread
        rows = db.recent_trade_signals()
        if rows and rows[0]["status"] == "win":
            break
        time.sleep(0.05)
    assert rows[0]["status"] == "win" and rows[0]["exit_reason"] == "TARGET"
    assert rows[0]["confidence"] == pytest.approx(0.5)  # legacy 1W/1L at entry time

    # a NEW ledger bootstraps: legacy (1 win, 1 loss) + the resolved trade (1 win)
    led2 = SignalLedger(cfg, db)
    b = led2.rule_buckets["vwap_fade"]
    assert (b.wins, b.n) == (2.0, 3.0)
    db.close()
