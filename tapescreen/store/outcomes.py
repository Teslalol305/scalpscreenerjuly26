"""Forward-return / MFE / MAE computation for every logged signal, as data streams in.

Use: ot = OutcomeTracker(cfg, db); ot.track(sid, ev); ot.on_tick(tick) per trade.
Depends on: store.db, core.events. Returns are signed by side (long: p/p0-1,
short: p0 side-flipped), horizons filled at the first trade at/after each horizon,
MFE/MAE over the mfe_mae_window_s window; the row completes when the window ends.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tapescreen.config import Config
from tapescreen.core.events import Tick
from tapescreen.core.signals.base import LONG, SHORT, SignalEvent
from tapescreen.store.db import Db

_HORIZON_COL = {30: "ret_30s", 60: "ret_1m", 180: "ret_3m", 300: "ret_5m"}


@dataclass(slots=True)
class OpenSignal:
    signal_id: int
    ts: float
    side_sign: float  # +1 long, -1 short
    entry: float
    pending: list[int]  # horizons (seconds) not yet filled
    mfe: float = 0.0
    mae: float = 0.0
    filled: dict[str, float] = field(default_factory=dict)


class OutcomeTracker:
    """Streams ticks into per-signal forward outcomes; persists incrementally."""

    def __init__(self, cfg: Config, db: Db | None) -> None:
        self.cfg = cfg
        self.db = db
        self.window_s = float(cfg.stats.mfe_mae_window_s)
        self.horizons = sorted(cfg.stats.horizons_s)
        self.open: dict[str, list[OpenSignal]] = {}
        self.completed_total = 0

    def track(self, signal_id: int, ev: SignalEvent) -> None:
        entry = float(ev.snapshot.get("price", 0.0))
        if entry <= 0 or ev.side not in (LONG, SHORT):
            return
        sig = OpenSignal(
            signal_id=signal_id,
            ts=ev.ts,
            side_sign=1.0 if ev.side == LONG else -1.0,
            entry=entry,
            pending=list(self.horizons),
        )
        self.open.setdefault(ev.symbol, []).append(sig)

    def on_tick(self, t: Tick) -> None:
        sigs = self.open.get(t.symbol)
        if not sigs:
            return
        done: list[OpenSignal] = []
        for sig in sigs:
            elapsed = t.ts_recv - sig.ts
            if elapsed < 0:
                continue
            ret = sig.side_sign * (t.price / sig.entry - 1.0)
            if elapsed <= self.window_s:
                sig.mfe = max(sig.mfe, ret)
                sig.mae = min(sig.mae, ret)
            newly: dict[str, float] = {}
            while sig.pending and elapsed >= sig.pending[0]:
                h = sig.pending.pop(0)
                col = _HORIZON_COL.get(h, f"ret_{h}s")
                sig.filled[col] = ret
                newly[col] = ret
            if newly and self.db is not None:
                self.db.upsert_outcome(sig.signal_id, dict(newly))
            if not sig.pending and elapsed >= self.window_s:
                done.append(sig)
        for sig in done:
            sigs.remove(sig)
            self.completed_total += 1
            if self.db is not None:
                self.db.upsert_outcome(
                    sig.signal_id,
                    {"mfe_5m": sig.mfe, "mae_5m": sig.mae,
                     "completed_at": sig.ts + self.window_s, **sig.filled},
                )
