"""FastAPI server: /ws browser bridge (batched pushes), /stats, /health, static UI.

Use: srv = UiServer(cfg, engine, feed, db); task = await srv.start(); srv.request_stop().
Depends on: fastapi/uvicorn, core.engine, store.db. Pushes a full grid snapshot
every ui.push_interval_ms plus immediate signal events; measures tick->push
latency on the monotonic clock (live feed only, meaningless in replay).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from tapescreen.config import Config
from tapescreen.core.engine import Engine, percentile
from tapescreen.core.thoughts import CAT_DESK
from tapescreen.core.validation import validation_report
from tapescreen.feeds.hyperliquid import HyperliquidFeed
from tapescreen.store.db import Db

log = logging.getLogger("tapescreen.server")

STATIC_DIR = Path(__file__).parent / "static"


class _QuietServer(uvicorn.Server):
    def install_signal_handlers(self) -> None:  # supervisor owns signal handling
        pass


class UiServer:
    def __init__(
        self,
        cfg: Config,
        engine: Engine,
        feed: HyperliquidFeed | None,
        db: Db | None,
        auditor: Any = None,
    ) -> None:
        self.cfg = cfg
        self.engine = engine
        self.feed = feed
        self.db = db
        self.auditor = auditor
        self.clients: set[WebSocket] = set()
        self._val: dict[str, Any] | None = None  # cached validation report
        self._val_ts = 0.0
        self._val_statuses: dict[str, str] = {}
        self.pipe_lat: deque[float] = deque(maxlen=2000)  # tick->push, seconds (live only)
        self._last_msgs_total = 0
        self._last_rate_ts = time.monotonic()
        self.msg_rate = 0.0
        self.app = self._build_app()
        self._uv: uvicorn.Server | None = None
        engine.add_signal_listener(self._on_signal)
        engine.add_trade_listener(self._on_trade)
        engine.thoughts.add_listener(self._on_thought)
        self._signal_out: asyncio.Queue = asyncio.Queue(maxsize=1000)

    # ------------------------------------------------------------------ fastapi

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="TapeScreen", docs_url=None, redoc_url=None)

        @app.middleware("http")
        async def no_stale_ui(request, call_next):
            # revalidate every UI asset on each load (304 when unchanged) so a
            # stale browser cache can never show an old dashboard after upgrades
            resp = await call_next(request)
            resp.headers["Cache-Control"] = "no-cache"
            return resp

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

        @app.get("/health")
        async def health() -> JSONResponse:
            report = self.auditor.last_report if self.auditor else None
            return JSONResponse({
                "ok": True,
                "engine": self.engine.status(),
                "feed": self.feed.health() if self.feed else {"venue": "replay"},
                "audit": report.to_dict() if report else {"ok": None, "detail": "not run yet"},
            })

        @app.get("/stats")
        async def stats() -> JSONResponse:
            if self.db is None:
                return JSONResponse({"error": "no database in this mode"}, status_code=503)
            data = await asyncio.to_thread(
                self.db.stats_summary, self.cfg.stats.spread_haircut_bps
            )
            return JSONResponse(data)

        @app.websocket("/ws")
        async def ws_endpoint(ws: WebSocket) -> None:
            await ws.accept()
            self.clients.add(ws)
            try:
                await ws.send_text(json.dumps(self._hello()))
                while True:
                    raw = await ws.receive_text()
                    with contextlib.suppress(ValueError):
                        req = json.loads(raw)
                        if req.get("type") == "candles":
                            await ws.send_text(json.dumps(self._candles(req.get("symbol", ""))))
            except WebSocketDisconnect:
                pass
            finally:
                self.clients.discard(ws)

        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
        return app

    # ------------------------------------------------------------------ validation

    def _probation(self) -> set[str]:
        r = self.engine.research
        return set(r.probation) if r is not None else set()

    def _compute_validation(self, now: float) -> None:
        """Sync refresh (used lazily by hello); push_loop threads the DB read."""
        if self.db is None:
            return
        report = validation_report(self.db, self.cfg, self._probation(), now)
        self._apply_validation(report, now)

    def _apply_validation(self, report: dict[str, Any], now: float) -> None:
        """Cache the report; narrate status transitions (rare). Loop-thread only."""
        self._val_ts = now
        first = not self._val_statuses
        for r in report["rules"]:
            old = self._val_statuses.get(r["rule"])
            self._val_statuses[r["rule"]] = r["status"]
            if first or old == r["status"] or old is None:
                continue
            if r["status"] == "validated":
                self.engine.thoughts.emit(now, CAT_DESK, "",
                    f"validation tracker: {r['rule']} is VALIDATED", [
                    f"{r['n']} resolved trades over {r['days']} active days; "
                    f"expectancy {r['mean_r']:+.2f}R with 95% CI low {r['ci_lo']:+.2f}R > 0, "
                    "net of spread + fees",
                    "eligible for real-trading consideration - sizing discipline still applies",
                ])
            elif r["status"] == "rejected":
                self.engine.thoughts.emit(now, CAT_DESK, "",
                    f"validation tracker: {r['rule']} is REJECTED", [
                    f"{r['n']} trades over {r['days']} days; 95% CI high "
                    f"{r['ci_hi']:+.2f}R < 0 - the edge is confidently negative",
                    "a firm no is a result: this verdict saves real money",
                ])
        self._val = report

    def _validation(self) -> dict[str, Any] | None:
        if self._val is None and self.db is not None:
            try:
                self._compute_validation(time.time())
            except Exception:
                log.exception("validation report failed")
        return self._val

    # ------------------------------------------------------------------ payloads

    def _hello(self) -> dict[str, Any]:
        from tapescreen import __version__

        return {
            "type": "hello",
            "version": __version__,
            "max_hold_s": self.cfg.learning.max_hold_s,
            "symbols": list(self.cfg.symbols),
            "sound_default": self.cfg.sound_default,
            "watch_score": self.cfg.composite.watch_score,
            "alert_score": self.cfg.composite.alert_score,
            "stale_s": self.cfg.symbol_stale_s,
            "recent_signals": list(self.engine.signal_feed)[-100:],
            "thoughts": list(self.engine.thoughts.recent)[-150:],
            "board": {
                "active": self.engine.ledger.active(),
                "resolved": self.engine.ledger.resolved_recent[-20:][::-1],
                "learning": self.engine.ledger.learning_snapshot(),
            },
            "desk": self.engine.research.snapshot() if self.engine.research else None,
            "curve": self.engine.session_curve[-500:],
            "hero": self.engine.hero(),
            "validation": self._validation(),
        }

    def _row(self, sym: str, now: float) -> dict[str, Any]:
        s = self.engine.snapshot(sym)
        flags = self.engine.flags.get(sym, {})
        last_tick = self.engine.last_tick_ts.get(sym, 0.0)
        return {
            "price": s.price,
            "pct_1m": round(s.roc_60s * 100, 3),
            "pct_5m": round(s.roc_5m * 100, 3),
            "pct_15m": round(s.roc_15m * 100, 3),
            "vol_z": round(s.vol_z, 2),
            "cvd_5m": round(s.cvd_5m, 4),
            "imb": round(s.book_imbalance, 3),
            "spread_bps": round(s.spread_bps, 2),
            "funding": s.funding,
            "funding_pctl": round(s.funding_pctl_7d, 1),
            "doi_5m": round(s.doi_5m, 4),
            "score_long": flags.get("score_long", 0.0),
            "score_short": flags.get("score_short", 0.0),
            "oi_compression": flags.get("oi_compression", False),
            "funding_bias": flags.get("funding_bias", ""),
            "warming": s.warming,
            "unavailable": sym in self.engine.unavailable,
            "quarantined": sym in self.engine.quarantined,
            "stale": bool(last_tick and now - last_tick > self.cfg.symbol_stale_s)
            or last_tick == 0.0,
        }

    def _grid(self) -> dict[str, Any]:
        now = time.time()
        rows = {sym: self._row(sym, now) for sym in self.cfg.symbols}
        st = self.engine.status(now)
        feed_health = self.feed.health() if self.feed else {}
        mono = time.monotonic()
        dt = mono - self._last_rate_ts
        total = feed_health.get("msgs_total", self.engine.events_total)
        if dt >= 1.0:
            self.msg_rate = (total - self._last_msgs_total) / dt
            self._last_msgs_total = total
            self._last_rate_ts = mono
        lat = sorted(self.pipe_lat)
        st["pipeline_latency_p50_ms"] = round(percentile(lat, 50) * 1000, 1)
        st["pipeline_latency_p95_ms"] = round(percentile(lat, 95) * 1000, 1)
        st["msg_rate"] = round(self.msg_rate, 1)
        st["feed_health"] = feed_health
        rep = self.auditor.last_report if self.auditor else None
        st["audit"] = ({"ok": rep.ok, "failures": rep.failures[:8],
                        "quarantined": rep.quarantined, "runs": rep.runs_total}
                       if rep else None)
        led = self.engine.ledger
        board = {
            "active": led.active(),
            "resolved": led.resolved_recent[-20:][::-1],
            "learning": led.learning_snapshot(),
        }
        return {"type": "grid", "ts": now, "rows": rows, "status": st, "board": board,
                "desk": self.engine.research.snapshot() if self.engine.research else None,
                "curve": self.engine.session_curve[-500:],
                "hero": self.engine.hero(),
                "validation": self._val}

    def _candles(self, sym: str) -> dict[str, Any]:
        if sym not in self.engine.states:
            return {"type": "candles", "symbol": sym, "bars_1m": [], "bars_1s": []}
        st = self.engine.states[sym]
        snap = self.engine.snapshot(sym)

        def dump(bars: list, cur) -> list[list[float]]:
            out = [[b.ts, b.open, b.high, b.low, b.close, b.volume, round(b.delta, 4)]
                   for b in bars]
            if cur is not None:
                out.append([cur.ts, cur.open, cur.high, cur.low, cur.close,
                            cur.volume, round(cur.delta, 4)])
            return out

        sigs = [r for r in self.engine.signal_feed if r["symbol"] == sym][-60:]
        return {
            "type": "candles",
            "symbol": sym,
            "bars_1m": dump(st.bars_1m.last(240), st.cur_1m),
            "bars_1s": dump(st.bars_1s.last(600), st.cur_1s),
            "vwap": {
                "session": snap.vwap_session,
                "sd": snap.vwap_session_sd,
                "vwap_30m": snap.vwap_30m,
            },
            "signals": [{k: v for k, v in r.items() if k != "snapshot"} for r in sigs],
        }

    # ------------------------------------------------------------------ push loop

    def _on_signal(self, row: dict[str, Any]) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            self._signal_out.put_nowait({"type": "signal", "row": row})

    def _on_trade(self, kind: str, payload: dict[str, Any]) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            self._signal_out.put_nowait({"type": kind, "trade": payload})

    def _on_thought(self, th: dict[str, Any]) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            self._signal_out.put_nowait({"type": "thought", "thought": th})

    async def _broadcast(self, text: str) -> None:
        dead = []
        for ws in list(self.clients):  # snapshot: the set mutates across awaits
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def push_loop(self) -> None:
        interval = self.cfg.push_interval_ms / 1000.0
        live = self.feed is not None
        while True:
            await asyncio.sleep(interval)
            try:
                if self.db is not None and time.time() - self._val_ts >= 60.0:
                    now = time.time()
                    try:
                        report = await asyncio.to_thread(
                            validation_report, self.db, self.cfg, self._probation(), now)
                        self._apply_validation(report, now)
                    except Exception:
                        log.exception("validation refresh failed")
                # forward signals/entries/exits that arrived since last cycle, immediately
                while not self._signal_out.empty():
                    msg = self._signal_out.get_nowait()
                    await self._broadcast(json.dumps(msg))
                if live:
                    now_mono = time.perf_counter()
                    monos = self.engine.tick_monos
                    while monos:
                        self.pipe_lat.append(max(0.0, now_mono - monos.popleft()))
                if self.clients:
                    await self._broadcast(json.dumps(self._grid()))
            except Exception:  # one bad cycle must not kill the UI forever
                log.exception("push cycle failed")

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> list[asyncio.Task]:
        config = uvicorn.Config(
            self.app, host="127.0.0.1", port=self.cfg.port, log_level="warning",
            access_log=False,
        )
        self._uv = _QuietServer(config)
        serve_task = asyncio.create_task(self._uv.serve(), name="uvicorn")
        push_task = asyncio.create_task(self.push_loop(), name="ui-push")
        log.info("dashboard at http://localhost:%d", self.cfg.port)
        return [serve_task, push_task]

    def request_stop(self) -> None:
        if self._uv is not None:
            self._uv.should_exit = True
