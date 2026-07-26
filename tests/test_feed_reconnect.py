"""Kill-and-recover: network drop -> auto-reconnect + resubscribe, no crash."""

from __future__ import annotations

import asyncio

import pytest

from tapescreen.config import FeedConfig, SymbolEntry
from tapescreen.core.events import Tick
from tapescreen.feeds.hyperliquid import HyperliquidFeed
from tests.mock_hl_server import MockHLServer


def feed_cfg(url: str) -> FeedConfig:
    return FeedConfig(
        ws_url=url,
        info_url="http://127.0.0.1:1/info",
        ping_interval_s=0.5,
        watchdog_timeout_s=2.0,
        backoff_initial_s=0.1,
        backoff_max_s=1.0,
        warmup_candles=False,
        warmup_candle_bars=1,
        warmup_funding_history=False,
    )


async def collect_ticks(queue: asyncio.Queue, n: int, within_s: float = 10.0) -> list[Tick]:
    ticks: list[Tick] = []
    async with asyncio.timeout(within_s):
        while len(ticks) < n:
            ev = await queue.get()
            if isinstance(ev, Tick):
                ticks.append(ev)
    return ticks


@pytest.mark.asyncio
async def test_reconnect_and_resubscribe_after_kill() -> None:
    symbols = {"BTC": SymbolEntry("BTC", "hyperliquid", "BTC", "test")}
    async with MockHLServer(["BTC"], trade_interval=0.02) as srv:
        queue: asyncio.Queue = asyncio.Queue(10_000)
        feed = HyperliquidFeed(feed_cfg(srv.url), symbols, queue)
        task = asyncio.create_task(feed.run())
        try:
            before = await collect_ticks(queue, 5)
            assert len(before) == 5
            subs_before = dict(srv.subscribe_frames)
            assert subs_before["trades"] == 1 and subs_before["l2Book"] == 1

            await srv.kill_connections()

            # feed must reconnect on its own and data must flow again
            after = await collect_ticks(queue, 5)
            assert len(after) == 5
            assert feed.reconnects >= 1
            assert srv.subscribe_frames["trades"] >= 2, "must resubscribe after reconnect"
            assert srv.subscribe_frames["activeAssetCtx"] >= 2
            assert not task.done(), "feed supervisor must survive the drop"
        finally:
            feed.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_watchdog_triggers_reconnect_on_silent_server() -> None:
    """A server that goes silent (but keeps the socket open) must trip the watchdog."""
    symbols = {"BTC": SymbolEntry("BTC", "hyperliquid", "BTC", "test")}
    async with MockHLServer([], trade_interval=0.02) as srv:  # no coins -> no data ever
        queue: asyncio.Queue = asyncio.Queue(1000)
        cfg = feed_cfg(srv.url)
        cfg.watchdog_timeout_s = 0.3
        cfg.ping_interval_s = 60.0  # ping silenced so nothing resets the server side
        feed = HyperliquidFeed(cfg, symbols, queue)
        task = asyncio.create_task(feed.run())
        try:
            await asyncio.sleep(1.5)
            assert feed.reconnects >= 1, "watchdog must force reconnects on silence"
            assert not task.done()
        finally:
            feed.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_ping_is_sent_and_pong_ignored() -> None:
    symbols = {"BTC": SymbolEntry("BTC", "hyperliquid", "BTC", "test")}
    async with MockHLServer(["BTC"], trade_interval=0.02) as srv:
        queue: asyncio.Queue = asyncio.Queue(10_000)
        cfg = feed_cfg(srv.url)
        cfg.ping_interval_s = 0.2
        feed = HyperliquidFeed(cfg, symbols, queue)
        task = asyncio.create_task(feed.run())
        try:
            await collect_ticks(queue, 3)
            await asyncio.sleep(0.5)
            assert srv.ping_count >= 2
        finally:
            feed.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
