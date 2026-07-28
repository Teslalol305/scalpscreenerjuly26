"""SignalLedger v3 tests: ladder entries, BE/trail management, learning, DB."""

from __future__ import annotations

import sqlite3
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


def fire(side: str = "long", price: float = 100.0, inv: float = 98.0,
         rule: str = "momentum_ignition", ts: float = T0) -> SignalEvent:
    return SignalEvent(ts, "BTC", side, rule, 0.8, tier="INFO", score=30.0,
                       snapshot={"price": price, "invalidation": inv})


def tick(ts: float, px: float) -> Tick:
    return Tick(ts, ts, ts, "BTC", px, 1.0, "buy", int(ts * 10) % 10**9, "hyperliquid")


# --------------------------------------------------------------- entry ladder

def test_ladder_construction_long() -> None:
    led = SignalLedger(fresh_cfg(), None)
    # inv 98 -> risk 2.0, inside [1.0, 4.0] x ATR(1.0)? clamp: min(4*1, max(1*1, 2)) = 1.0? no:
    # stop_atr_min/max are in ATR multiples: min(4.0, max(1.0, 2.0)) = 2.0 -> stop 98
    t = led.on_rule_fire(fire(), snap(atr=1.0))
    assert t.risk_unit == pytest.approx(2.0)
    assert t.stop == pytest.approx(98.0)
    # ladder: E1 at signal, adds each 0.5R = 1.0 lower
    assert [lv["px"] for lv in t.levels] == pytest.approx([100.0, 99.0, 98.0])
    assert t.filled == 1 and t.avg_entry == pytest.approx(100.0)
    assert t.confidence == pytest.approx(0.5) and t.conf_src == "bucket"


def test_ladder_fills_improve_average_entry() -> None:
    led = SignalLedger(fresh_cfg(), None)
    led.on_rule_fire(fire(), snap())
    assert led.on_tick(tick(T0 + 30, 99.0)) == []  # E2 fills, still open
    t = led.open["BTC"][0]
    assert t.filled == 2 and t.avg_entry == pytest.approx(99.5)


def test_ladder_freezes_after_entry_window() -> None:
    cfg = fresh_cfg()  # entry_window_s 900
    led = SignalLedger(cfg, None)
    led.on_rule_fire(fire(), snap())
    led.on_tick(tick(T0 + 901, 99.0))  # past the window: no fill
    t = led.open["BTC"][0]
    assert t.filled == 1 and t.avg_entry == pytest.approx(100.0)


def test_stop_out_with_partial_ladder_costs_half_r() -> None:
    led = SignalLedger(fresh_cfg(), None)
    led.on_rule_fire(fire(), snap())
    # one print sweeps through E2, E3 and the stop: fills average to 99, exit at 98
    (t,) = led.on_tick(tick(T0 + 10, 97.9))
    assert t.filled == 3 and t.avg_entry == pytest.approx(99.0)
    assert t.exit_reason == "STOP" and t.status == "loss"
    # scaling thesis in one number: full stop-out loses (98-99)/2 = -0.5R, not -1R
    assert t.total_r == pytest.approx(-0.5)


# ------------------------------------------------------------ trade management

def test_tp1_banks_partial_and_moves_stop_to_break_even() -> None:
    cfg = fresh_cfg()
    led = SignalLedger(cfg, None)
    led.on_rule_fire(fire(), snap())  # long 100, risk 2
    assert led.on_tick(tick(T0 + 60, 102.0)) == []  # +1R: TP1 + BE, stays open
    t = led.open["BTC"][0]
    assert t.tp1_done and t.state == "BE"
    assert t.stop == pytest.approx(100.0)
    assert t.realized == pytest.approx(cfg.learning.tp1_fraction * cfg.learning.tp1_r)
    (done,) = led.on_tick(tick(T0 + 120, 99.9))  # BE stop tags out the runner
    assert done.exit_reason == "BE" and done.status == "win"
    assert done.total_r == pytest.approx(t.realized)  # runner leg exits flat


def test_trailing_stop_ratchets_and_exits() -> None:
    cfg = fresh_cfg()
    f = cfg.learning.tp1_fraction
    led = SignalLedger(cfg, None)
    led.on_rule_fire(fire(), snap())  # long 100, risk 2
    led.on_tick(tick(T0 + 60, 103.0))  # +1.5R: TP1 done AND trail armed
    t = led.open["BTC"][0]
    assert t.state == "TRAIL"
    assert t.stop == pytest.approx(101.0)  # 103 - 1R(=2.0)
    led.on_tick(tick(T0 + 120, 105.0))  # ratchet: stop -> 103
    assert t.stop == pytest.approx(103.0)
    led.on_tick(tick(T0 + 130, 104.0))  # pullback above stop: no change
    assert t.stop == pytest.approx(103.0)
    (done,) = led.on_tick(tick(T0 + 180, 102.9))
    assert done.exit_reason == "TRAIL" and done.status == "win"
    # banked f*1R + runner (1-f) * (103-100)/2
    assert done.total_r == pytest.approx(f * 1.0 + (1 - f) * 1.5)


