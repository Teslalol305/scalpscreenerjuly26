"""ResearchDesk tests: candidate math, promotion/retirement, probation,
exploration quota, loop-breaker, and model-width safety across schema changes."""

from __future__ import annotations

import json
import math

import pytest

from tapescreen.config import load_config
from tapescreen.core.features import FeatureSnapshot
from tapescreen.core.research import (
    CANDIDATES,
    ResearchDesk,
    compute_candidates,
    point_biserial,
)
from tapescreen.core.signals.base import SignalEvent
from tapescreen.core.signals.ledger import Bucket, SignalLedger
from tapescreen.core.signals.models import FEATURES, OnlineLogistic
from tapescreen.core.thoughts import CAT_DESK, CAT_TRADE, ThoughtLog
from tapescreen.store.db import Db
from tests.conftest import REPO

T0 = 1_750_000_000.0


def fresh_cfg():
    return load_config(REPO / "config.yaml")


def fire(side: str = "long", rule: str = "momentum_ignition",
         ts: float = T0) -> SignalEvent:
    return SignalEvent(ts, "BTC", side, rule, 0.8, tier="INFO", score=30.0,
                       snapshot={"price": 100.0, "invalidation": 98.0})


def snap(**kw) -> FeatureSnapshot:
    s = FeatureSnapshot(symbol="BTC")
    s.atr_1m = 1.0
    s.price = 100.0
    for k, v in kw.items():
        setattr(s, k, v)
    return s


# ------------------------------------------------------------- candidate math

def test_point_biserial_hand_computed() -> None:
    # x=[1,2,3,4], wins=[F,F,T,T]: (3.5-1.5)/sqrt(1.25) * 0.5 = 1/sqrt(1.25)
    r = point_biserial([1, 2, 3, 4], [False, False, True, True])
    assert r == pytest.approx(1.0 / math.sqrt(1.25))
    assert point_biserial([2, 2, 2, 2], [True, False, True, False]) == 0.0
    assert point_biserial([1, 2], [True, True]) == 0.0  # one class -> undefined


def test_candidates_bounded_and_side_signed() -> None:
    s = snap(roc_15m=0.02, basis_bps=3.0, funding_pctl_7d=90.0,
             bb_width_pctl=10.0, imb_high_dur_s=120.0, vol_z=1.0, vol_z_5m_peak=2.0)
    c_long = compute_candidates(fire("long"), s, streak=0.5)
    c_short = compute_candidates(fire("short"), s, streak=0.5)
    assert set(c_long) == set(CANDIDATES)
    assert all(math.isfinite(v) for v in c_long.values())
    # directional candidates flip with side; regime candidates do not
    assert c_long["roc15_edge"] == pytest.approx(-c_short["roc15_edge"])
    assert c_long["basis_edge"] == pytest.approx(-c_short["basis_edge"])
    assert c_long["funding_pctl"] == c_short["funding_pctl"] == pytest.approx(0.8)
    assert c_long["bb_squeeze"] == pytest.approx(0.8)
    assert c_long["imb_dur"] == pytest.approx(2.0)
    assert c_long["vol_freshness"] == pytest.approx(0.5)
    # hour encoding is on the unit circle
    assert c_long["hour_sin"] ** 2 + c_long["hour_cos"] ** 2 == pytest.approx(1.0)


def test_model_width_safety() -> None:
    m = OnlineLogistic(0.05, 0.001, names=[*FEATURES, "imb_dur"])
    assert m.k == 13
    m.update([1.0] * 12, True)   # old-era vector: padded, no crash
    m.update([1.0] * 13, False)
    assert 0.0 < m.predict([0.5] * 12) < 1.0
    assert 0.0 < m.predict([0.5] * 14) < 1.0  # over-wide: truncated


# --------------------------------------------------------- promotion pipeline

