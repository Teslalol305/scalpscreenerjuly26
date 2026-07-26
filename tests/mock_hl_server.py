"""Mock Hyperliquid WS server speaking the verified wire protocol, for tests.

Use: async with MockHLServer(["BTC"]) as srv: connect to srv.url; srv.kill_connections().
Depends on: websockets. Emits the connect banner, subscriptionResponse acks, pong,
and deterministic trades/l2Book/bbo/activeAssetCtx streams for subscribed coins.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import math
import time
from collections import Counter
from typing import Any

import websockets

BANNER = "Websocket connection established."


class MockHLServer:
    def __init__(
        self,
        coins: list[str],
        trade_interval: float = 0.05,
        book_interval: float = 0.2,
        base_price: float = 100.0,
        burst_every_s: float = 0.0,  # >0: periodic one-sided ignition bursts (soak realism)
        burst_len_s: float = 12.0,
    ) -> None:
        self.coins = coins
        self.trade_interval = trade_interval
        self.book_interval = book_interval
        self.base_price = base_price
        self.burst_every = int(burst_every_s / trade_interval) if burst_every_s > 0 else 0
        self.burst_len = int(burst_len_s / trade_interval)
        self._drift: dict[str, float] = dict.fromkeys(coins, 0.0)  # persistent repricing
        self._drift_t = -1  # last virtual tick the drift ramp was applied at
        self.subscribe_frames: Counter[str] = Counter()  # subscription type -> count
        self.ping_count = 0
        self.url = ""
        self._server: Any = None
        self._conns: set[Any] = set()
        self._tid = itertools.count(1)
        self._t = 0  # deterministic virtual clock (ticks of trade_interval)

    async def __aenter__(self) -> MockHLServer:
        self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def kill_connections(self) -> None:
        """Abruptly drop every client (network-failure simulation, no close frame)."""
        for ws in list(self._conns):
            ws.transport.abort()

    def _price(self, coin: str, t: int) -> float:
        # realistic micro-scale wobble (~5bps) + aperiodic jitter: keeps sigma(ROC)
        # small so burst repricings (~40bps) are genuine statistical outliers,
        # the way real ignition prints stand out from quiet tape
        seed = sum(coin.encode())
        base = self.base_price + seed % 10
        wobble = 0.05 * math.sin((t + seed) / 40.0) + 0.02 * ((t * 31 + seed) % 17) / 17.0
        return (base + wobble) * (1.0 + self._drift.get(coin, 0.0))

    def _burst_state(self, t: int) -> tuple[str, str] | None:
        """(coin, side) while a burst is active at virtual time t, else None."""
        if not self.burst_every:
            return None
        phase = t % self.burst_every
        if phase >= self.burst_len:
            return None
        n = t // self.burst_every
        coin = self.coins[n % len(self.coins)]
        side = "B" if n % 2 == 0 else "A"
        if t != self._drift_t:  # apply the drift ramp once per virtual tick
            self._drift_t = t
            step = 0.004 / self.burst_len  # persistent ~0.4% repricing per burst
            self._drift[coin] += step if side == "B" else -step
        return coin, side

    async def _handler(self, ws: Any) -> None:
        self._conns.add(ws)
        subscribed: set[tuple[str, str]] = set()  # (type, coin)
        producer = asyncio.create_task(self._produce(ws, subscribed))
        try:
            await ws.send(BANNER)
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                method = msg.get("method")
                if method == "ping":
                    self.ping_count += 1
                    await ws.send('{"channel":"pong"}')
                elif method == "subscribe":
                    sub = msg.get("subscription", {})
                    self.subscribe_frames[sub.get("type", "?")] += 1
                    subscribed.add((sub.get("type", ""), sub.get("coin", "")))
                    await ws.send(
                        json.dumps(
                            {"channel": "subscriptionResponse",
                             "data": {"method": "subscribe", "subscription": sub}}
                        )
                    )
        except websockets.ConnectionClosed:
            pass
        finally:
            producer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await producer
            self._conns.discard(ws)

    async def _produce(self, ws: Any, subscribed: set[tuple[str, str]]) -> None:
        """Push deterministic market data for whatever this client subscribed to."""
        book_every = max(1, round(self.book_interval / self.trade_interval))
        try:
            while True:
                await asyncio.sleep(self.trade_interval)
                self._t += 1
                t = self._t
                ts_ms = int(time.time() * 1000)  # wall clock: realistic ingest latency
                burst = self._burst_state(t)
                for coin in self.coins:
                    px = self._price(coin, t)
                    bursting = burst is not None and burst[0] == coin
                    if ("trades", coin) in subscribed:
                        # period-97 wobble keeps 10s bucket sums non-identical, so
                        # volume baselines have nonzero sigma (z-scores stay meaningful)
                        wobble = 0.01 * ((t * 7919) % 97) / 97.0
                        if bursting:
                            side = burst[1]
                            sz = 0.6 + (t % 5) * 0.3 + wobble  # ~6x prints, one-sided
                        else:
                            side = "B" if (t + sum(coin.encode())) % 3 else "A"
                            sz = 0.1 + (t % 5) * 0.05 + wobble
                        await ws.send(json.dumps({
                            "channel": "trades",
                            "data": [{
                                "coin": coin, "side": side, "px": f"{px:.4f}",
                                "sz": f"{sz:.2f}", "time": ts_ms,
                                "hash": "0x" + "ab" * 32, "tid": next(self._tid),
                                "users": ["0x" + "1" * 40, "0x" + "2" * 40],
                            }],
                        }))
                    if t % book_every == 0:
                        bid_mult = 3.0 if bursting and burst[1] == "B" else 1.0
                        ask_mult = 3.0 if bursting and burst[1] == "A" else 1.0
                        if ("l2Book", coin) in subscribed:
                            bids = [{"px": f"{px - 0.01 * (i + 1):.4f}",
                                     "sz": f"{(1.0 + i) * bid_mult:.2f}", "n": i + 1}
                                    for i in range(10)]
                            asks = [{"px": f"{px + 0.01 * (i + 1):.4f}",
                                     "sz": f"{(1.0 + i) * ask_mult:.2f}", "n": i + 1}
                                    for i in range(10)]
                            await ws.send(json.dumps({
                                "channel": "l2Book",
                                "data": {"coin": coin, "time": ts_ms, "levels": [bids, asks]},
                            }))
                        if ("bbo", coin) in subscribed:
                            await ws.send(json.dumps({
                                "channel": "bbo",
                                "data": {"coin": coin, "time": ts_ms, "bbo": [
                                    {"px": f"{px - 0.01:.4f}", "sz": "1.00", "n": 1},
                                    {"px": f"{px + 0.01:.4f}", "sz": "1.00", "n": 1}]},
                            }))
                        if ("activeAssetCtx", coin) in subscribed:
                            await ws.send(json.dumps({
                                "channel": "activeAssetCtx",
                                "data": {"coin": coin, "ctx": {
                                    "funding": "0.0000125", "openInterest": f"{1000 + t:.1f}",
                                    "prevDayPx": f"{px:.4f}", "dayNtlVlm": "1000000.0",
                                    "premium": "0.0001", "oraclePx": f"{px:.4f}",
                                    "markPx": f"{px + 0.005:.4f}", "midPx": f"{px:.4f}",
                                    "impactPxs": [f"{px - 0.01:.4f}", f"{px + 0.01:.4f}"],
                                    "dayBaseVlm": "5000.0"}},
                            }))
        except websockets.ConnectionClosed:
            pass
