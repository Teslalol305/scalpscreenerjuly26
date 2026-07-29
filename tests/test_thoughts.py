"""Reasoning-trace tests: ThoughtLog core, and that every decision point narrates."""

from __future__ import annotations

from tapescreen.config import load_config
from tapescreen.core.events import Tick
from tapescreen.core.features import FeatureSnapshot
from tapescreen.core.signals.base import SignalEvent
from tapescreen.core.signals.composite import Composite
from tapescreen.core.signals.ledger import SignalLedger
from tapescreen.core.thoughts import (
    CAT_AUDIT,
    CAT_LEARN,
    CAT_SIGNAL,
    CAT_TRADE,
    ThoughtLog,
    px,
)
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


def tick(ts: float, price: float) -> Tick:
    return Tick(ts, ts, ts, "BTC", price, 1.0, "buy", int(ts * 10) % 10**9, "hyperliquid")


def heads(th: ThoughtLog, cat: str | None = None) -> list[str]:
    return [t["headline"] for t in th.recent if cat is None or t["cat"] == cat]


# ------------------------------------------------------------------- core log

def test_emit_ring_and_listeners() -> None:
    th = ThoughtLog(None, maxlen=3)
    got: list[dict] = []
    th.add_listener(got.append)
    for i in range(5):
        th.emit(T0 + i, CAT_SIGNAL, "BTC", f"h{i}", [f"d{i}"])
    assert len(got) == 5 and got[0]["headline"] == "h0"
    assert [t["headline"] for t in th.recent] == ["h2", "h3", "h4"]  # ring caps at 3
    assert got[0]["id"] == 1 and got[4]["id"] == 5  # monotonic ids
    assert got[1]["detail"] == ["d1"]


def test_px_formatting() -> None:
    assert px(117842.4) == "117,842.4"
    assert px(48.213) == "48.21"
    assert px(0.2345) == "0.2345"
    assert px(0.004312) == "0.004312"


def test_db_round_trip_and_restore(tmp_path) -> None:
    db = Db(tmp_path / "t.db")
    th = ThoughtLog(db)
    th.emit(T0, CAT_TRADE, "SOL", "opened long", ["line one", "line two"])
    th.emit(T0 + 1, CAT_LEARN, "", "learned", [])
    db.close()

    db2 = Db(tmp_path / "t.db")
    th2 = ThoughtLog(db2)  # restores the trail and continues numbering
    assert [t["headline"] for t in th2.recent] == ["opened long", "learned"]
    assert th2.recent[0]["symbol"] == "SOL"
    assert th2.recent[0]["detail"] == ["line one", "line two"]
    nxt = th2.emit(T0 + 2, CAT_AUDIT, "", "after restart", [])
    assert nxt["id"] == 3
    db2.close()


# ------------------------------------------------------------ composite trace

def test_rule_fire_and_cooldown_suppression_narrated() -> None:
    cfg = fresh_cfg()
    th = ThoughtLog(None)
    comp = Composite(cfg, "BTC", thoughts=th)
    s = FeatureSnapshot(symbol="BTC")
    s.ts = T0
    s.price = 100.0
    s.warming = False
    # drive momentum_ignition: roc30 over threshold, vol_z hot, cvd agrees
    s.roc_30s = 0.01
    s.roc30_sigma = 0.001
    s.vol_z = 4.0
    s.cvd_slope_1m = 1.0
    s.spread_bps = 1.0
    events, _ = comp.evaluate(s)
    assert len(events) == 1
    fired = [t for t in th.recent if t["cat"] == CAT_SIGNAL]
    assert len(fired) == 1
    assert "momentum_ignition fired long" in fired[0]["headline"]
    detail = " | ".join(fired[0]["detail"])
    assert "score math" in detail and "tier" in detail and "evidence" in detail

    # same setup 1s later: suppression narrated exactly once for this window
    s.ts = T0 + 1.0
    comp.evaluate(s)
    s.ts = T0 + 2.0
    comp.evaluate(s)
    sup = [t for t in th.recent if "holding fire" in t["headline"]]
    assert len(sup) == 1
    assert "cooldown" in sup[0]["detail"][0]


# --------------------------------------------------------------- ledger trace

def test_full_trade_lifecycle_is_narrated() -> None:
    cfg = fresh_cfg()
    th = ThoughtLog(None)
    led = SignalLedger(cfg, None, thoughts=th)
    led.on_rule_fire(fire(), snap())  # entry: stop 98, 1R = 2

    opened = heads(th, CAT_TRADE)
    assert any(h.startswith("opening long") for h in opened)
    entry_th = next(t for t in th.recent if t["headline"].startswith("opening long"))
    joined = " | ".join(entry_th["detail"])
    assert "stop 98.00" in joined and "scale-in ladder" in joined
    assert "win prob" in joined and "plan:" in joined

    led.on_tick(tick(T0 + 10, 99.0))     # E2 fills
    assert any("add filled: E2" in h for h in heads(th, CAT_TRADE))

    led.on_tick(tick(T0 + 60, 101.0))    # +1R off avg 99.5? leg_r=(101-99.5)/2=0.75 -> no
    led.on_tick(tick(T0 + 90, 101.6))    # +1.05R -> TP1 + BE
    assert any("stop to break-even" in h for h in heads(th, CAT_TRADE))

    led.on_tick(tick(T0 + 120, 102.6))   # best +1.55R -> trailing engages
    assert any("trailing stop engaged" in h for h in heads(th, CAT_TRADE))

    (closed,) = led.on_tick(tick(T0 + 150, 100.5))  # falls to the trail stop
    assert closed.status == "win"
    exit_th = next(t for t in th.recent if t["headline"].startswith("closed long"))
    assert "trailing stop" in exit_th["headline"]
    assert any("win test" in d for d in exit_th["detail"])

    learned = [t for t in th.recent if t["cat"] == CAT_LEARN]
    assert len(learned) == 1
    ldetail = " | ".join(learned[0]["detail"])
    assert "record" in ldetail and "score weight" in ldetail and "ML model" in ldetail


def test_one_per_side_skip_is_narrated() -> None:
    th = ThoughtLog(None)
    led = SignalLedger(fresh_cfg(), None, thoughts=th)
    assert led.on_rule_fire(fire(), snap()) is not None
    assert led.on_rule_fire(fire(ts=T0 + 1), snap()) is None
    assert any("not stacking" in h for h in heads(th, CAT_TRADE))


def test_stop_clamp_reasoning_present() -> None:
    th = ThoughtLog(None)
    led = SignalLedger(fresh_cfg(), None, thoughts=th)
    # invalidation 99.9 -> raw dist 0.1 < 1.0 x ATR floor -> widened
    led.on_rule_fire(fire(inv=99.9), snap(atr=1.0))
    entry_th = next(t for t in th.recent if t["headline"].startswith("opening"))
    assert any("widened" in d and "floor" in d for d in entry_th["detail"])
