"""Wiring + supervisor: config -> feed/replayer -> engine, with graceful shutdown.

Use: `python -m tapescreen [--record N] [--replay FILE --speed X]`.
Depends on: config, logs, feeds.*, core.engine. Feeds auto-restart with backoff
inside HyperliquidFeed.run(); this module owns task lifecycle and shutdown flush.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import time

from tapescreen.config import Config, load_config
from tapescreen.core.engine import Engine
from tapescreen.feeds.hyperliquid import HyperliquidFeed
from tapescreen.feeds.recorder import Recorder
from tapescreen.feeds.replayer import Replayer
from tapescreen.logs import setup_logging

log = logging.getLogger("tapescreen.main")

QUEUE_CAP = 50_000  # hard cap on in-flight events (documented memory bound)


async def _consume(queue: asyncio.Queue, engine: Engine) -> None:
    while True:
        ev = await queue.get()
        engine.on_event(ev)
        queue.task_done()


async def run(cfg: Config, args: argparse.Namespace) -> int:
    queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_CAP)
    engine = Engine(cfg)
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # pragma: no cover - non-unix
            loop.add_signal_handler(sig, stop.set)

    tasks: list[asyncio.Task] = [asyncio.create_task(_consume(queue, engine), name="consume")]
    recorder: Recorder | None = None
    feed: HyperliquidFeed | None = None

    if args.replay:
        replayer = Replayer(args.replay, cfg.symbols, queue, speed=args.speed)

        async def _replay_then_stop() -> None:
            try:
                await replayer.run()
                await queue.join()  # let the consumer drain before shutdown
            finally:
                stop.set()

        tasks.append(asyncio.create_task(_replay_then_stop(), name="replayer"))
    else:
        if cfg.recorder.enabled or args.record:
            recorder = Recorder(cfg.recorder.path, cfg.recorder.rotate_mb)
            log.info("recording to %s", recorder.path)
        feed = HyperliquidFeed(cfg.feed, cfg.symbols, queue, recorder, debug=cfg.feed_debug)
        tasks.append(asyncio.create_task(feed.run(), name="feed"))

    if args.record:
        async def _timer() -> None:
            await asyncio.sleep(args.record)
            log.info("record window of %ss elapsed", args.record)
            stop.set()

        tasks.append(asyncio.create_task(_timer(), name="record-timer"))

    status_task = asyncio.create_task(_status_loop(engine, feed), name="status")

    await stop.wait()
    log.info("shutting down")
    if feed is not None:
        feed.stop()
    for t in tasks:
        t.cancel()
    status_task.cancel()
    await asyncio.gather(*tasks, status_task, return_exceptions=True)
    if recorder is not None:
        recorder.close()
        log.info("recording flushed: %s", recorder.path)
    st = engine.status()
    log.info(
        "session summary: %s events (%s ticks) in %.1fs",
        st["events_total"], st["ticks_total"], st["uptime_s"],
        extra={"events": st["events_total"], "ticks": st["ticks_total"]},
    )
    return 0


async def _status_loop(engine: Engine, feed: HyperliquidFeed | None, every: float = 10.0) -> None:
    while True:
        await asyncio.sleep(every)
        st = engine.status()
        extra = {**st, **(feed.health() if feed else {})}
        extra.pop("feeds", None)
        log.info("status", extra={k: v for k, v in extra.items() if not isinstance(v, dict)})


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tapescreen", description="Real-time crypto scalp screener")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("--record", type=int, metavar="SECONDS", default=0,
                        help="record raw feed for N seconds, then exit")
    parser.add_argument("--replay", metavar="FILE", default="",
                        help="replay a recorded ndjson file instead of connecting live")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="replay speed multiplier (0 = as fast as possible)")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    log_file = setup_logging(cfg.log_path, cfg.feed_debug)
    log.info("tapescreen starting", extra={"log_file": str(log_file), "symbols": len(cfg.symbols)})
    t0 = time.time()
    try:
        rc = asyncio.run(run(cfg, args))
    except KeyboardInterrupt:  # pragma: no cover
        rc = 130
    log.info("exited after %.1fs", time.time() - t0)
    return rc
