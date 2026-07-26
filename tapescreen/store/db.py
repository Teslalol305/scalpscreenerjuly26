"""SQLite (WAL) persistence: signals, outcomes, funding history, session stats.

Use: db = Db(path); id = db.insert_signal(ev); db.upsert_outcome(...); db.close().
Depends on: stdlib sqlite3. All writes flow through one writer thread (the event
loop never blocks); reads open short-lived read-only connections (WAL-safe).
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
from pathlib import Path
from typing import Any

from tapescreen.core.signals.base import SignalEvent

log = logging.getLogger("tapescreen.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id        INTEGER PRIMARY KEY,
    ts        REAL NOT NULL,
    symbol    TEXT NOT NULL,
    side      TEXT NOT NULL,
    rule      TEXT NOT NULL,
    strength  REAL NOT NULL,
    tier      TEXT NOT NULL,
    score     REAL NOT NULL,
    price     REAL NOT NULL,
    snapshot  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);
CREATE TABLE IF NOT EXISTS outcomes (
    signal_id    INTEGER PRIMARY KEY REFERENCES signals(id),
    ret_30s      REAL,
    ret_1m       REAL,
    ret_3m       REAL,
    ret_5m       REAL,
    mfe_5m       REAL,
    mae_5m       REAL,
    completed_at REAL
);
CREATE TABLE IF NOT EXISTS funding_history (
    symbol TEXT NOT NULL,
    ts     REAL NOT NULL,
    rate   REAL NOT NULL,
    PRIMARY KEY (symbol, ts)
);
"""

_SENTINEL: Any = object()


class Db:
    """Single-writer SQLite wrapper; safe to call from the event loop."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM signals").fetchone()
        self._next_id = int(row[0]) + 1
        self._id_lock = threading.Lock()
        self._q: queue.Queue = queue.Queue(maxsize=50_000)
        self.write_errors = 0
        self._thread = threading.Thread(target=self._run, name="db-writer", daemon=True)
        self._thread.start()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # ------------------------------------------------------------------ writes

    def insert_signal(self, ev: SignalEvent) -> int:
        with self._id_lock:
            sid = self._next_id
            self._next_id += 1
        price = float(ev.snapshot.get("price", 0.0))
        self._submit(
            "INSERT INTO signals (id, ts, symbol, side, rule, strength, tier, score, price, snapshot)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sid, ev.ts, ev.symbol, ev.side, ev.rule, ev.strength, ev.tier, ev.score,
             price, json.dumps(ev.snapshot, separators=(",", ":"))),
        )
        return sid

    def upsert_outcome(self, signal_id: int, fields: dict[str, float | None]) -> None:
        cols = ("ret_30s", "ret_1m", "ret_3m", "ret_5m", "mfe_5m", "mae_5m", "completed_at")
        vals = [fields.get(c) for c in cols]
        sets = ", ".join(f"{c}=COALESCE(excluded.{c}, outcomes.{c})" for c in cols)
        self._submit(
            f"INSERT INTO outcomes (signal_id, {', '.join(cols)}) VALUES (?,?,?,?,?,?,?,?)"
            f" ON CONFLICT(signal_id) DO UPDATE SET {sets}",
            (signal_id, *vals),
        )

    def insert_funding(self, symbol: str, ts: float, rate: float) -> None:
        self._submit(
            "INSERT OR IGNORE INTO funding_history (symbol, ts, rate) VALUES (?,?,?)",
            (symbol, ts, rate),
        )

    def _submit(self, sql: str, params: tuple) -> None:
        try:
            self._q.put_nowait((sql, params))
        except queue.Full:
            self.write_errors += 1

    def _run(self) -> None:
        conn = self._connect()
        try:
            while True:
                item = self._q.get()
                if item is _SENTINEL:
                    break
                sql, params = item
                try:
                    conn.execute(sql, params)
                    conn.commit()
                except sqlite3.Error:
                    self.write_errors += 1
                    log.exception("db write failed")
        finally:
            conn.commit()
            conn.close()

    def close(self) -> None:
        """Flush queued writes and stop the writer thread."""
        self._q.put(_SENTINEL)
        self._thread.join(timeout=15)

    # ------------------------------------------------------------------ reads

    def read_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def recent_signals(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.read_conn() as conn:
            rows = conn.execute(
                "SELECT s.*, o.ret_30s, o.ret_1m, o.ret_3m, o.ret_5m, o.mfe_5m, o.mae_5m"
                " FROM signals s LEFT JOIN outcomes o ON o.signal_id = s.id"
                " ORDER BY s.ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["snapshot"] = json.loads(d["snapshot"])
            out.append(d)
        return out

    def load_funding(self, symbol: str, since_ts: float) -> list[tuple[float, float]]:
        with self.read_conn() as conn:
            rows = conn.execute(
                "SELECT ts, rate FROM funding_history WHERE symbol=? AND ts>=? ORDER BY ts",
                (symbol, since_ts),
            ).fetchall()
        return [(r["ts"], r["rate"]) for r in rows]

    def stats_summary(self, haircut_bps: float) -> dict[str, Any]:
        """Per-rule and per-symbol counts, hit-rates per horizon (net of haircut), MFE/MAE."""
        h = haircut_bps / 1e4
        horizons = ("ret_30s", "ret_1m", "ret_3m", "ret_5m")

        def bucket(group_col: str) -> list[dict[str, Any]]:
            hits = ", ".join(
                f"AVG(CASE WHEN o.{c} IS NOT NULL THEN (o.{c} > {h}) END) AS hit_{c},"
                f" SUM(o.{c} IS NOT NULL) AS n_{c}" for c in horizons
            )
            sql = (
                f"SELECT s.{group_col} AS grp, COUNT(*) AS signals,"
                f" SUM(s.tier='ALERT') AS alerts, {hits},"
                f" AVG(o.mfe_5m) AS avg_mfe, AVG(o.mae_5m) AS avg_mae"
                f" FROM signals s LEFT JOIN outcomes o ON o.signal_id = s.id"
                f" GROUP BY s.{group_col} ORDER BY signals DESC"
            )
            with self.read_conn() as conn:
                return [dict(r) for r in conn.execute(sql).fetchall()]

        with self.read_conn() as conn:
            totals = dict(conn.execute(
                "SELECT COUNT(*) AS signals, SUM(tier='ALERT') AS alerts,"
                " (SELECT COUNT(*) FROM outcomes WHERE completed_at IS NOT NULL) AS completed"
                " FROM signals").fetchone())
        return {
            "haircut_bps": haircut_bps,
            "totals": totals,
            "by_rule": bucket("rule"),
            "by_symbol": bucket("symbol"),
        }
