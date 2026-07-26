"""Hyperliquid WebSocket client: subscribe, keepalive, watchdog, reconnect, resubscribe.

Use: feed = HyperliquidFeed(cfg.feed, symbols, out_queue, recorder); await feed.run().
Depends on: websockets, core.normalize, feeds.recorder. Emits canonical events +
FeedStatus into out_queue; drops (never blocks) when the queue is full.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any

import websockets

from tapescreen.config import FeedConfig, SymbolEntry
from tapescreen.core.events import FeedStatus
from tapescreen.core.normalize import Normalizer
from tapescreen.feeds.recorder import Recorder

log = logging.getLogger("tapescreen.feed.hyperliquid")

_CONNECT_BANNER = "Websocket connection established."


class HyperliquidFeed:
    """One WS connection carrying trades/l2Book/bbo/activeAssetCtx for all symbols."""

    def __init__(
        self,
        cfg: FeedConfig,
        symbols: dict[str, SymbolEntry],
        out: asyncio.Queue,
        recorder: Recorder | None = None,
        debug: bool = False,
        top_levels: int = 10,
    ) -> None:
        self.cfg = cfg
        self.out = out
        self.recorder = recorder
        self.debug = debug
        self.coins = [s.venue_symbol for s in symbols.values() if s.venue == "hyperliquid"]
        coin_to_symbol = {
            s.venue_symbol: s.ui_symbol for s in symbols.values() if s.venue == "hyperliquid"
        }
        self.normalizer = Normalizer(coin_to_symbol, top_levels=top_levels)
        # health counters, read by the status endpoint / UI
        self.connected = False
        self.connected_since = 0.0
        self.reconnects = 0
        self.msgs_total = 0
        self.queue_drops = 0
        self.last_msg_recv = 0.0
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    def subscriptions(self) -> list[dict[str, Any]]:
        subs: list[dict[str, Any]] = [{"type": "allMids"}]
        for coin in self.coins:
            subs.append({"type": "trades", "coin": coin})
            subs.append({"type": "l2Book", "coin": coin})
            subs.append({"type": "bbo", "coin": coin})
            subs.append({"type": "activeAssetCtx", "coin": coin})
        return subs

    async def run(self) -> None:
        """Connect/read forever with exponential backoff; returns only when stopped."""
        backoff = self.cfg.backoff_initial_s
        while not self._stop.is_set():
            session_start = time.monotonic()
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network errors, watchdog timeouts, protocol junk
                self.connected = False
                self._emit_status(False, f"{type(e).__name__}: {e}")
                if self._stop.is_set():
                    break
                if time.monotonic() - session_start >= 30.0:
                    backoff = self.cfg.backoff_initial_s  # session was stable: fresh backoff
                self.reconnects += 1
                delay = backoff * (1.0 + 0.25 * random.random())
                log.warning(
                    "feed disconnected, reconnecting",
                    extra={"error": str(e), "delay_s": round(delay, 2)},
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
                backoff = min(backoff * 2.0, self.cfg.backoff_max_s)

    async def _session(self) -> None:
        """One connection lifetime: subscribe, then read until error/stop."""
        async with websockets.connect(
            self.cfg.ws_url, ping_interval=None, max_queue=4096, open_timeout=10
        ) as ws:
            self.connected = True
            self.connected_since = time.time()
            self.last_msg_recv = time.time()
            self._emit_status(True, "connected")
            for sub in self.subscriptions():
                await ws.send(json.dumps({"method": "subscribe", "subscription": sub}))
            ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                while not self._stop.is_set():
                    try:
                        raw = await asyncio.wait_for(
                            ws.recv(), timeout=self.cfg.watchdog_timeout_s
                        )
                    except TimeoutError:
                        raise ConnectionError(
                            f"watchdog: no message in {self.cfg.watchdog_timeout_s}s"
                        ) from None
                    self._on_raw(raw)
            finally:
                ping_task.cancel()
                self.connected = False

    async def _ping_loop(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(self.cfg.ping_interval_s)
            await ws.send('{"method":"ping"}')

    def _on_raw(self, raw: str | bytes) -> None:
        ts_recv = time.time()
        ts_mono = time.perf_counter()
        self.msgs_total += 1
        self.last_msg_recv = ts_recv
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if self.recorder is not None:
            self.recorder.write(ts_recv, raw)
        if raw == _CONNECT_BANNER:  # first frame after connect is plain text
            return
        try:
            msg = json.loads(raw)
        except ValueError:
            self.normalizer.dropped += 1
            return
        if msg.get("channel") == "error":
            log.warning("feed error frame", extra={"data": str(msg.get("data"))[:300]})
            return
        if self.debug:
            log.debug("ws msg", extra={"channel": msg.get("channel")})
        for ev in self.normalizer.normalize(msg, ts_recv, ts_mono):
            self._put(ev)

    def _put(self, ev: Any) -> None:
        try:
            self.out.put_nowait(ev)
        except asyncio.QueueFull:  # memory-bounded: shed load rather than grow
            self.queue_drops += 1

    def _emit_status(self, connected: bool, detail: str) -> None:
        self._put(FeedStatus(ts=time.time(), venue="hyperliquid", connected=connected, detail=detail))

    def health(self) -> dict[str, Any]:
        return {
            "venue": "hyperliquid",
            "connected": self.connected,
            "connected_since": self.connected_since,
            "reconnects": self.reconnects,
            "msgs_total": self.msgs_total,
            "dropped_msgs": self.normalizer.dropped,
            "queue_drops": self.queue_drops,
            "last_msg_recv": self.last_msg_recv,
        }
