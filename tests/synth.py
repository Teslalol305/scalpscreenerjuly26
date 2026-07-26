"""Deterministic synthetic Hyperliquid frame streams for pipeline-level tests.

Use: lines = synth_recording(coins, minutes=3); write_ndjson(path, lines).
Depends on: stdlib only. Same arguments -> byte-identical output (seeded PRNG).
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

T0 = 1_750_000_000.0  # synthetic session start (epoch seconds)


def synth_recording(
    coins: list[str],
    minutes: float = 3.0,
    trades_per_s: float = 4.0,
    seed: int = 42,
) -> list[tuple[float, str]]:
    """(ts_recv, raw_frame) tuples resembling a live capture."""
    rng = random.Random(seed)
    frames: list[tuple[float, str]] = [(T0, "Websocket connection established.")]
    tid = 0
    total_s = int(minutes * 60)
    for step in range(total_s * 10):  # 100ms grid
        ts = T0 + step / 10.0
        ts_ms = int(ts * 1000)
        for ci, coin in enumerate(coins):
            base = 100.0 * (ci + 1)
            px = base + 0.8 * math.sin((step + ci * 37) / 120.0) + rng.uniform(-0.05, 0.05)
            if rng.random() < trades_per_s / 10.0:
                tid += 1
                side = "B" if rng.random() < 0.55 else "A"
                sz = round(rng.uniform(0.05, 2.0), 3)
                frames.append((ts, json.dumps({"channel": "trades", "data": [{
                    "coin": coin, "side": side, "px": f"{px:.4f}", "sz": f"{sz}",
                    "time": ts_ms, "hash": "0x" + "ee" * 32, "tid": tid,
                    "users": ["0x" + "1" * 40, "0x" + "2" * 40]}]})))
            if step % 20 == ci % 20:  # each coin's book every 2s, staggered
                bids = [{"px": f"{px - 0.01 * (i + 1):.4f}", "sz": f"{1 + (i + step) % 4}.0",
                         "n": 1} for i in range(10)]
                asks = [{"px": f"{px + 0.01 * (i + 1):.4f}", "sz": f"{1 + (i + step + 1) % 4}.0",
                         "n": 1} for i in range(10)]
                frames.append((ts, json.dumps({"channel": "l2Book", "data": {
                    "coin": coin, "time": ts_ms, "levels": [bids, asks]}})))
            if step % 10 == 0:
                frames.append((ts, json.dumps({"channel": "bbo", "data": {
                    "coin": coin, "time": ts_ms, "bbo": [
                        {"px": f"{px - 0.01:.4f}", "sz": "1.0", "n": 1},
                        {"px": f"{px + 0.01:.4f}", "sz": "1.0", "n": 1}]}})))
            if step % 50 == 0:
                frames.append((ts, json.dumps({"channel": "activeAssetCtx", "data": {
                    "coin": coin, "ctx": {
                        "funding": f"{0.00001 * math.sin(step / 500.0):.8f}",
                        "openInterest": f"{50_000 + 10 * step:.1f}",
                        "prevDayPx": f"{base:.2f}", "dayNtlVlm": "1e6",
                        "premium": "0.0001", "oraclePx": f"{px:.4f}",
                        "markPx": f"{px + 0.002:.4f}", "midPx": f"{px:.4f}",
                        "impactPxs": None, "dayBaseVlm": "1e3"}}})))
    return frames


def write_ndjson(path: Path, frames: list[tuple[float, str]]) -> Path:
    path.write_text("".join(
        json.dumps({"ts": ts, "raw": raw}, separators=(",", ":")) + "\n" for ts, raw in frames))
    return path
