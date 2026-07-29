"""Wiring + supervisor: config -> warmup -> feed/replayer -> engine, graceful shutdown.

Use: `python -m tapescreen [--record N] [--replay FILE --speed X] [--no-ui]`.
Depends on: config, logs, feeds.*, core.engine, server.app, store.db. This module
owns task lifecycle: a crashed task is logged, stops the app, and sets rc != 0.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import time
from pathlib import Path

from tapescreen.config import Config, load_config
from tapescreen.core.audit import Auditor
from tapescreen.core.engine import Engine
from tapescreen.feeds.hyperliquid import HyperliquidFeed
from tapescreen.feeds.recorder import Recorder
from tapescreen.feeds.replayer import Replayer
from tapescreen.feeds.warmup import run_warmup
from tapescreen.logs import setup_logging
from tapescreen.selftest import run_selftest
from tapescreen.server.app import UiServer
from tapescreen.store.db import Db

log = logging.getLogger("tapescreen.main")

QUEUE_CAP = 50_000  # hard cap on in-flight events (documented memory bound)


async def _consume(queue: asyncio.Queue, engine: Engine) -> None:
    while True:
        ev = await queue.get()
        engine.on_event(ev)
        queue.task_done()


def _replay_db_path(cfg: Config, recording: str) -> Path:
    """Replay never touches the live stats DB: fresh sibling DB per recording."""
    live = Path(cfg.db_path)
    path = live.with_name(f"replay-{Path(recording).stem}.db")
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(OSError):
            Path(str(path) + suffix).unlink()
    return path


async def run(cfg: Config, args: argparse.Namespace) -> int:
    queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_CAP)
    replay_mode = bool(args.replay)
    db = Db(_replay_db_path(cfg, args.replay) if replay_mode else cfg.db_path)
    # funding seeding from the DB uses wall-clock now-7d: live only, or replay
    # results would depend on prior DB contents and the day you run it
    engine = Engine(cfg, db, seed_funding_from_db=not replay_mode)
    stop = asyncio.Event()
    failed: list[str] = []

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # pragma: no cover - non-unix
            loop.add_signal_handler(sig, stop.set)

    def supervise(task: asyncio.Task) -> asyncio.Task:
        def cb(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                log.error("task %r crashed", t.get_name(), exc_info=exc)
                failed.append(t.get_name())
                stop.set()

        task.add_done_callback(cb)
        return task

    tasks: list[asyncio.Task] = [
        supervise(asyncio.create_task(_consume(queue, engine), name="consume"))
    ]
    recorder: Recorder | None = None
    feed: HyperliquidFeed | None = None

    if replay_mode:
        replayer = Replayer(args.replay, cfg.symbols, queue, speed=args.speed,
                            top_levels=cfg.features.book_top_levels)

        async def _replay_then_stop() -> None:
            try:
                await replayer.run()
                await queue.join()  # let the consumer drain before shutdown
            finally:
                stop.set()

        tasks.append(supervise(asyncio.create_task(_replay_then_stop(), name="replayer")))
    else:
        # REST warm-up + symbol availability check (non-fatal if unreachable)
        unavailable = await run_warmup(cfg, engine, db)
        live_symbols = {s: e for s, e in cfg.symbols.items() if s not in unavailable}
        if not live_symbols:
            log.error("no available symbols to subscribe; check symbol_map.yaml")
            db.close()
            return 1
        if cfg.recorder.enabled or args.record:
            recorder = Recorder(cfg.recorder.path, cfg.recorder.rotate_mb)
            log.info("recording to %s", recorder.path)
        feed = HyperliquidFeed(cfg.feed, live_symbols, queue, recorder, debug=cfg.feed_debug,
                               top_levels=cfg.features.book_top_levels)
        tasks.append(supervise(asyncio.create_task(feed.run(), name="feed")))

    if args.record:
        async def _timer() -> None:
            await asyncio.sleep(args.record)
            log.info("record window of %ss elapsed", args.record)
            stop.set()

        tasks.append(supervise(asyncio.create_task(_timer(), name="record-timer")))

    auditor = Auditor(cfg, engine, feed, db)

    async def _audit_loop() -> None:
        while True:
            await asyncio.sleep(cfg.audit.interval_s)
            auditor.run()

    tasks.append(supervise(asyncio.create_task(_audit_loop(), name="auditor")))

    if engine.research is not None:
        async def _research_loop() -> None:
            while True:
                await asyncio.sleep(cfg.research.interval_s)
                engine.research.meeting()

        tasks.append(supervise(asyncio.create_task(_research_loop(), name="research")))

    ui: UiServer | None = None
    if not args.no_ui:
        ui = UiServer(cfg, engine, feed, db, auditor=auditor)
        tasks += [supervise(t) for t in await ui.start()]

    status_task = supervise(asyncio.create_task(_status_loop(engine, feed), name="status"))

    await stop.wait()
    log.info("shutting down")
    if feed is not None:
        feed.stop()
    if ui is not None:
        ui.request_stop()
    await asyncio.sleep(0.2)  # let uvicorn begin its graceful exit
    for t in tasks:
        t.cancel()
    status_task.cancel()
    await asyncio.gather(*tasks, status_task, return_exceptions=True)
    if recorder is not None:
        recorder.close()
        log.info("recording flushed: %s", recorder.path)
    db.close()
    st = engine.status()
    log.info(
        "session summary: %s events (%s ticks) in %.1fs",
        st["events_total"], st["ticks_total"], st["uptime_s"],
        extra={"events": st["events_total"], "ticks": st["ticks_total"]},
    )
    if failed:
        log.error("exiting after task failure in: %s", ", ".join(failed))
        return 1
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
    parser.add_argument("--no-ui", action="store_true", help="run headless (no dashboard)")
    parser.add_argument("--selftest", action="store_true",
                        help="run the logic self-test and exit")
    args = parser.parse_args(argv)

    if args.selftest:
        errs = run_selftest()
        print("selftest:", "OK - all checks passed" if not errs else f"FAILED: {errs}")
        return 0 if not errs else 1

    if args.replay and not Path(args.replay).exists():
        parser.error(f"recording not found: {args.replay}")

    cfg = load_config(args.config)
    log_file = setup_logging(cfg.log_path, cfg.feed_debug)

    if cfg.audit.selftest_on_boot:
        errs = run_selftest()
        if errs:
            log.error("boot self-test FAILED - refusing to run on broken logic: %s", errs)
            return 1
        log.info("boot self-test passed (%s)", "core math verified")
    log.info("tapescreen starting", extra={"log_file": str(log_file), "symbols": len(cfg.symbols)})
    t0 = time.time()
    try:
        rc = asyncio.run(run(cfg, args))
    except KeyboardInterrupt:  # pragma: no cover
        rc = 130
    log.info("exited after %.1fs", time.time() - t0)
    return rc
