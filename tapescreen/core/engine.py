"""Engine: consumes canonical events, owns per-symbol state and health metrics.

Use: eng = Engine(cfg); eng.on_event(ev) per event; eng.status() for the UI bar.
Depends on: core.events. (Feature/signal layers attach here in later milestones.)
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from tapescreen.config import Config
from tapescreen.core.events import Bbo, BookTop, Event, FeedStatus, Mids, PerpCtx, Tick
from tapescreen.core.features import FeatureEngine, FeatureSnapshot
from tapescreen.core.research import ResearchDesk
from tapescreen.core.signals.base import SignalEvent
from tapescreen.core.signals.composite import Composite
from tapescreen.core.signals.ledger import SignalLedger
from tapescreen.core.state import SymbolState
from tapescreen.core.thoughts import CAT_SYSTEM, CAT_TRADE, ThoughtLog
from tapescreen.store.db import Db
from tapescreen.store.outcomes import OutcomeTracker


def percentile(sorted_vals: list[float], q: float) -> float:
    """Nearest-rank percentile on a pre-sorted list; 0.0 for empty input."""
    if not sorted_vals:
        return 0.0
    idx = max(0, min(len(sorted_vals) - 1, round(q / 100.0 * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


class Engine:
    """Event fan-in: state -> features -> signals -> persistence + UI feed."""

    def __init__(self, cfg: Config, db: Db | None = None,
                 seed_funding_from_db: bool = True) -> None:
        self.cfg = cfg
        self.db = db
        self.unavailable: set[str] = set()  # symbols absent/delisted on the venue
        self.quarantined: set[str] = set()  # failing data audits: no new trade signals
        self.last_mids: dict[str, float] = {}  # venue allMids: independent price reference
        self.last_mids_ts = 0.0
        self.started_at = time.time()
        self.events_total = 0
        self.ticks_total = 0
        # ingest latency = ts_recv - ts_exch on trades (includes venue+network+clock skew)
        self.ingest_lat: deque[float] = deque(maxlen=1000)
        # monotonic receive stamps of unpushed ticks; UI server drains for tick->UI latency
        self.tick_monos: deque[float] = deque(maxlen=2000)
        self.last_tick_ts: dict[str, float] = {sym: 0.0 for sym in cfg.symbols}
        self.last_any_ts: dict[str, float] = {sym: 0.0 for sym in cfg.symbols}
        self.feed_status: dict[str, FeedStatus] = {}
        # per-symbol rolling state + feature engines + composite scorers
        self.states: dict[str, SymbolState] = {sym: SymbolState(sym, cfg) for sym in cfg.symbols}
        self.features: dict[str, FeatureEngine] = {
            sym: FeatureEngine(st) for sym, st in self.states.items()
        }
        self.thoughts = ThoughtLog(db)
        from tapescreen import __version__
        self.thoughts.emit(time.time(), CAT_SYSTEM, "",
                           f"TapeScreen v{__version__} starting: {len(cfg.symbols)} symbols", [
            "boot self-test verified core math against hand-computed values "
            "before this point (the app refuses to start otherwise)"
            if cfg.audit.selftest_on_boot else "boot self-test disabled in config",
            "every decision from here on is narrated below, as it happens",
        ])
        self.ledger = SignalLedger(cfg, db, thoughts=self.thoughts)
        self.research: ResearchDesk | None = (
            ResearchDesk(cfg, self.ledger, self.thoughts, db)
            if cfg.research.enabled else None
        )
        self.composites: dict[str, Composite] = {
            sym: Composite(cfg, sym, weight_mult=self.ledger.rule_multiplier,
                           thoughts=self.thoughts)
            for sym in cfg.symbols
        }
        # session equity curve: [ts, r, cum_r] per resolved trade, last 24h
        self.session_curve: list[list[float]] = []
        if db is not None:
            cum = 0.0
            for ts, r in db.closed_trades_since(time.time() - 86400.0):
                cum += r
                self.session_curve.append([ts, r, round(cum, 3)])
        self.flags: dict[str, dict] = {sym: {} for sym in cfg.symbols}
        self.outcomes = OutcomeTracker(cfg, db)
        self.signal_feed: deque[dict] = deque(maxlen=500)  # newest last; UI reverses
        self.signals_total = 0
        self._signal_listeners: list = []  # callables(dict) for immediate UI push
        self._trade_listeners: list = []  # callables(kind, dict) for entry/exit pushes
        self._funding_persist_ts: dict[str, float] = {sym: 0.0 for sym in cfg.symbols}
        for sym, st in self.states.items():
            st.on_bar_1s.append(self._make_signal_hook(sym))
        if db is not None and seed_funding_from_db:
            self._seed_funding_from_db()

    def mark_unavailable(self, symbols: set[str]) -> None:
        self.unavailable = set(symbols)

    def _seed_funding_from_db(self) -> None:
        assert self.db is not None
        since = time.time() - 7 * 86400.0
        for sym, st in self.states.items():
            hist = self.db.load_funding(sym, since)
            if hist:
                st.seed_funding(hist)

    def add_signal_listener(self, cb) -> None:
        self._signal_listeners.append(cb)

    def add_trade_listener(self, cb) -> None:
        self._trade_listeners.append(cb)

    def _emit_trade(self, kind: str, payload: dict) -> None:
        if kind == "exit":
            r = float(payload.get("r_result", 0.0))
            prev = self.session_curve[-1][2] if self.session_curve else 0.0
            self.session_curve.append([float(payload.get("exit_ts", time.time())),
                                       r, round(prev + r, 3)])
            cut = time.time() - 86400.0
            while self.session_curve and self.session_curve[0][0] < cut:
                self.session_curve.pop(0)
        for cb in self._trade_listeners:
            cb(kind, payload)

    def hero(self) -> dict[str, Any]:
        """24h headline numbers for the dashboard's performance card."""
        rs = [p[1] for p in self.session_curve]
        wins = sum(1 for r in rs if r > 0)
        open_n = sum(len(v) for v in self.ledger.open.values())
        return {
            "net_r": round(sum(rs), 2),
            "trades": len(rs),
            "wins": wins,
            "win_rate": round(wins / len(rs) * 100, 1) if rs else None,
            "expectancy": round(sum(rs) / len(rs), 2) if rs else None,
            "open": open_n,
            "signals_total": self.signals_total,
        }

    def _make_signal_hook(self, sym: str):
        comp = self.composites[sym]
        fe = self.features[sym]

        def hook(_st: SymbolState, _bar) -> None:
            events, flags = comp.evaluate(fe.snapshot)
            self.flags[sym] = flags
            for ev in events:
                self._record_signal(ev)

        return hook

    def _record_signal(self, ev: SignalEvent) -> None:
        self.signals_total += 1
        sid = self.db.insert_signal(ev) if self.db is not None else self.signals_total
        self.outcomes.track(sid, ev)
        row = {
            "id": sid, "ts": ev.ts, "symbol": ev.symbol, "side": ev.side,
            "rule": ev.rule, "strength": round(ev.strength, 3), "tier": ev.tier,
            "score": round(ev.score, 1), "snapshot": ev.snapshot,
        }
        self.signal_feed.append(row)
        for cb in self._signal_listeners:
            cb(row)
        # open a tracked trade signal (entry) for directional fires - unless the
        # symbol is quarantined by the auditor (never signal off suspect data)
        if ev.symbol in self.quarantined:
            if ev.side:
                self.thoughts.emit(ev.ts, CAT_TRADE, ev.symbol,
                                   f"{ev.rule} fired {ev.side} but {ev.symbol} is "
                                   "quarantined - no trade", [
                    "this symbol is failing data self-audits; "
                    "no trades open on data the system cannot trust",
                ])
        else:
            ev.snapshot["_db_id"] = sid
            trade = self.ledger.on_rule_fire(ev, self.features[ev.symbol].snapshot)
            if trade is not None:
                self._emit_trade("entry", trade.to_dict())

    def snapshot(self, symbol: str) -> FeatureSnapshot:
        return self.features[symbol].snapshot

    def on_event(self, ev: Event) -> None:
        self.events_total += 1
        if isinstance(ev, Tick):
            self.ticks_total += 1
            self.ingest_lat.append(ev.ts_recv - ev.ts_exch)
            self.tick_monos.append(ev.ts_mono)
            self.last_tick_ts[ev.symbol] = ev.ts_recv
            self.last_any_ts[ev.symbol] = ev.ts_recv
            self.states[ev.symbol].on_event(ev)
            self.outcomes.on_tick(ev)
            for exited in self.ledger.on_tick(ev):
                self._emit_trade("exit", exited.to_dict())
        elif isinstance(ev, (BookTop, Bbo)):
            self.last_any_ts[ev.symbol] = ev.ts_recv
            self.states[ev.symbol].on_event(ev)
        elif isinstance(ev, PerpCtx):
            self.last_any_ts[ev.symbol] = ev.ts_recv
            self.states[ev.symbol].on_event(ev)
            if self.db is not None and ev.ts_recv - self._funding_persist_ts[ev.symbol] >= 60.0:
                self._funding_persist_ts[ev.symbol] = ev.ts_recv
                self.db.insert_funding(ev.symbol, ev.ts_recv, ev.funding_rate)
        elif isinstance(ev, Mids):
            self.last_mids.update(ev.mids)
            self.last_mids_ts = ev.ts_recv
        elif isinstance(ev, FeedStatus):
            self.feed_status[ev.venue] = ev

    def stale_symbols(self, now: float | None = None) -> list[str]:
        now = now if now is not None else time.time()
        cut = self.cfg.symbol_stale_s
        return [s for s, ts in self.last_tick_ts.items() if now - ts > cut]

    def status(self, now: float | None = None) -> dict[str, Any]:
        now = now if now is not None else time.time()
        lat = sorted(self.ingest_lat)
        return {
            "uptime_s": round(now - self.started_at, 1),
            "events_total": self.events_total,
            "ticks_total": self.ticks_total,
            "signals_total": self.signals_total,
            "outcomes_completed": self.outcomes.completed_total,
            "db_write_errors": self.db.write_errors if self.db else 0,
            "ingest_latency_p50_ms": round(percentile(lat, 50) * 1000, 1),
            "ingest_latency_p95_ms": round(percentile(lat, 95) * 1000, 1),
            "stale_symbols": self.stale_symbols(now),
            "feeds": {
                v: {"connected": fs.connected, "detail": fs.detail}
                for v, fs in self.feed_status.items()
            },
        }
