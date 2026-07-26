"""Startup REST warm-up: symbol availability, 1m candle seeds, funding history seeds.

Use: unavailable = await run_warmup(cfg, engine, db)  (live mode only, non-fatal).
Depends on: stdlib urllib (run in a thread), core.state.Bar, store.db. Any network
failure logs a warning and degrades gracefully - baselines then warm from live tape.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
from typing import Any

from tapescreen.config import Config
from tapescreen.core.engine import Engine
from tapescreen.core.state import Bar
from tapescreen.store.db import Db

log = logging.getLogger("tapescreen.warmup")


def _post(info_url: str, payload: dict[str, Any], timeout: float = 10.0) -> Any:
    req = urllib.request.Request(
        info_url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def parse_candles(raw: Any) -> list[Bar]:
    """candleSnapshot rows {t,T,s,i,o,c,h,l,v,n} -> closed 1m Bars (oldest first).

    Candles carry no aggressor split, so buy_volume is set to volume/2 (CVD-neutral);
    seeded bars only feed 1m indicators (ATR/RSI/EMA/BB), never CVD windows.
    """
    bars: list[Bar] = []
    if not isinstance(raw, list):
        return bars
    for c in raw:
        try:
            ts = float(c["t"]) / 1000.0
            o, h, lo, cl = float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"])
            v = float(c["v"])
        except (KeyError, TypeError, ValueError):
            continue
        bars.append(Bar(ts=ts - ts % 60.0, open=o, high=h, low=lo, close=cl, volume=v,
                        buy_volume=v / 2.0, ntrades=int(c.get("n") or 0),
                        pv=cl * v, pv2=cl * cl * v))
    bars.sort(key=lambda b: b.ts)
    return bars


def parse_funding(raw: Any) -> list[tuple[float, float]]:
    """fundingHistory rows {coin, fundingRate, time} -> (ts_seconds, rate), oldest first."""
    out: list[tuple[float, float]] = []
    if not isinstance(raw, list):
        return out
    for row in raw:
        try:
            out.append((float(row["time"]) / 1000.0, float(row["fundingRate"])))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort()
    return out


async def run_warmup(cfg: Config, engine: Engine, db: Db | None) -> set[str]:
    """Check availability and seed baselines. Returns ui symbols to NOT subscribe."""
    url = cfg.feed.info_url
    unavailable: set[str] = set()

    try:
        meta, _ctxs = await asyncio.to_thread(_post, url, {"type": "metaAndAssetCtxs"})
        by_name = {a.get("name"): a for a in meta.get("universe", [])}
        for sym, entry in cfg.symbols.items():
            asset = by_name.get(entry.venue_symbol)
            if asset is None:
                log.warning("symbol %s not on hyperliquid (venue_symbol=%s) - marked unavailable",
                            sym, entry.venue_symbol)
                unavailable.add(sym)
            elif asset.get("isDelisted"):
                log.warning("symbol %s is delisted - marked unavailable", sym)
                unavailable.add(sym)
    except Exception as e:
        log.warning("symbol availability check skipped (%s: %s)", type(e).__name__, e)
        engine.mark_unavailable(set())
        return set()

    engine.mark_unavailable(unavailable)
    now_ms = int(time.time() * 1000)
    seeded_c = seeded_f = 0
    for sym, entry in cfg.symbols.items():
        if sym in unavailable:
            continue
        coin = entry.venue_symbol
        if cfg.feed.warmup_candles:
            try:
                raw = await asyncio.to_thread(_post, url, {
                    "type": "candleSnapshot",
                    "req": {"coin": coin, "interval": "1m",
                            "startTime": now_ms - cfg.feed.warmup_candle_bars * 60_000,
                            "endTime": now_ms},
                })
                bars = parse_candles(raw)
                if bars:
                    engine.states[sym].seed_bars_1m(bars)
                    seeded_c += 1
            except Exception as e:
                log.warning("candle warmup failed for %s: %s", sym, e)
        if cfg.feed.warmup_funding_history:
            try:
                raw = await asyncio.to_thread(_post, url, {
                    "type": "fundingHistory", "coin": coin,
                    "startTime": now_ms - 7 * 86_400_000,
                })
                hist = parse_funding(raw)
                if hist:
                    engine.states[sym].seed_funding(hist)
                    seeded_f += 1
                    if db is not None:
                        for ts, rate in hist:
                            db.insert_funding(sym, ts, rate)
            except Exception as e:
                log.warning("funding warmup failed for %s: %s", sym, e)
    log.info("warmup done: %d unavailable, %d candle-seeded, %d funding-seeded",
             len(unavailable), seeded_c, seeded_f)
    return unavailable
