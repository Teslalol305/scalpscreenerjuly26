"""Replays recorded ndjson frames through the same normalize pipeline at N x speed.

Use: rep = Replayer(path, symbols, out_queue, speed=10); await rep.run().
Depends on: core.normalize, feeds.recorder line format. Deterministic: all event
timestamps come from the recording (ts_mono := recorded ts), so replaying the same
file twice yields identical event streams regardless of replay speed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from tapescreen.config import SymbolEntry
from tapescreen.core.events import FeedStatus
from tapescreen.core.normalize import Normalizer

log = logging.getLogger("tapescreen.replayer")

_CONNECT_BANNER = "Websocket connection established."


class Replayer:
    """Feeds recorded frames into the pipeline, pacing by recorded inter-arrival gaps."""

    def __init__(
        self,
        path: str | Path,
        symbols: dict[str, SymbolEntry],
        out: asyncio.Queue,
        speed: float = 1.0,
    ) -> None:
        self.path = Path(path)
        self.out = out
        self.speed = speed  # 0 = as fast as possible
        coin_to_symbol = {
            s.venue_symbol: s.ui_symbol for s in symbols.values() if s.venue == "hyperliquid"
        }
        self.normalizer = Normalizer(coin_to_symbol)
        self.frames = 0
        self.events = 0

    async def run(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(f"recording not found: {self.path}")
        await self.out.put(
            FeedStatus(ts=0.0, venue="replay", connected=True, detail=f"replaying {self.path.name}")
        )
        prev_ts: float | None = None
        with self.path.open(encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts, raw = float(rec["ts"]), rec["raw"]
                except (ValueError, KeyError, TypeError):
                    log.warning("skipping malformed recording line", extra={"line": lineno})
                    continue
                if prev_ts is not None and self.speed > 0:
                    gap = (ts - prev_ts) / self.speed
                    if gap > 0:
                        await asyncio.sleep(min(gap, 5.0))
                prev_ts = ts
                self.frames += 1
                for ev in self._normalize(raw, ts):
                    self.events += 1
                    await self.out.put(ev)  # blocking put: replay never sheds load
        await self.out.put(
            FeedStatus(ts=prev_ts or 0.0, venue="replay", connected=False, detail="replay finished")
        )

    def _normalize(self, raw: str, ts: float) -> list[Any]:
        if raw == _CONNECT_BANNER:
            return []
        try:
            msg = json.loads(raw)
        except ValueError:
            self.normalizer.dropped += 1
            return []
        if msg.get("channel") == "error":
            return []
        # ts_mono := recorded wall time -> identical latency math on every replay
        return self.normalizer.normalize(msg, ts, ts)
