"""Raw feed capture to ndjson for deterministic replay (one line per WS frame).

Use: rec = Recorder(dir_path, rotate_mb); rec.write(ts_recv, raw); rec.close().
Depends on: stdlib. Writes happen on a dedicated thread (no event-loop blocking);
line format: {"ts": <recv epoch seconds>, "raw": "<original frame text>"}.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from pathlib import Path

log = logging.getLogger("tapescreen.recorder")

_SENTINEL = None


class Recorder:
    """ndjson writer with size-based rotation and a bounded hand-off queue."""

    def __init__(self, dir_path: str | Path, rotate_mb: int = 256, max_pending: int = 100_000) -> None:
        self.dir = Path(dir_path)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.rotate_bytes = rotate_mb * 1024 * 1024
        self._q: queue.Queue = queue.Queue(maxsize=max_pending)
        self.drops = 0
        self._path = self._new_path()
        self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
        self._thread.start()

    @property
    def path(self) -> Path:
        return self._path

    def _new_path(self) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        p = self.dir / f"hl-{stamp}.ndjson"
        i = 1
        while p.exists():
            p = self.dir / f"hl-{stamp}-{i}.ndjson"
            i += 1
        return p

    def write(self, ts_recv: float, raw: str) -> None:
        """Queue one frame; drops (counted) instead of blocking when saturated."""
        try:
            self._q.put_nowait((ts_recv, raw))
        except queue.Full:
            self.drops += 1

    def _run(self) -> None:
        f = self._path.open("a", encoding="utf-8")
        written = f.tell()
        try:
            while True:
                item = self._q.get()
                if item is _SENTINEL:
                    break
                ts, raw = item
                line = json.dumps({"ts": ts, "raw": raw}, separators=(",", ":")) + "\n"
                f.write(line)
                written += len(line)
                if written >= self.rotate_bytes:
                    f.close()
                    self._path = self._new_path()
                    f = self._path.open("a", encoding="utf-8")
                    written = 0
        finally:
            f.flush()
            f.close()

    def close(self) -> None:
        """Flush and stop the writer thread (graceful shutdown path)."""
        self._q.put(_SENTINEL)
        self._thread.join(timeout=10)
        if self._thread.is_alive():  # pragma: no cover - defensive
            log.warning("recorder thread did not stop in time")
