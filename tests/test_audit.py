"""Auditor + selftest tests: clean pass, defect detection, quarantine hysteresis."""

from __future__ import annotations

import math

from tapescreen.config import load_config
from tapescreen.core.audit import Auditor
from tapescreen.core.engine import Engine
from tapescreen.core.events import Bbo, Mids, Tick
from tapescreen.selftest import run_selftest
from tests.conftest import REPO

T0 = 1_750_000_000.0


def fresh_cfg():
    return load_config(REPO / "config.yaml")


def tick(ts: float, px: float, sym: str = "BTC") -> Tick:
    return Tick(ts, ts, ts, sym, px, 1.0, "buy", int(ts * 10) % 10**9, "hyperliquid")


def bbo(ts: float, bid: float, ask: float, sym: str = "BTC") -> Bbo:
    return Bbo(ts, ts, ts, sym, bid, 1.0, ask, 1.0)


def test_selftest_all_green() -> None:
    assert run_selftest() == []


def test_clean_engine_passes_audit() -> None:
    cfg = fresh_cfg()
    engine = Engine(cfg)
    engine.on_event(tick(T0, 100.0))
    engine.on_event(bbo(T0 + 0.1, 99.9, 100.1))
    engine.on_event(Mids(T0 + 0.2, {"BTC": 100.0}))
    engine.on_event(tick(T0 + 1.0, 100.05))
    auditor = Auditor(cfg, engine)
    report = auditor.run(now=T0 + 1.1)
    assert report.ok, report.failures
    assert report.checks_run > 20
    assert report.quarantined == []


def test_price_divergence_quarantines_and_recovers() -> None:
    cfg = fresh_cfg()  # divergence 1%, clear after 3 clean audits
    engine = Engine(cfg)
    engine.on_event(tick(T0, 100.0))
    engine.on_event(Mids(T0 + 0.1, {"BTC": 105.0}))  # venue says 105: 5% divergence
    auditor = Auditor(cfg, engine)
    r1 = auditor.run(now=T0 + 1.0)
    assert not r1.ok and any("diverges" in f for f in r1.failures)
    assert "BTC" in engine.quarantined

    # quarantined symbols do not open trade signals
    from tapescreen.core.signals.base import SignalEvent
    ev = SignalEvent(T0 + 2, "BTC", "long", "momentum_ignition", 0.8, tier="INFO",
                     snapshot={"price": 100.0, "invalidation": 99.0})
    engine._record_signal(ev)
    assert engine.ledger.active() == []

    # data heals: divergence gone -> quarantine lifts after 3 clean audits
    engine.on_event(Mids(T0 + 3.0, {"BTC": 100.0}))
    engine.on_event(tick(T0 + 3.1, 100.0))
    for i in range(2):
        assert "BTC" in engine.quarantined
        auditor.run(now=T0 + 4.0 + i)
    auditor.run(now=T0 + 7.0)
    assert "BTC" not in engine.quarantined


def test_crossed_book_and_nonfinite_feature_detected() -> None:
    cfg = fresh_cfg()
    engine = Engine(cfg)
    engine.on_event(tick(T0, 100.0))
    engine.on_event(bbo(T0 + 0.1, 100.2, 100.1))  # crossed: bid > ask
    auditor = Auditor(cfg, engine)
    r = auditor.run(now=T0 + 1.0)
    assert any("crossed book" in f for f in r.failures)

    engine2 = Engine(cfg)
    engine2.on_event(tick(T0, 100.0))
    engine2.features["BTC"].snapshot.vol_z = math.nan  # corrupt a derived value
    r2 = Auditor(cfg, engine2).run(now=T0 + 1.0)
    assert any("non-finite feature" in f for f in r2.failures)


def test_ledger_invariant_violation_detected() -> None:
    cfg = fresh_cfg()
    engine = Engine(cfg)
    from tapescreen.core.features import FeatureSnapshot
    from tapescreen.core.signals.base import SignalEvent
    snap = FeatureSnapshot(symbol="BTC")
    snap.atr_1m = 1.0
    snap.price = 100.0
    ev = SignalEvent(T0, "BTC", "long", "momentum_ignition", 0.8, tier="INFO",
                     snapshot={"price": 100.0, "invalidation": 98.0})
    t = engine.ledger.on_rule_fire(ev, snap)
    assert t is not None
    auditor = Auditor(cfg, engine)
    assert auditor._check_ledger([]) >= 1  # healthy trade passes
    t.stop = 101.0  # corrupt: INIT-state long with stop above entry
    fails: list[str] = []
    auditor._check_ledger(fails)
    assert fails and "invariant broken" in fails[0]


def test_model_weight_corruption_detected() -> None:
    cfg = fresh_cfg()
    engine = Engine(cfg)
    m = engine.ledger._model("momentum_ignition")
    m.w[0] = math.inf
    fails: list[str] = []
    Auditor(cfg, engine)._check_learning(fails)
    assert fails and "non-finite weights" in fails[0]