def seeded_db(tmp_path, n: int = 60):
    """DB with n resolved trades where 'imb_dur' strongly separates wins."""
    db = Db(tmp_path / "r.db")
    for i in range(n):
        won = i % 2 == 0
        cand = dict.fromkeys(CANDIDATES, 0.0)
        cand["imb_dur"] = 2.0 if won else 0.2  # the signal to be discovered
        cand["dow"] = (i % 7) / 6.0            # noise
        db.insert_trade_signal(
            i + 1, 0, T0 + i, "BTC", "long", "momentum_ignition", "INFO", 0.5,
            100.0, 98.0, 0.0, entries_json="[]",
            features_json=json.dumps([0.1] * 12),
            candidates_json=json.dumps(cand),
        )
        db.close_trade_signal(i + 1, "win" if won else "loss", T0 + i + 60,
                              101.0 if won else 98.0, "TRAIL" if won else "STOP",
                              1.5 if won else -1.0, 1.5 if won else -1.0, "INIT")
    db.close()
    return Db(tmp_path / "r.db")


def test_scout_promotes_predictive_variable(tmp_path) -> None:
    cfg = fresh_cfg()
    db = seeded_db(tmp_path)
    th = ThoughtLog(None)
    led = SignalLedger(cfg, db, thoughts=th)
    desk = ResearchDesk(cfg, led, th, db)
    desk.meeting(T0 + 10_000)
    assert desk.active_extras == ["imb_dur"]
    assert led.extra_names == ["imb_dur"]
    m = led.models["momentum_ignition"]
    assert m.k == 13 and m.names[-1] == "imb_dur" and m.n == 60
    promoted = [t for t in th.recent if "PROMOTED" in t["headline"]]
    assert len(promoted) == 1 and "imb_dur" in promoted[0]["headline"]
    assert any("correlation" in d for d in promoted[0]["detail"])
    # persisted: a fresh desk restores the promotion and rebuilds
    led2 = SignalLedger(cfg, db, thoughts=ThoughtLog(None))
    desk2 = ResearchDesk(cfg, led2, ThoughtLog(None), db)
    assert desk2.active_extras == ["imb_dur"] and led2.extra_names == ["imb_dur"]
    db.close()


def test_scout_promotes_at_most_one_per_meeting(tmp_path) -> None:
    cfg = fresh_cfg()
    db = Db(tmp_path / "r2.db")
    for i in range(60):
        won = i % 2 == 0
        cand = dict.fromkeys(CANDIDATES, 0.0)
        cand["imb_dur"] = 2.0 if won else 0.2
        cand["bb_squeeze"] = 0.9 if won else -0.9  # a second strong variable
        db.insert_trade_signal(i + 1, 0, T0 + i, "BTC", "long", "vwap_fade",
                               "INFO", 0.5, 100.0, 98.0, 0.0, "[]",
                               json.dumps([0.1] * 12), json.dumps(cand))
        db.close_trade_signal(i + 1, "win" if won else "loss", T0 + i + 60,
                              101.0, "TRAIL", 1.0, 1.0, "INIT")
    db.close()
    db = Db(tmp_path / "r2.db")
    led = SignalLedger(cfg, db, thoughts=None)
    desk = ResearchDesk(cfg, led, ThoughtLog(None), db)
    desk.meeting(T0 + 10_000)
    assert len(desk.active_extras) == 1  # deliberate, reviewable steps
    desk._resolved_seen = 0  # simulate new outcomes arriving
    desk.meeting(T0 + 10_600)
    assert len(desk.active_extras) == 2
    db.close()


# ------------------------------------------------------ probation/exploration

