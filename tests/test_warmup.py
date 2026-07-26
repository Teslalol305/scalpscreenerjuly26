"""Warmup parser tests (pure functions) + replay DB isolation."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from tapescreen.config import load_config
from tapescreen.feeds.warmup import parse_candles, parse_funding
from tapescreen.main import _replay_db_path
from tests.conftest import REPO


def test_parse_candles_shapes_and_order() -> None:
    raw = [  # out of order + one malformed row that must be skipped
        {"t": 120_000, "T": 179_999, "s": "BTC", "i": "1m",
         "o": "101", "c": "103", "h": "104", "l": "100", "v": "6", "n": 9},
        {"t": 60_000, "T": 119_999, "s": "BTC", "i": "1m",
         "o": "100", "c": "101", "h": "102", "l": "99", "v": "4", "n": 5},
        {"t": "garbage", "o": "1", "c": "1", "h": "1", "l": "1", "v": "x"},
    ]
    bars = parse_candles(raw)
    assert len(bars) == 2
    b0, b1 = bars
    assert (b0.ts, b1.ts) == (60.0, 120.0)  # ms -> s, oldest first, minute aligned
    assert (b0.open, b0.high, b0.low, b0.close, b0.volume, b0.ntrades) == (100, 102, 99, 101, 4, 5)
    # buy split is unknown for candles: CVD-neutral half/half
    assert b0.buy_volume == pytest.approx(2.0)
    assert b0.pv == pytest.approx(101 * 4)
    assert b0.pv2 == pytest.approx(101 * 101 * 4)
    assert parse_candles({"not": "a list"}) == []


def test_parse_funding_order_and_junk() -> None:
    raw = [
        {"coin": "BTC", "fundingRate": "0.0002", "time": 2_000_000},
        {"coin": "BTC", "fundingRate": "-0.0001", "time": 1_000_000},
        {"coin": "BTC", "fundingRate": None, "time": 3_000_000},
    ]
    assert parse_funding(raw) == [(1000.0, -0.0001), (2000.0, 0.0002)]
    assert parse_funding(None) == []


def test_replay_db_is_isolated_and_fresh(tmp_path: Path) -> None:
    cfg = load_config(REPO / "config.yaml")
    cfg.db_path = str(tmp_path / "live.db")
    path = _replay_db_path(cfg, "recordings/hl-20260726-101010.ndjson")
    assert path.parent == tmp_path
    assert path.name == "replay-hl-20260726-101010.db"
    # a stale replay DB from a prior run is deleted so every replay starts clean
    path.write_text("stale")
    again = _replay_db_path(cfg, "recordings/hl-20260726-101010.ndjson")
    assert again == path and not path.exists()


@pytest.mark.asyncio
async def test_replay_run_exits_nonzero_when_recording_missing(tmp_path: Path) -> None:
    from tapescreen.main import run

    cfg = load_config(REPO / "config.yaml")
    cfg.db_path = str(tmp_path / "live.db")
    args = Namespace(replay=str(tmp_path / "nope.ndjson"), record=0, speed=0.0, no_ui=True)
    rc = await run(cfg, args)
    assert rc == 1  # replayer task crash is supervised, logged, and fails the run
