"""Unit tests for tapescreen.store: Db (SQLite WAL, async single-writer) + OutcomeTracker.

All expectations are hand-computed from the documented semantics (module docstrings +
build spec), independent of the implementation. Db writes flow through a queue and a
writer thread, so reads poll until the expected row is visible; Db.close() drains the
queue, so post-close reads need no polling.
"""

from __future__ import annotations

import time

import pytest

from tapescreen.core.events import BUY, Tick
from tapescreen.core.signals.base import LONG, SHORT, TIER_ALERT, TIER_INFO, TIER_WATCH, SignalEvent
from tapescreen.store.db import Db
from tapescreen.store.outcomes import OutcomeTracker

T0 = 1_750_000_000.0
SYM = "BTC"


def sig_ev(ts: float, symbol: str = SYM, side: str = LONG, rule: str = "momentum_ignition",
           strength: float = 0.8, tier: str = TIER_INFO, score: float = 30.0,
           price: float = 100.0, extra: dict | None = None) -> SignalEvent:
    snap: dict = {"price": price, "vol_z": 3.0, "counter_trend": False}
    if extra:
        snap.update(extra)
    return SignalEvent(ts=ts, symbol=symbol, side=side, rule=rule, strength=strength,
                       tier=tier, score=score, snapshot=snap)


def tick(ts: float, price: float, symbol: str = SYM) -> Tick:
    return Tick(ts_exch=ts, ts_recv=ts, ts_mono=ts, symbol=symbol, price=price,
                size=1.0, side=BUY, trade_id=0, venue="hyperliquid")


