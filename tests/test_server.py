"""UI server tests: endpoints, ws bridge, and live end-to-end latency vs mock feed."""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets
from starlette.testclient import TestClient

from tapescreen.config import load_config
from tapescreen.core.engine import Engine
from tapescreen.feeds.hyperliquid import HyperliquidFeed
from tapescreen.server.app import UiServer
from tapescreen.store.db import Db
from tests.conftest import REPO
from tests.mock_hl_server import MockHLServer
from tests.test_feed_reconnect import feed_cfg


def fresh_cfg():
    return load_config(REPO / "config.yaml")


def test_health_stats_and_ws_hello(tmp_path) -> None:
    cfg = fresh_cfg()
    db = Db(tmp_path / "t.db")
    engine = Engine(cfg, db)
    srv = UiServer(cfg, engine, None, db)
    try:
        with TestClient(srv.app) as client:
            h = client.get("/health").json()
            assert h["ok"] is True and "engine" in h

            s = client.get("/stats").json()
            assert s["haircut_bps"] == cfg.stats.spread_haircut_bps
            assert "by_rule" in s and "by_symbol" in s

            r = client.get("/")
            assert r.status_code == 200 and "TapeScreen" in r.text

            with client.websocket_connect("/ws") as ws:
                hello = ws.receive_json()
                assert hello["type"] == "hello"
                assert set(hello["symbols"]) == set(cfg.symbols)
                ws.send_text(json.dumps({"type": "candles", "symbol": "BTC"}))
                candles = ws.receive_json()
                assert candles["type"] == "candles" and candles["symbol"] == "BTC"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_end_to_end_grid_push_and_latency(tmp_path) -> None:
    """Mock feed -> engine -> ws push; asserts rows populate and tick->UI p95 <= 250ms."""
    cfg = fresh_cfg()
    cfg.port = 0  # ephemeral port for the test server
    async with MockHLServer(["BTC", "ETH"], trade_interval=0.02) as mock:
        fcfg = feed_cfg(mock.url)
        # narrow the symbol set to what the mock serves
        for sym in list(cfg.symbols):
            if sym not in ("BTC", "ETH"):
                del cfg.symbols[sym]
        db = Db(tmp_path / "e2e.db")
        engine = Engine(cfg, db)
        queue: asyncio.Queue = asyncio.Queue(50_000)
        feed = HyperliquidFeed(fcfg, cfg.symbols, queue)
        srv = UiServer(cfg, engine, feed, db)

        async def consume() -> None:
            while True:
                engine.on_event(await queue.get())

        tasks = [asyncio.create_task(feed.run()), asyncio.create_task(consume())]
        tasks += await srv.start()
        try:
            # wait for uvicorn to bind, then discover the ephemeral port
            for _ in range(100):
                await asyncio.sleep(0.05)
                if srv._uv and srv._uv.started:
                    break
            port = srv._uv.servers[0].sockets[0].getsockname()[1]

            async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
                hello = json.loads(await ws.recv())
                assert hello["type"] == "hello"
                grid = None
                async with asyncio.timeout(15):
                    while True:
                        msg = json.loads(await ws.recv())
                        if msg["type"] != "grid":
                            continue
                        row = msg["rows"]["BTC"]
                        if row["price"] > 0 and msg["status"]["pipeline_latency_p95_ms"] > 0:
                            grid = msg
                            break
                row = grid["rows"]["BTC"]
                assert row["spread_bps"] > 0
                assert 0.0 <= row["imb"] <= 1.0
                assert "score_long" in row and "score_short" in row
                p95 = grid["status"]["pipeline_latency_p95_ms"]
                print(f"\ntick->UI pipeline latency p95: {p95}ms (p50 "
                      f"{grid['status']['pipeline_latency_p50_ms']}ms)")
                assert p95 <= 250.0, f"tick->UI p95 {p95}ms exceeds 250ms budget"
        finally:
            feed.stop()
            srv.request_stop()
            await asyncio.sleep(0.1)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            db.close()


@pytest.mark.asyncio
async def test_stale_symbol_flag(tmp_path) -> None:
    """Symbols with no ticks (or none at all) must be marked stale in the grid payload."""
    cfg = fresh_cfg()
    db = Db(tmp_path / "s.db")
    engine = Engine(cfg, db)
    srv = UiServer(cfg, engine, None, db)
    try:
        grid = srv._grid()
        assert all(r["stale"] for r in grid["rows"].values()), "no ticks yet -> all stale"
    finally:
        db.close()
