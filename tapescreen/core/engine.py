"""Engine: consumes canonical events, owns per-symbol state and health metrics.

Use: eng = Engine(cfg); eng.on_event(ev) per event; eng.status() for the UI bar.
Depends on: core.events. (Feature/signal layers attach here in later milestones.)
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from tapescreen.config import Config
from tapescreen.core.events import Bbo, BookTop, Event, FeedStatus, PerpCtx, Tick


def percentile(sorted_vals: list[float], q: float) -> float:
    """Nearest-rank percentile on a pre-sorted list; 0.0 for empty input."""
    if not sorted_vals:
        return 0.0
    idx = max(0, min(len(sorted_vals) - 1, round(q / 100.0 * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


class Engine:
    """Event fan-in: updates per-symbol state, tracks ingest latency and staleness."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.started_at = time.time()
        self.events_total = 0
        self.ticks_total = 0
        # ingest latency = ts_recv - ts_exch on trades (includes venue+network+clock skew)
        self.ingest_lat: deque[float] = deque(maxlen=1000)
        self.last_tick_ts: dict[str, float] = {sym: 0.0 for sym in cfg.symbols}
        self.last_any_ts: dict[str, float] = {sym: 0.0 for sym in cfg.symbols}
        self.feed_status: dict[str, FeedStatus] = {}

    def on_event(self, ev: Event) -> None:
        self.events_total += 1
        if isinstance(ev, Tick):
            self.ticks_total += 1
            self.ingest_lat.append(ev.ts_recv - ev.ts_exch)
            self.last_tick_ts[ev.symbol] = ev.ts_recv
            self.last_any_ts[ev.symbol] = ev.ts_recv
        elif isinstance(ev, (BookTop, Bbo, PerpCtx)):
            self.last_any_ts[ev.symbol] = ev.ts_recv
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
            "ingest_latency_p50_ms": round(percentile(lat, 50) * 1000, 1),
            "ingest_latency_p95_ms": round(percentile(lat, 95) * 1000, 1),
            "stale_symbols": self.stale_symbols(now),
            "feeds": {
                v: {"connected": fs.connected, "detail": fs.detail}
                for v, fs in self.feed_status.items()
            },
        }
