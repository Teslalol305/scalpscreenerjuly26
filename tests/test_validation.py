"""Validation tracker tests: CI math vs hand computation, status gates, ETA,
and the fee-inclusive win test in the ledger."""

from __future__ import annotations

import math

import pytest

from tapescreen.config import load_config
from tapescreen.core.features import FeatureSnapshot
from tapescreen.core.signals.base import SignalEvent
from tapescreen.core.signals.ledger import SignalLedger
from tapescreen.core.validation import (
    ST_COLLECTING,
    ST_REJECTED,
    ST_VALIDATED,
    ST_WAITING,
    Z95,
    validation_report,
)
from tapescreen.store.db import Db
from tests.conftest import REPO

T0 = 1_750_000_000.0
DAY = 86400.0


def fresh_cfg():
    return load_config(REPO / "config.yaml")


def seed_trades(db: Db, rule: str, results: list[float], start_ts: float,
                spacing_s: float, first_id: int = 1) -> None:
    """Insert resolved trades with given R results, evenly spaced in time."""
    for i, r in enumerate(results):
        tid = first_id + i
        ts = start_ts + i * spacing_s
        db.insert_trade_signal(tid, 0, ts, "BTC", "long", rule, "INFO", 0.5,
                               100.0, 98.0, 0.0, "[]", "[]", "{}")
        db.close_trade_signal(tid, "win" if r > 0 else "loss", ts + 60.0,
                              100.0 + r, "TRAIL" if r > 0 else "STOP", r, r, "INIT")


def reopen(db: Db, tmp_path) -> Db:
    db.close()
    return Db(tmp_path / "v.db")


# ------------------------------------------------------------------- CI math

def test_report_math_hand_computed(tmp_path) -> None:
    cfg = fresh_cfg()
    db = Db(tmp_path / "v.db")
    # 4 trades: +1, +1, -1, +1 -> mean 0.5, var 0.75, se = sqrt(0.75/4)
    seed_trades(db, "vwap_fade", [1.0, 1.0, -1.0, 1.0], T0, spacing_s=2 * DAY)
    db = reopen(db, tmp_path)
    now = T0 + 8 * DAY
    rep = validation_report(db, cfg, set(), now)
    r = next(x for x in rep["rules"] if x["rule"] == "vwap_fade")
    assert r["n"] == 4 and r["days"] == 4 and r["win_rate"] == 75.0
    assert r["mean_r"] == pytest.approx(0.5)
    se = math.sqrt(0.75 / 4)
    assert r["ci_lo"] == pytest.approx(0.5 - Z95 * se, abs=1e-3)
    assert r["ci_hi"] == pytest.approx(0.5 + Z95 * se, abs=1e-3)
    assert r["status"] == ST_COLLECTING and r["eta_ts"] is not None
    # rules with no trades are listed as waiting, so the card shows the full slate
    assert any(x["status"] == ST_WAITING for x in rep["rules"])
    assert rep["ready"] is False
    db.close()


def test_validated_requires_all_gates(tmp_path) -> None:
    cfg = fresh_cfg()
    cfg.validation.min_trades = 20
    cfg.validation.min_days = 5
    db = Db(tmp_path / "v.db")
    # 20 trades over 10 days, 75% wins of +1 vs -1: mean 0.5, clearly positive
    seed_trades(db, "vwap_fade", [1.0, 1.0, 1.0, -1.0] * 5, T0, spacing_s=DAY / 2)
    db = reopen(db, tmp_path)
    now = T0 + 11 * DAY
    rep = validation_report(db, cfg, set(), now)
    r = next(x for x in rep["rules"] if x["rule"] == "vwap_fade")
    assert r["status"] == ST_VALIDATED and r["eta_ts"] is None
    assert rep["ready"] is True and rep["validated"] == ["vwap_fade"]
    # same record on probation: not validated (desk gate is part of the bar)
    rep2 = validation_report(db, cfg, {"vwap_fade"}, now)
    assert next(x for x in rep2["rules"] if x["rule"] == "vwap_fade")["status"] == ST_COLLECTING
    db.close()


def test_rejected_when_confidently_negative(tmp_path) -> None:
    cfg = fresh_cfg()
    cfg.validation.min_trades = 20
    cfg.validation.min_days = 5
    db = Db(tmp_path / "v.db")
    seed_trades(db, "momentum_ignition", [-1.0, -1.0, -1.0, 0.5] * 5, T0, spacing_s=DAY / 2)
    db = reopen(db, tmp_path)
    rep = validation_report(db, cfg, set(), T0 + 11 * DAY)
    r = next(x for x in rep["rules"] if x["rule"] == "momentum_ignition")
    assert r["ci_hi"] < 0 and r["status"] == ST_REJECTED and r["eta_ts"] is None
    db.close()


def test_eta_uses_recent_rate_and_coverage_floor(tmp_path) -> None:
    cfg = fresh_cfg()  # targets 150 trades / 28 days
    db = Db(tmp_path / "v.db")
    # 14 trades in the last 7 days -> rate 2/day; 143 more needed -> ~71.5 days out
    seed_trades(db, "sweep_reclaim", [1.0, -1.0] * 7, T0, spacing_s=DAY / 2)
    db = reopen(db, tmp_path)
    now = T0 + 7 * DAY
    rep = validation_report(db, cfg, set(), now)
    r = next(x for x in rep["rules"] if x["rule"] == "sweep_reclaim")
    assert r["rate_per_day"] == pytest.approx(2.0)
    assert r["eta_ts"] == pytest.approx(now + (150 - 14) / 2.0 * DAY, abs=DAY)
    db.close()


# --------------------------------------------------------- fee-inclusive wins

def test_win_test_includes_taker_fees() -> None:
    """A tiny gain that beats the spread but not spread+fees is a LOSS."""
    cfg = fresh_cfg()  # spread 2bp + 2 x 4.5bp taker = 11bp round-trip cost
    led = SignalLedger(cfg, None)
    s = FeatureSnapshot(symbol="BTC")
    s.atr_1m = 1.0
    s.price = 100.0
    ev = SignalEvent(T0, "BTC", "long", "momentum_ignition", 0.8, tier="INFO",
                     snapshot={"price": 100.0, "invalidation": 98.0})
    assert led.on_rule_fire(ev, s) is not None
    # cost in R: 0.0011 / (2/100) = 0.055R. Force a +0.05R outcome via TIME exit:
    from tapescreen.core.events import Tick
    (done,) = led.on_tick(Tick(T0 + 7201, T0 + 7201, T0 + 7201, "BTC", 100.10,
                               1.0, "buy", 1, "hyperliquid"))
    assert done.exit_reason == "TIME"
    assert done.total_r == pytest.approx(0.05)
    assert done.total_r > led.haircut / (2.0 / 100.0)          # beats spread alone
    assert done.status == "loss"                               # but not spread+fees
    # and the narration says so
    led2 = SignalLedger(cfg, None)
    assert led2.fee_frac == pytest.approx(2 * 4.5 / 1e4)