def wait_for(cond, timeout: float = 5.0):
    """Poll ``cond`` until truthy (writer thread is async); return its last value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = cond()
        if v:
            return v
        time.sleep(0.01)
    return cond()


def outcome_row(db: Db, sid: int):
    with db.read_conn() as conn:
        return conn.execute("SELECT * FROM outcomes WHERE signal_id=?", (sid,)).fetchone()


# ------------------------------------------------------------------------ 1. schema


def test_schema_created(tmp_path) -> None:
    db = Db(tmp_path / "t.db")
    with db.read_conn() as conn:
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
    assert {"signals", "outcomes", "funding_history"} <= names
    assert {"idx_signals_ts", "idx_signals_symbol"} <= names
    db.close()


# ----------------------------------------------------------- 2. signal roundtrip / ids


def test_insert_signal_roundtrip_and_ordering(tmp_path) -> None:
    db = Db(tmp_path / "t.db")
    snap_extra = {"invalidation": 99.5, "note": "abc"}
    sids = [
        db.insert_signal(sig_ev(T0, tier=TIER_INFO, extra=snap_extra)),
        db.insert_signal(sig_ev(T0 + 1, symbol="ETH", side=SHORT, rule="vwap_fade",
                                strength=0.5, tier=TIER_WATCH, score=61.0, price=200.0)),
        db.insert_signal(sig_ev(T0 + 2, tier=TIER_ALERT, score=85.0)),
    ]
    # ids are assigned synchronously and monotonically from 1 on a fresh db
    assert sids == [1, 2, 3]
    assert wait_for(lambda: len(db.recent_signals()) == 3)
    rows = db.recent_signals()
    # ORDER BY ts DESC -> newest first
    assert [r["id"] for r in rows] == [3, 2, 1]
    oldest = rows[-1]
    assert oldest["ts"] == T0
    assert oldest["symbol"] == SYM
    assert oldest["side"] == LONG
    assert oldest["rule"] == "momentum_ignition"
    assert oldest["strength"] == pytest.approx(0.8)
    assert oldest["tier"] == TIER_INFO
    assert oldest["score"] == pytest.approx(30.0)
    assert oldest["price"] == pytest.approx(100.0)  # lifted out of the snapshot
    # snapshot JSON round-trips as a decoded dict
    assert oldest["snapshot"] == {"price": 100.0, "vol_z": 3.0, "counter_trend": False,
                                  "invalidation": 99.5, "note": "abc"}
    # no outcomes yet -> LEFT JOIN gives nulls
    assert oldest["ret_30s"] is None and oldest["mfe_5m"] is None
    assert len(db.recent_signals(limit=2)) == 2
    db.close()


def test_id_seeding_continues_after_reopen(tmp_path) -> None:
    p = tmp_path / "t.db"
    db1 = Db(p)
    for i in range(3):
        db1.insert_signal(sig_ev(T0 + i))
    db1.close()  # flushes the queue
    db2 = Db(p)  # seeds next id from MAX(id)
    sid = db2.insert_signal(sig_ev(T0 + 10))
    assert sid == 4
    assert wait_for(lambda: len(db2.recent_signals()) == 4)
    assert [r["id"] for r in db2.recent_signals()] == [4, 3, 2, 1]
    db2.close()


# --------------------------------------------------------------- 3. outcome COALESCE


def test_upsert_outcome_partial_fills_never_null_earlier_columns(tmp_path) -> None:
    db = Db(tmp_path / "t.db")
    sid = db.insert_signal(sig_ev(T0))
    db.upsert_outcome(sid, {"ret_30s": 0.02})
    row = wait_for(lambda: outcome_row(db, sid))
    assert row["ret_30s"] == pytest.approx(0.02)
    assert row["ret_1m"] is None and row["completed_at"] is None

    db.upsert_outcome(sid, {"ret_1m": -0.01})  # later partial fill omits ret_30s
    row = wait_for(lambda: (r := outcome_row(db, sid)) and r["ret_1m"] is not None and r)
    assert row["ret_30s"] == pytest.approx(0.02)  # COALESCE keeps the earlier fill
    assert row["ret_1m"] == pytest.approx(-0.01)
    assert row["ret_3m"] is None and row["ret_5m"] is None and row["mfe_5m"] is None

    db.upsert_outcome(sid, {"mfe_5m": 0.03, "mae_5m": -0.01, "completed_at": T0 + 300})
    row = wait_for(lambda: (r := outcome_row(db, sid)) and r["completed_at"] is not None and r)
    assert row["ret_30s"] == pytest.approx(0.02)
    assert row["ret_1m"] == pytest.approx(-0.01)
    assert row["mfe_5m"] == pytest.approx(0.03)
    assert row["mae_5m"] == pytest.approx(-0.01)
    assert row["completed_at"] == pytest.approx(T0 + 300)
    db.close()


# ---------------------------------------------------------------------- 4. funding


def test_funding_insert_and_load_window(tmp_path) -> None:
    db = Db(tmp_path / "t.db")
    db.insert_funding(SYM, T0, 1e-4)
    db.insert_funding(SYM, T0 + 60, 2e-4)
    db.insert_funding(SYM, T0 + 120, 3e-4)
    db.insert_funding("ETH", T0, 9e-4)
    db.insert_funding(SYM, T0, 5e-4)  # duplicate (symbol, ts) -> INSERT OR IGNORE
    db.insert_funding("ETH", T0 + 60, 8e-4)  # marker: submitted last, FIFO queue
    assert wait_for(lambda: len(db.load_funding("ETH", 0.0)) == 2)

    rows = db.load_funding(SYM, 0.0)
    assert [(ts, pytest.approx(r)) for ts, r in rows] == [
        (T0, pytest.approx(1e-4)), (T0 + 60, pytest.approx(2e-4)), (T0 + 120, pytest.approx(3e-4))]
    assert rows[0][1] == pytest.approx(1e-4)  # duplicate did not overwrite
    # window filter is ts >= since
    assert db.load_funding(SYM, T0 + 60) == [(T0 + 60, pytest.approx(2e-4)),
                                             (T0 + 120, pytest.approx(3e-4))]
    assert db.load_funding(SYM, T0 + 121) == []
    assert db.load_funding("ETH", T0 + 1) == [(T0 + 60, pytest.approx(8e-4))]
    db.close()


# ------------------------------------------------------------------ 5. stats summary


def test_stats_summary_hand_computed(tmp_path) -> None:
    db = Db(tmp_path / "t.db")
    s1 = db.insert_signal(sig_ev(T0, symbol=SYM, side=LONG, rule="momentum_ignition",
                                 tier=TIER_ALERT, score=85.0))
    s2 = db.insert_signal(sig_ev(T0 + 1, symbol=SYM, side=SHORT, rule="momentum_ignition",
                                 tier=TIER_WATCH, score=65.0))
    s3 = db.insert_signal(sig_ev(T0 + 2, symbol="ETH", side=LONG, rule="sweep_reclaim",
                                 tier=TIER_INFO, score=10.0))
    db.insert_signal(sig_ev(T0 + 3, symbol="ETH", side=SHORT, rule="vwap_fade",
                            tier=TIER_ALERT, score=90.0))  # s4: no outcomes row at all
    db.upsert_outcome(s1, {"ret_30s": 0.001, "ret_1m": 0.01, "ret_3m": 0.02, "ret_5m": 0.03,
                           "mfe_5m": 0.03, "mae_5m": -0.005, "completed_at": T0 + 300})
    db.upsert_outcome(s2, {"ret_30s": -0.001, "ret_1m": 0.0001, "ret_3m": -0.02, "ret_5m": -0.03,
                           "mfe_5m": 0.001, "mae_5m": -0.03, "completed_at": T0 + 301})
    db.upsert_outcome(s3, {"ret_30s": 0.005})  # partial only, not completed
    assert wait_for(lambda: outcome_row(db, s3) is not None)

    out = db.stats_summary(haircut_bps=2.0)  # hit = ret > 0.0002
    assert out["haircut_bps"] == 2.0
    assert out["totals"] == {"signals": 4, "alerts": 2, "completed": 2}

    by_rule = {r["grp"]: r for r in out["by_rule"]}
    assert set(by_rule) == {"momentum_ignition", "sweep_reclaim", "vwap_fade"}
    assert out["by_rule"][0]["grp"] == "momentum_ignition"  # ORDER BY signals DESC

    mi = by_rule["momentum_ignition"]
    assert mi["signals"] == 2 and mi["alerts"] == 1
    # ret_30s: 0.001 hit, -0.001 miss -> 0.5 of 2; ret_1m: 0.01 hit, 0.0001 <= 2bps miss -> 0.5
    for c in ("ret_30s", "ret_1m", "ret_3m", "ret_5m"):
        assert mi[f"hit_{c}"] == pytest.approx(0.5)
        assert mi[f"n_{c}"] == 2
    assert mi["avg_mfe"] == pytest.approx((0.03 + 0.001) / 2)
    assert mi["avg_mae"] == pytest.approx((-0.005 + -0.03) / 2)

    sr = by_rule["sweep_reclaim"]
    assert sr["signals"] == 1 and sr["alerts"] == 0
    assert sr["hit_ret_30s"] == pytest.approx(1.0) and sr["n_ret_30s"] == 1
    assert sr["hit_ret_1m"] is None and sr["n_ret_1m"] == 0  # nulls excluded from the hit rate
    assert sr["avg_mfe"] is None and sr["avg_mae"] is None

    vf = by_rule["vwap_fade"]
    assert vf["signals"] == 1 and vf["alerts"] == 1
    assert vf["hit_ret_30s"] is None and vf["n_ret_30s"] == 0

    by_sym = {r["grp"]: r for r in out["by_symbol"]}
    assert by_sym[SYM]["signals"] == 2 and by_sym[SYM]["alerts"] == 1
    assert by_sym[SYM]["hit_ret_1m"] == pytest.approx(0.5)
    eth = by_sym["ETH"]
    assert eth["signals"] == 2 and eth["alerts"] == 1
    assert eth["hit_ret_30s"] == pytest.approx(1.0) and eth["n_ret_30s"] == 1  # s4 has no row
    assert eth["avg_mfe"] is None
    db.close()


# ------------------------------------------------------------ 6. OutcomeTracker (no db)


def test_outcome_tracker_long_scenario_no_db(cfg) -> None:
    ot = OutcomeTracker(cfg, None)
    ot.track(1, sig_ev(T0, side=LONG, price=100.0))
    sig = ot.open[SYM][0]
    assert sig.pending == [30, 60, 180, 300]

    ot.on_tick(tick(T0 + 10, 101.0))  # ret +0.01: inside window, before every horizon
    assert sig.filled == {}
    assert sig.mfe == pytest.approx(0.01) and sig.mae == pytest.approx(0.0)

    ot.on_tick(tick(T0 + 31, 102.0))  # first tick >= +30s -> ret_30s = 0.02
    assert sig.filled == {"ret_30s": pytest.approx(0.02)}
    assert sig.mfe == pytest.approx(0.02)

    ot.on_tick(tick(T0 + 61, 99.0))  # first tick >= +60s -> ret_1m = -0.01
    assert sig.filled["ret_1m"] == pytest.approx(-0.01)
    assert sig.mae == pytest.approx(-0.01)

    ot.on_tick(tick(T0 + 181, 103.0))  # ret_3m = 0.03
    assert sig.filled["ret_3m"] == pytest.approx(0.03)
    assert sig.mfe == pytest.approx(0.03)

    ot.on_tick(tick(T0 + 301, 100.5))  # ret_5m = 0.005; past the window -> no MFE/MAE update
    assert sig.filled["ret_5m"] == pytest.approx(0.005)
    assert sig.mfe == pytest.approx(0.03) and sig.mae == pytest.approx(-0.01)
    # completion drops it from the tracker
    assert ot.open[SYM] == []
    assert ot.completed_total == 1


def test_outcome_tracker_short_scenario_signs_flip(cfg) -> None:
    ot = OutcomeTracker(cfg, None)
    ot.track(7, sig_ev(T0, side=SHORT, price=100.0))
    sig = ot.open[SYM][0]
    for ts, px in ((T0 + 10, 101.0), (T0 + 31, 102.0), (T0 + 61, 99.0),
                   (T0 + 181, 103.0), (T0 + 301, 100.5)):
        ot.on_tick(tick(ts, px))
    assert sig.filled["ret_30s"] == pytest.approx(-0.02)
    assert sig.filled["ret_1m"] == pytest.approx(0.01)
    assert sig.filled["ret_3m"] == pytest.approx(-0.03)
    assert sig.filled["ret_5m"] == pytest.approx(-0.005)
    assert sig.mfe == pytest.approx(0.01) and sig.mae == pytest.approx(-0.03)
    assert ot.open[SYM] == [] and ot.completed_total == 1


def test_horizons_fill_only_at_first_tick_at_or_after(cfg) -> None:
    ot = OutcomeTracker(cfg, None)
    ot.track(1, sig_ev(T0, side=LONG, price=100.0))
    sig = ot.open[SYM][0]
    ot.on_tick(tick(T0 - 5, 50.0))  # pre-signal tick is ignored entirely
    assert sig.filled == {} and sig.mfe == 0.0 and sig.mae == 0.0
    # first tick after BOTH the 30s and 60s horizons fills both with the same ret
    ot.on_tick(tick(T0 + 65, 104.0))
    assert sig.filled == {"ret_30s": pytest.approx(0.04), "ret_1m": pytest.approx(0.04)}
    assert sig.pending == [180, 300]
    # a later tick never rewrites already-filled horizons
    ot.on_tick(tick(T0 + 70, 200.0))
    assert sig.filled["ret_30s"] == pytest.approx(0.04)
    assert sig.filled["ret_1m"] == pytest.approx(0.04)
    assert sig.mfe == pytest.approx(1.0)


def test_tracker_ignores_context_and_zero_entry(cfg) -> None:
    ot = OutcomeTracker(cfg, None)
    ot.track(1, sig_ev(T0, side=""))  # context: no side -> not tracked
    ot.track(2, sig_ev(T0, price=0.0))  # no entry price -> not tracked
    assert ot.open == {}
    ot.on_tick(tick(T0 + 1, 100.0))  # tick with nothing open is a no-op
    assert ot.completed_total == 0


# ---------------------------------------------------------- 7. OutcomeTracker + real Db


def test_outcome_tracker_persists_through_db(cfg, tmp_path) -> None:
    db = Db(tmp_path / "t.db")
    ot = OutcomeTracker(cfg, db)
    ev = sig_ev(T0, side=LONG, price=100.0, tier=TIER_ALERT, score=85.0)
    sid = db.insert_signal(ev)
    ot.track(sid, ev)
    for ts, px in ((T0 + 10, 101.0), (T0 + 31, 102.0), (T0 + 61, 99.0),
                   (T0 + 181, 103.0), (T0 + 301, 100.5)):
        ot.on_tick(tick(ts, px))
    assert ot.open[SYM] == []  # completed in-memory immediately

    row = wait_for(lambda: (r := outcome_row(db, sid)) and r["completed_at"] is not None and r)
    assert row["ret_30s"] == pytest.approx(0.02)
    assert row["ret_1m"] == pytest.approx(-0.01)
    assert row["ret_3m"] == pytest.approx(0.03)
    assert row["ret_5m"] == pytest.approx(0.005)
    assert row["mfe_5m"] == pytest.approx(0.03)
    assert row["mae_5m"] == pytest.approx(-0.01)
    assert row["completed_at"] == pytest.approx(T0 + 300)  # signal ts + mfe_mae window

    joined = db.recent_signals()[0]  # outcomes visible through the signals join
    assert joined["id"] == sid
    assert joined["ret_1m"] == pytest.approx(-0.01)
    assert joined["mfe_5m"] == pytest.approx(0.03)
    db.close()


# ----------------------------------------------------------------- 8. close() flushes


def test_close_flushes_pending_writes(tmp_path) -> None:
    p = tmp_path / "t.db"
    db = Db(p)
    sid = db.insert_signal(sig_ev(T0))
    db.upsert_outcome(sid, {"ret_30s": 0.001})
    db.insert_funding(SYM, T0, 1e-4)
    db.close()  # must drain the queue before the writer thread exits

    db2 = Db(p)  # no polling: everything must already be on disk
    rows = db2.recent_signals()
    assert len(rows) == 1
    assert rows[0]["id"] == sid
    assert rows[0]["ret_30s"] == pytest.approx(0.001)
    assert db2.load_funding(SYM, 0.0) == [(T0, pytest.approx(1e-4))]
    db2.close()