def test_probation_enter_gate_explore_and_exit() -> None:
    cfg = fresh_cfg()
    th = ThoughtLog(None)
    led = SignalLedger(cfg, None, thoughts=th)
    desk = ResearchDesk(cfg, led, th, None)
    # weak record: posterior (8+3)/(30+6) = 0.306 < 0.45 with n=30 >= 25
    led.rule_buckets["momentum_ignition"] = Bucket(wins=8.0, n=30.0, r_sum=-8.0, r_n=30.0)
    desk.meeting(T0)
    assert "momentum_ignition" in desk.probation
    assert any("ON PROBATION" in t["headline"] for t in th.recent)

    # high confidence passes the gate outright
    assert desk.gate("momentum_ignition", 0.60) == (True, False, "")
    # low confidence: blocked 3x, 4th opens as exploration (exploration_every=4)
    results = [desk.gate("momentum_ignition", 0.50) for _ in range(4)]
    assert [r[0] for r in results] == [False, False, False, True]
    assert results[3][1] is True and "exploration" in results[3][2]

    # record recovers -> probation lifted at the next meeting with new outcomes
    led.rule_buckets["momentum_ignition"] = Bucket(wins=20.0, n=36.0, r_sum=5.0, r_n=36.0)
    desk.meeting(T0 + 600)
    assert "momentum_ignition" not in desk.probation
    assert any("probation LIFTED" in t["headline"] for t in th.recent)


def test_ledger_respects_desk_gate() -> None:
    cfg = fresh_cfg()
    th = ThoughtLog(None)
    led = SignalLedger(cfg, None, thoughts=th)
    desk = ResearchDesk(cfg, led, th, None)
    desk.probation["momentum_ignition"] = {"since": T0, "conf_min": 0.55, "n_at_entry": 0}
    s = snap()
    opened = [led.on_rule_fire(fire(ts=T0 + i), s) for i in range(4)]
    assert opened[:3] == [None, None, None]
    assert opened[3] is not None and opened[3].exploration is True
    held = [t for t in th.recent if t["cat"] == CAT_TRADE and "holds it back" in t["headline"]]
    assert len(held) == 3 and "probation gate" in held[0]["detail"][0]
    entry = next(t for t in th.recent if t["headline"].startswith("opening"))
    assert "exploration" in entry["headline"]


def test_loop_breaker_boosts_starved_probation() -> None:
    cfg = fresh_cfg()
    led = SignalLedger(cfg, None, thoughts=None)
    th = ThoughtLog(None)
    desk = ResearchDesk(cfg, led, th, None)
    led.rule_buckets["momentum_ignition"] = Bucket(wins=8.0, n=30.0, r_sum=-8.0, r_n=30.0)
    led.rule_buckets["vwap_fade"] = Bucket(wins=10.0, n=15.0, r_sum=2.0, r_n=15.0)
    desk.meeting(T0)
    assert "momentum_ignition" in desk.probation
    # other rules keep resolving, the gated rule stays starved -> after
    # stagnant_meetings the loop-breaker raises its exploration rate
    for i in range(cfg.research.stagnant_meetings + 1):
        led.rule_buckets["vwap_fade"].n += 1
        desk.meeting(T0 + 60 * (i + 1))
    assert desk.exploration_every.get("momentum_ignition") == 2
    assert any("loop-breaker" in t["headline"] for t in th.recent)


def test_meeting_is_quiet_without_new_outcomes() -> None:
    cfg = fresh_cfg()
    led = SignalLedger(cfg, None, thoughts=None)
    th = ThoughtLog(None)
    desk = ResearchDesk(cfg, led, th, None)
    led.rule_buckets["vwap_fade"] = Bucket(wins=30.0, n=54.0, r_sum=10.0, r_n=54.0)
    desk.meeting(T0)
    n_thoughts = len(th.recent)
    for i in range(5):  # nothing new resolved: no narration, no churn
        desk.meeting(T0 + 60 * (i + 1))
    assert len(th.recent) == n_thoughts
    assert any(t["cat"] == CAT_DESK for t in th.recent)


def test_candidates_recorded_on_open_trades() -> None:
    cfg = fresh_cfg()
    led = SignalLedger(cfg, None, thoughts=None)
    t = led.on_rule_fire(fire(), snap(imb_high_dur_s=90.0))
    assert set(t.candidates) == set(CANDIDATES)
    assert t.candidates["imb_dur"] == pytest.approx(1.5)
    assert len(t.features) == 12  # no promotions yet -> base width
