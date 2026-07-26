"""Recorder/replayer determinism: same recording -> identical event streams."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tapescreen.config import SymbolEntry
from tapescreen.core.events import Event, Tick
from tapescreen.feeds.hyperliquid import HyperliquidFeed
from tapescreen.feeds.recorder import Recorder
from tapescreen.feeds.replayer import Replayer
from tests.mock_hl_server import MockHLServer
from tests.test_feed_reconnect import feed_cfg

SYMBOLS = {"BTC": SymbolEntry("BTC", "hyperliquid", "BTC", "test")}


def make_recording(tmp_path: Path) -> Path:
    """Hand-built ndjson recording covering every channel."""
    frames = [
        (100.0, "Websocket connection established."),
        (100.1, json.dumps({"channel": "subscriptionResponse",
                            "data": {"method": "subscribe",
                                     "subscription": {"type": "trades", "coin": "BTC"}}})),
        (100.2, json.dumps({"channel": "trades", "data": [
            {"coin": "BTC", "side": "B", "px": "100.5", "sz": "0.3", "time": 100200,
             "hash": "0x1", "tid": 1, "users": ["0xa", "0xb"]}]})),
        (100.3, json.dumps({"channel": "l2Book", "data": {
            "coin": "BTC", "time": 100300,
            "levels": [[{"px": "100.4", "sz": "2.0", "n": 1}],
                       [{"px": "100.6", "sz": "1.0", "n": 1}]]}})),
        (100.4, json.dumps({"channel": "bbo", "data": {
            "coin": "BTC", "time": 100400,
            "bbo": [{"px": "100.4", "sz": "2.0", "n": 1}, {"px": "100.6", "sz": "1.0", "n": 1}]}})),
        (100.5, json.dumps({"channel": "activeAssetCtx", "data": {
            "coin": "BTC", "ctx": {"funding": "0.0001", "openInterest": "5000",
                                   "prevDayPx": "99", "dayNtlVlm": "1", "premium": "0",
                                   "oraclePx": "100.5", "markPx": "100.55", "midPx": "100.5",
                                   "impactPxs": None, "dayBaseVlm": "1"}}})),
        (100.6, json.dumps({"channel": "pong"})),
        (100.7, json.dumps({"channel": "trades", "data": [
            {"coin": "BTC", "side": "A", "px": "100.4", "sz": "1.1", "time": 100700,
             "hash": "0x2", "tid": 2, "users": ["0xa", "0xb"]}]})),
    ]
    path = tmp_path / "rec.ndjson"
    path.write_text("".join(
        json.dumps({"ts": ts, "raw": raw}, separators=(",", ":")) + "\n" for ts, raw in frames))
    return path


async def replay_all(path: Path, speed: float) -> list[Event]:
    queue: asyncio.Queue = asyncio.Queue()
    rep = Replayer(path, SYMBOLS, queue, speed=speed)
    await rep.run()
    out: list[Event] = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


@pytest.mark.asyncio
async def test_replaying_twice_yields_identical_streams(tmp_path: Path) -> None:
    path = make_recording(tmp_path)
    a = await replay_all(path, speed=0)
    b = await replay_all(path, speed=0)
    assert a == b  # slots dataclasses compare field-wise
    # 2 ticks + book + bbo + ctx + 2 FeedStatus bookends
    assert len(a) == 7
    ticks = [e for e in a if isinstance(e, Tick)]
    assert [t.trade_id for t in ticks] == [1, 2]
    assert ticks[0].ts_recv == 100.2  # timestamps come from the recording


@pytest.mark.asyncio
async def test_replay_speed_does_not_change_events(tmp_path: Path) -> None:
    path = make_recording(tmp_path)
    fast = await replay_all(path, speed=0)
    paced = await replay_all(path, speed=50.0)
    assert fast == paced


@pytest.mark.asyncio
async def test_live_capture_replays_identically() -> None:
    """Record from the mock server, then replay: tick payloads must match exactly."""
    import tempfile

    async with MockHLServer(["BTC"], trade_interval=0.01) as srv:
        with tempfile.TemporaryDirectory() as td:
            recorder = Recorder(td, rotate_mb=64)
            queue: asyncio.Queue = asyncio.Queue(10_000)
            feed = HyperliquidFeed(feed_cfg(srv.url), SYMBOLS, queue, recorder)
            task = asyncio.create_task(feed.run())
            live_ticks: list[Tick] = []
            async with asyncio.timeout(10):
                while len(live_ticks) < 20:
                    ev = await queue.get()
                    if isinstance(ev, Tick):
                        live_ticks.append(ev)
            feed.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            recorder.close()

            replayed = await replay_all(recorder.path, speed=0)
            rep_ticks = [e for e in replayed if isinstance(e, Tick)][: len(live_ticks)]
            live_key = [(t.symbol, t.price, t.size, t.side, t.trade_id, t.ts_exch) for t in live_ticks]
            rep_key = [(t.symbol, t.price, t.size, t.side, t.trade_id, t.ts_exch) for t in rep_ticks]
            assert live_key == rep_key
