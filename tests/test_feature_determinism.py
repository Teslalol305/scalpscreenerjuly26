"""M2 acceptance: replaying the same recording twice yields identical feature streams."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import pytest

from tapescreen.config import Config
from tapescreen.core.engine import Engine
from tapescreen.core.features import FeatureSnapshot
from tapescreen.feeds.replayer import Replayer
from tests.synth import synth_recording, write_ndjson


async def feature_stream(cfg: Config, path: Path) -> list[FeatureSnapshot]:
    engine = Engine(cfg)
    captured: list[FeatureSnapshot] = []

    def make_hook(sym: str):
        fe = engine.features[sym]

        def hook(_st, _bar) -> None:
            captured.append(dataclasses.replace(fe.snapshot, extras={}))

        return hook

    for sym, st in engine.states.items():
        st.on_bar_1s.append(make_hook(sym))  # runs after FeatureEngine's rebuild

    queue: asyncio.Queue = asyncio.Queue()
    rep = Replayer(path, cfg.symbols, queue, speed=0)
    await rep.run()
    while not queue.empty():
        engine.on_event(queue.get_nowait())
    return captured


@pytest.mark.asyncio
async def test_double_replay_identical_feature_streams(cfg: Config, tmp_path: Path) -> None:
    coins = ["BTC", "ETH"]
    path = write_ndjson(tmp_path / "synth.ndjson", synth_recording(coins, minutes=2.5))
    a = await feature_stream(cfg, path)
    b = await feature_stream(cfg, path)
    assert len(a) > 200, "expected a dense snapshot stream"
    assert a == b
    # sanity: snapshots carry real feature values, not just defaults
    last_by_sym = {s.symbol: s for s in a}
    for sym in coins:
        s = last_by_sym[sym]
        assert s.price > 0 and s.vwap_session > 0
        assert s.spread_bps > 0
        assert s.cvd_session != 0.0
        assert s.atr_1m > 0  # 1m bars closed during 2.5 minutes
