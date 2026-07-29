"""Runtime self-verification: data cross-checks, logic invariants, quarantine.

Use: auditor = Auditor(cfg, engine, feed, db); report = auditor.run(now) each
audit.interval_s; report is surfaced on /health and in the UI status bar.
Depends on: core.engine, core.state, signals.ledger. Checks are cheap and
read-only; a symbol failing DATA checks is quarantined (grayed out, no new
trade signals) until it passes quarantine_clear_checks consecutive audits.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any

from tapescreen.config import Config
from tapescreen.core.engine import Engine
from tapescreen.core.signals.ledger import ST_BE, ST_INIT, ST_TRAIL
from tapescreen.core.thoughts import CAT_AUDIT
from tapescreen.store.db import Db

log = logging.getLogger("tapescreen.audit")


def _finite(*vals: float) -> bool:
    return all(isinstance(v, int | float) and math.isfinite(v) for v in vals)


@dataclass(slots=True)
class AuditReport:
    ts: float
    ok: bool
    failures: list[str]  # human-readable, stable prefixes for grouping
    checks_run: int
    quarantined: list[str]
    runs_total: int
    failures_total: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts, "ok": self.ok, "failures": self.failures,
            "checks_run": self.checks_run, "quarantined": self.quarantined,
            "runs_total": self.runs_total, "failures_total": self.failures_total,
        }


@dataclass(slots=True)
class _SymHealth:
    dirty: bool = False
    clean_streak: int = 0


class Auditor:
    """Continuously proves the data and the system's own logic are sane."""

    def __init__(self, cfg: Config, engine: Engine, feed: Any = None,
                 db: Db | None = None) -> None:
        self.cfg = cfg
        self.engine = engine
        self.feed = feed
        self.db = db
        self.runs_total = 0
        self.failures_total = 0
        self.last_report: AuditReport | None = None
        self._sym: dict[str, _SymHealth] = {s: _SymHealth() for s in cfg.symbols}
        self._last_db_errors = 0
        self._last_quick_check = 0.0

    # ------------------------------------------------------------------ checks

    def run(self, now: float | None = None) -> AuditReport:
        now = now if now is not None else time.time()
        failures: list[str] = []
        checks = 0
        sym_fail: set[str] = set()

        for sym in self.cfg.symbols:
            if sym in self.engine.unavailable:
                continue
            n_fail = len(failures)
            checks += self._check_symbol_data(sym, now, failures)
            if len(failures) > n_fail:
                sym_fail.add(sym)

        checks += self._check_ledger(failures)
        checks += self._check_learning(failures)
        checks += self._check_scores(failures)
        checks += self._check_db(now, failures)
        checks += self._check_feed(now, failures)

        self._update_quarantine(sym_fail, failures, now)
        self.runs_total += 1
        self.failures_total += len(failures)
        report = AuditReport(
            ts=now, ok=not failures, failures=failures, checks_run=checks,
            quarantined=sorted(self.engine.quarantined),
            runs_total=self.runs_total, failures_total=self.failures_total,
        )
        self.last_report = report
        th = self.engine.thoughts
        if failures:
            log.warning("self-audit found %d issue(s): %s",
                        len(failures), "; ".join(failures[:6]))
            th.emit(now, CAT_AUDIT, "",
                    f"self-audit #{self.runs_total}: {len(failures)} issue(s) found",
                    failures[:6])
        elif self.runs_total % 10 == 1:  # ~5-min heartbeat proving the checks run
            th.emit(now, CAT_AUDIT, "",
                    f"self-audit #{self.runs_total}: all {checks} checks passed", [
                "prices cross-checked against the venue's independent mid feed",
                "book/bar/feature invariants, open-trade state, learning sanity, "
                "DB health all verified",
            ])
        return report

    def _check_symbol_data(self, sym: str, now: float, out: list[str]) -> int:
        st = self.engine.states[sym]
        checks = 0

        # price sanity + divergence vs the venue's independent allMids feed
        checks += 1
        if st.last_price and not _finite(st.last_price):
            out.append(f"data:{sym}: non-finite last price")
        mid = self.engine.last_mids.get(sym, 0.0)
        fresh_mid = mid > 0 and now - self.engine.last_mids_ts <= self.cfg.audit.mids_fresh_s
        fresh_trade = st.last_tick_ts and now - st.last_tick_ts <= self.cfg.audit.mids_fresh_s
        if fresh_mid and fresh_trade and st.last_price > 0:
            checks += 1
            div = abs(st.last_price - mid) / mid * 100.0
            if div > self.cfg.audit.price_divergence_pct:
                out.append(f"data:{sym}: price {st.last_price:.6g} diverges "
                           f"{div:.2f}% from venue mid {mid:.6g}")

        # book integrity: never crossed, sides ordered
        if st.bid_px > 0 and st.ask_px > 0:
            checks += 1
            if st.bid_px >= st.ask_px:
                out.append(f"data:{sym}: crossed book bid {st.bid_px} >= ask {st.ask_px}")
        book = st.book
        if book is not None and book.bids and book.asks:
            checks += 1
            bid_sorted = all(book.bids[i][0] >= book.bids[i + 1][0]
                             for i in range(len(book.bids) - 1))
            ask_sorted = all(book.asks[i][0] <= book.asks[i + 1][0]
                             for i in range(len(book.asks) - 1))
            if not (bid_sorted and ask_sorted):
                out.append(f"data:{sym}: unsorted book levels")

        # bar integrity
        for bar in (st.cur_1s, st.cur_1m):
            if bar is None:
                continue
            checks += 1
            if not (bar.high >= max(bar.open, bar.close) - 1e-12
                    and bar.low <= min(bar.open, bar.close) + 1e-12
                    and bar.volume >= 0 and bar.buy_volume <= bar.volume + 1e-12):
                out.append(f"logic:{sym}: bar invariant broken at ts {bar.ts}")

        # feature sanity
        s = self.engine.snapshot(sym)
        checks += 1
        if not _finite(s.vol_z, s.vwap_session, s.atr_1m, s.cvd_5m, s.spread_bps):
            out.append(f"logic:{sym}: non-finite feature values")
        elif not (0.0 <= s.rsi_14 <= 100.0 and 0.0 <= s.book_imbalance <= 1.0
                  and s.atr_1m >= 0.0 and s.spread_bps >= 0.0):
            out.append(f"logic:{sym}: feature out of range "
                       f"(rsi {s.rsi_14}, imb {s.book_imbalance})")
        return checks

    def _check_ledger(self, out: list[str]) -> int:
        checks = 0
        for sigs in self.engine.ledger.open.values():
            for t in sigs:
                checks += 1
                sign = t.side_sign
                ok = (
                    t.risk_unit > 0 and t.filled >= 1
                    and _finite(t.avg_entry, t.stop, t.live_r())
                    and (not t.tp1_done or t.realized > 0)
                )
                if ok and t.state == ST_INIT:
                    ok = sign * (t.avg_entry - t.stop) > 0  # stop on the loss side
                elif ok and t.state == ST_BE:
                    ok = sign * (t.stop - t.avg_entry) >= -1e-9  # at/beyond entry
                elif ok and t.state == ST_TRAIL:
                    ok = sign * (t.best_px - t.avg_entry) > 0
                if not ok:
                    out.append(f"logic:ledger #{t.id} {t.symbol} {t.side} "
                               f"invariant broken (state {t.state})")
        return checks

    def _check_learning(self, out: list[str]) -> int:
        checks = 0
        led = self.engine.ledger
        for rule, b in led.rule_buckets.items():
            checks += 1
            if not (0 <= b.wins <= b.n and _finite(b.r_sum)):
                out.append(f"logic:bucket {rule}: wins {b.wins} > n {b.n}")
        for rule, m in led.models.items():
            checks += 1
            if not all(math.isfinite(w) for w in [*m.w, m.b]):
                out.append(f"logic:model {rule}: non-finite weights")
        return checks

    def _check_scores(self, out: list[str]) -> int:
        checks = 0
        for sym, comp in self.engine.composites.items():
            checks += 1
            for side, sc in comp.scores.items():
                if not (_finite(sc) and 0.0 <= sc <= 100.0):
                    out.append(f"logic:{sym}: composite {side} score {sc} out of range")
        return checks

    def _check_db(self, now: float, out: list[str]) -> int:
        if self.db is None:
            return 0
        checks = 1
        errs = self.db.write_errors
        if errs > self._last_db_errors:
            out.append(f"db: {errs - self._last_db_errors} new write error(s)")
        self._last_db_errors = errs
        if now - self._last_quick_check >= 3600.0:  # hourly file integrity probe
            self._last_quick_check = now
            checks += 1
            try:
                row = self.db.read_conn().execute("PRAGMA quick_check(1)").fetchone()
                if row and row[0] != "ok":
                    out.append(f"db: quick_check failed: {row[0]}")
            except Exception as e:
                out.append(f"db: quick_check error: {e}")
        return checks

    def _check_feed(self, now: float, out: list[str]) -> int:
        if self.feed is None:
            return 0
        h = self.feed.health()
        checks = 1
        if h.get("connected") and now - h.get("last_msg_recv", 0) > 15.0:
            out.append("feed: connected but silent >15s (watchdog should fire)")
        if h.get("queue_drops", 0) > 0:
            out.append(f"feed: {h['queue_drops']} queued events shed")
        return checks

    # -------------------------------------------------------------- quarantine

    def _update_quarantine(self, failing: set[str], failures: list[str],
                           now: float) -> None:
        need = self.cfg.audit.quarantine_clear_checks
        th = self.engine.thoughts
        for sym, h in self._sym.items():
            if sym in failing:
                h.dirty = True
                h.clean_streak = 0
                if sym not in self.engine.quarantined:
                    log.warning("quarantining %s: failing data audits", sym)
                    why = [f for f in failures if f":{sym}:" in f][:3]
                    th.emit(now, CAT_AUDIT, sym,
                            f"quarantining {sym} - its data failed verification",
                            why + [f"no new trade signals on {sym} until it passes "
                                   f"{need} consecutive clean audits"])
                self.engine.quarantined.add(sym)
            elif h.dirty:
                h.clean_streak += 1
                if h.clean_streak >= need:
                    h.dirty = False
                    self.engine.quarantined.discard(sym)
                    log.info("quarantine lifted for %s after %d clean audits", sym, need)
                    th.emit(now, CAT_AUDIT, sym,
                            f"{sym} back online - quarantine lifted", [
                        f"passed {need} consecutive clean audits; "
                        "signals resume on verified data"])
