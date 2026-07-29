"""ThoughtLog: the system's live reasoning trace - every decision, explained.

Use: th = ThoughtLog(db); th.emit(ts, CAT_TRADE, "BTC", headline, [detail, ...]).
Depends on: store.db (optional). A thought is one structured record of a decision
the system just made: what it saw (the actual values), which rule or threshold it
applied, and what it did about it. Thoughts sit in a ring buffer for the UI,
reach listeners immediately (the /ws bridge streams them live), and persist to
SQLite so the trail survives restarts. This exists because an adaptive system
that tunes its own weights must be observable: the trace is how the operator
verifies the logic instead of trusting it blindly.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from typing import Any

log = logging.getLogger("tapescreen.thoughts")

CAT_SIGNAL = "signal"    # rule logic: fires, cooldown suppressions, tier grading
CAT_TRADE = "trade"      # trade management: ladders, BE/trail moves, exits
CAT_LEARN = "learning"   # record/weight/model updates from resolved trades
CAT_DESK = "research"    # quant-desk meetings: promotions, probation, exploration
CAT_AUDIT = "audit"      # self-verification results, quarantine decisions
CAT_SYSTEM = "system"    # boot: memory load, model refits, self-test

_PRUNE_EVERY = 2000   # inserts between DB prunes
_PRUNE_KEEP = 20000   # rows kept in the thoughts table


def px(p: float) -> str:
    """Prices formatted the way the UI shows them, for readable detail lines."""
    if p >= 1000:
        return f"{p:,.1f}"
    if p >= 10:
        return f"{p:.2f}"
    if p >= 0.1:
        return f"{p:.4f}"
    return f"{p:.4g}"


class ThoughtLog:
    """Ring buffer + fan-out + persistence for decision-trace events."""

    def __init__(self, db: Any = None, maxlen: int = 400) -> None:
        self.db = db
        self.recent: deque[dict] = deque(maxlen=maxlen)
        self.emitted_total = 0
        self._listeners: list = []
        self._seq = 0
        if db is not None:
            try:
                self._seq = db.next_thought_id() - 1
                for row in db.recent_thoughts(150):
                    self.recent.append(row)
            except Exception:
                log.exception("could not restore thought history; starting fresh")

    def add_listener(self, cb) -> None:
        self._listeners.append(cb)

    def emit(self, ts: float, category: str, symbol: str, headline: str,
             detail: list[str] | tuple[str, ...] = ()) -> dict:
        self._seq += 1
        self.emitted_total += 1
        th = {
            "id": self._seq, "ts": round(ts, 3), "cat": category,
            "symbol": symbol, "headline": headline, "detail": list(detail),
        }
        self.recent.append(th)
        for cb in self._listeners:
            cb(th)
        if self.db is not None:
            self.db.insert_thought(th["id"], th["ts"], category, symbol,
                                   headline, json.dumps(th["detail"]))
            if self.emitted_total % _PRUNE_EVERY == 0:
                self.db.prune_thoughts(self._seq - _PRUNE_KEEP)
        return th
