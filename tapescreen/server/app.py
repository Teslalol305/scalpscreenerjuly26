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
    ) -> None:
        self.cfg = cfg
        self.engine = engine
        self.feed = feed
        self.db = db
        self.clients: set[WebSocket] = set()
        self.pipe_lat: deque[float] = deque(maxlen=2000)  # tick->push, seconds (live only)
        self._last_msgs_total = 0
        self._last_rate_ts = time.monotonic()
        self.msg_rate = 0.0
        self.app = self._build_app()
        self._uv: uvicorn.Server | None = None
        engine.add_signal_listener(self._on_signal)
        self._signal_out: asyncio.Queue = asyncio.Queue(maxsize=1000)

    # ------------------------------------------------------------------ fastapi

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="TapeScreen", docs_url=None, redoc_url=None)

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

        @app.get("/health")
        async def health() -> JSONResponse:
            return JSONResponse({
                "ok": True,
                "engine": self.engine.status(),
                "feed": self.feed.health() if self.feed else {"venue": "replay"},
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

    # ------------------------------------------------------------------ payloads

    def _hello(self) -> dict[str, Any]:
        return {
            "type": "hello",
            "symbols": list(self.cfg.symbols),
            "sound_default": self.cfg.sound_default,
            "watch_score": self.cfg.composite.watch_score,
            "alert_score": self.cfg.composite.alert_score,
            "stale_s": self.cfg.symbol_stale_s,
            "recent_signals": list(self.engine.signal_feed)[-100:],
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
        return {"type": "grid", "ts": now, "rows": rows, "status": st}

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
            self._signal_out.put_nowait(row)

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
                # forward any signals that arrived since the last cycle, immediately
                while not self._signal_out.empty():
                    row = self._signal_out.get_nowait()
                    await self._broadcast(json.dumps({"type": "signal", "row": row}))
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