def test_time_exit_at_max_hold() -> None:
    cfg = fresh_cfg()  # max_hold_s 7200 (2h)
    led = SignalLedger(cfg, None)
    led.on_rule_fire(fire(), snap())
    led.on_tick(tick(T0 + 7100, 100.5))  # still inside the hold window
    assert led.open["BTC"]
    (t,) = led.on_tick(tick(T0 + 7200, 100.5))
    assert t.exit_reason == "TIME" and t.status == "win"  # +0.25R > haircut
    assert t.total_r == pytest.approx(0.25)


def test_short_side_trail_mirror() -> None:
    cfg = fresh_cfg()
    f = cfg.learning.tp1_fraction
    led = SignalLedger(cfg, None)
    led.on_rule_fire(fire(side="short", price=100.0, inv=102.0), snap())
    led.on_tick(tick(T0 + 60, 97.0))  # -1.5R move: TP1 + TRAIL, stop 97+2=99
    t = led.open["BTC"][0]
    assert t.state == "TRAIL" and t.stop == pytest.approx(99.0)
    (done,) = led.on_tick(tick(T0 + 120, 99.1))
    assert done.exit_reason == "TRAIL" and done.status == "win"
    # banked f*1R + runner exits at the 99 trail: (100-99)/2 = 0.5R
    assert done.total_r == pytest.approx(f * 1.0 + (1 - f) * 0.5)


def test_one_per_side_dedup() -> None:
    led = SignalLedger(fresh_cfg(), None)
    assert led.on_rule_fire(fire(), snap()) is not None
    assert led.on_rule_fire(fire(rule="vwap_fade"), snap()) is None
    assert led.on_rule_fire(fire(side="short", inv=102.0), snap()) is not None


# ------------------------------------------------------------------- learning

def test_confidence_backoff_and_model_gate() -> None:
    cfg = fresh_cfg()  # min_bucket_n 20, model_min_n 30, priors 3/3
    led = SignalLedger(cfg, None)
    led.rule_buckets["r"] = Bucket(wins=7, n=10)
    p, n, src = led.confidence("r", "BTC", None)
    assert (p, n, src) == (pytest.approx(0.625), 10, "bucket")
    # an under-trained model must NOT take over
    m = led._model("r")
    for _ in range(10):
        m.update([1.0] * 12, True)
    _, _, src2 = led.confidence("r", "BTC", [1.0] * 12)
    assert src2 == "bucket"
    for _ in range(25):
        m.update([1.0] * 12, True)
    p3, n3, src3 = led.confidence("r", "BTC", [1.0] * 12)
    assert src3 == "model" and n3 == m.n and 0.0 < p3 < 1.0


def test_resolution_trains_model_and_updates_buckets() -> None:
    led = SignalLedger(fresh_cfg(), None)
    led.on_rule_fire(fire(), snap())
    led.on_tick(tick(T0 + 60, 105.0))
    led.on_tick(tick(T0 + 120, 102.9))  # trail exit -> win
    b = led.rule_buckets["momentum_ignition"]
    assert b.n == 1.0 and b.wins == 1.0 and b.r_n == 1.0 and b.r_sum > 0
    assert led._model("momentum_ignition").n == 1
    snap_l = led.learning_snapshot()[0]
    assert snap_l["model_n"] == 1 and snap_l["avg_r"] > 0


def test_db_roundtrip_refit_and_v2_schema_migration(tmp_path) -> None:
    cfg = fresh_cfg()
    # simulate a pre-v3 database: v0.2 trade_signals without the new columns
    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)
    conn.execute("""CREATE TABLE trade_signals (
        id INTEGER PRIMARY KEY, signal_id INTEGER, ts REAL NOT NULL,
        symbol TEXT NOT NULL, side TEXT NOT NULL, rule TEXT NOT NULL,
        tier TEXT NOT NULL, confidence REAL NOT NULL, entry REAL NOT NULL,
        stop REAL NOT NULL, target REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'open', exit_ts REAL, exit_price REAL,
        exit_reason TEXT, r_result REAL)""")
    conn.commit()
    conn.close()
    db = Db(old)  # must upgrade in place
    cols = {r[1] for r in db.read_conn().execute("PRAGMA table_info(trade_signals)")}
    assert {"avg_entry", "entries", "state", "features", "realized_r"} <= cols

    led = SignalLedger(cfg, db)
    led.on_rule_fire(fire(rule="vwap_fade"), snap())
    led.on_tick(tick(T0 + 60, 103.0))
    led.on_tick(tick(T0 + 120, 100.0))  # trail stop hit -> resolved
    deadline = time.time() + 5
    rows = []
    while time.time() < deadline:  # async writer thread
        rows = db.recent_trade_signals()
        if rows and rows[0]["status"] != "open":
            break
        time.sleep(0.05)
    assert rows[0]["status"] == "win"
    assert rows[0]["state"] == "TRAIL" and rows[0]["features"]

    # a fresh ledger refits its model from the stored history
    led2 = SignalLedger(cfg, db)
    assert led2._model("vwap_fade").n == 1
    assert led2.rule_buckets["vwap_fade"].n == 1.0
    assert db.load_models().get("vwap_fade")  # persisted for inspection
    db.close()
